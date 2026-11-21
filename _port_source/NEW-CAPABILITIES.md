# New capabilities — status, evidence, and what is still unproven

Six ideas were commissioned. Five are built and tested, one was killed by
a spike after it turned out to need a platform primitive that does not
exist. Nothing here is claimed beyond what was measured.

| # | Capability | State | Evidence |
|---|---|---|---|
| 1 | Merkle index | **built, tested** | 25 unit tests incl. brute-force property check; end-to-end over real `qemu-nbd` |
| 2 | Wave dedup | **built, tested** | 10 unit tests; 7.4–7.9x measured on a simulated wave |
| 3 | Rewind buffer | **built, tested on the lab** | create/list/prune verified against conundrum-lab; expiry confirmed |
| 5 | Chaos harness | **built, tested** | 10 fault-injection tests, all green |
| 6 | Network translation | **built, tested on the lab** | 163 unit tests across 5 gates; end-to-end probe green against conundrum-lab |
| 7 | Lazy migration | **killed by spike** | VergeOS has no external disk backing (see below) |

Total suite: **402 tests**. ansible-lint passes the configured `basic`
profile and also `moderate`; it does **not** pass `production` (23
pre-existing `no-changed-when` / `risky-shell-pipe` / `no-handler`
findings, none of them new). An earlier revision of this file claimed
production-clean. That was wrong.

---

## 1. Merkle index — `nbd_pump merkle`, `resync --merkle`

Divergence is found by descending a hash tree instead of exchanging a
digest per chunk. Measured on a 4096-chunk disk, fanout 16, depth 3:

| changed | round trips | nodes compared | wire | vs flat |
|---|---|---|---|---|
| 0 | **1** | 1 | 16 B | 9267x |
| 1 | 4 | 49 | 0.8 KB | 189x |
| 32 | 4 | 657 | 10.5 KB | 14x |
| 256 | 4 | 2961 | 47.4 KB | 3x |

End-to-end against a real `qemu-nbd` export: 3 of 512 chunks found,
196,608 bytes written, replica byte-identical, and a second run cost
**one round trip and one node**.

Correctness is pinned by a brute-force property test — 25 randomised
trials comparing the descent's answer against a byte-for-byte diff —
plus fanout-independence (2/4/8/16), depth-0 disks, and refusals on
geometry or size mismatch.

**Honest limits.** Past roughly 10% divergence the descent stops paying
for itself and the flat path is simpler; both are kept. The bigger prize
is not yet realised: a receiver that maintains the tree *incrementally as
writes land* would never read its disk at all, turning resync from
O(disk) into O(divergence) in time as well as bandwidth. Today both sides
still recompute, so wall-clock is unchanged — only the wire shrinks.

## 2. Wave dedup — `tools/dedup.py`

`index` (receiver) -> `plan` (source) -> `fill` (receiver). Only chunks
nobody in the wave has sent cross the link; the rest are filled by a
local copy on the receiver.

Measured, 10 VMs of 16 MiB from a common golden image:

| sharing | naive | deduped link | filled locally | index |
|---|---|---|---|---|
| 80% | 160.0 MiB | **21.5 MiB (7.4x)** | 138.4 MiB | 0.01 MB |
| 85% | 160.0 MiB | **20.3 MiB (7.9x)** | 139.5 MiB | 0.01 MB |

Better than the 3.1x I modelled, because each VM dedups against every
previously-seeded VM, not just the golden image.

`fill` re-reads and re-verifies each source chunk's digest before
writing. A collision or a stale index would corrupt a replica silently;
the check costs a local read and makes acting on one impossible. Tested:
a deliberately corrupted index is refused.

**Not yet wired into the playbooks** — the tool and its guarantees are
tested, the orchestration is not written.

## 3. Rewind buffer — `-t rewind`

Block replication is a faithful corruption-propagation machine:
ransomware on the source reaches the standby in seconds, and a standby
holds exactly one point in time — the damaged one. Periodic standby
snapshots give failover a time axis.

Verified against conundrum-lab: three rewind points created with a 2h
expiry, listed in creation order, pruned to depth (correctly keeping the
newest), and `expires` confirmed set on the row. Lab returned to its
prior state.

Crash-consistent, deliberately: the API refuses `quiesced` on create, and
it would be meaningless anyway — the receiver guest never mounts the
replica disks, so there is no filesystem of its own to freeze.
Consistency comes from the source-side quiesce at failover.

**Not claimed: immutability.** VergeOS has immutable snapshots, but the
flag is not on `machine_snapshots`; it appears to live in snapshot
profiles. Wiring the buffer to an immutable profile is the obvious next
step and would make the buffer resistant to an attacker who owns the
source — but it is unverified, so it is not claimed.

## 5. Chaos harness — `tools/tests/test_chaos.py`

Encodes one rule: after any injected fault, the data is byte-correct or
the tooling exits non-zero. A clean exit over a wrong replica is the only
unacceptable outcome.

Faults covered: receiver drops the connection mid-seed and mid-resync;
receiver unreachable; digest payload truncated / emptied / garbled /
key-renamed; digests from the wrong disk; `SIGKILL` mid-resync followed
by a convergence run; randomised divergence soak.

This exists because of a real miss. Cycle mode shipped with a crash path
nobody had exercised — a `SIGKILL`ed source leaves its bitmap
`inconsistent, recording=False, count=0`, which every check read as
"healthy and caught up". No unit test could have caught it, because
nothing ever crashed anything.

## 7. Lazy migration — spiked, not feasible

The idea: boot the VM in VergeOS against a disk that reads through to the
source, faulting blocks in on demand, so downtime is independent of disk
size.

It requires a VergeOS disk whose backing store is external. It is not:
`machine_drives.media_source` points at a row in `files`, and a `files`
row has **no externally-referencing field at all** (checked every field;
types are limited to `dir/img/iso/lrq/nvram/ova/qcow2/raw`). A drive is
always backed by a local vSAN file.

**Killed** — not deferred. The blocker is a missing platform primitive
rather than effort, so it belongs in a feature request: a `files` row that
can reference an external URL/NBD target, or an import mode that boots
then fetches. If that ever exists, this is about a week's work.

It matters little either way. The goal — downtime independent of disk
size — is already achieved by the DR path, which pre-copies instead of
post-copying and cuts over in ~30–90 s regardless of size. Lazy migration
would only have helped where pre-copy is impossible (no source-side QMP,
a read-only source, a hard bandwidth cap). Worth saying in the request,
because that is a narrow market.

## 6. Network translation — `pve_net_parse` / `pve_topology` / `netmap` / `netguard`

Built as five gates, because "migrate the network" is not one problem.
163 unit tests plus a live lab probe.

### Gate 1 — capture (`roles/proxmox_export/files/pve_net_parse.py`, 40 tests)

This gate fixed **two live correctness bugs**, not merely missing
features. The old capture was one regex, `bridge=([^,]+)`, defaulting to
`vmbr0`:

* It dropped `tag=`. Four NICs on `vmbr0` tags 20/30, a trunk, and a
  plain port all reduced to `vmbr0` and were then collapsed into a single
  OVF `<Network>` — a test actually asserted that collapse was correct.
  Migrating them merged isolated VLANs into one broadcast domain.
* It invented a bridge. A Proxmox NIC with no `bridge=` is *deliberately*
  unconnected; defaulting it to `vmbr0` attached an isolated interface to
  production.

The unit of identity is now the **segment** — `(bridge, vlan)` — written
`vmbr0`, `vmbr0.20` or `vmbr0:trunk`. Also captured: NIC model, MTU
(including virtio's `mtu=1` "inherit" sentinel, which must never be
programmed as a 1-byte MTU), rate, queues, firewall, link_down.

Model matters more than it looks: silently turning virtio into E1000
renames a Linux guest's interface (`ens18` -> `eth0`) and strands a
statically-addressed host. The OVF now carries it.

### Gate 2 — host topology (`roles/proxmox_export/files/pve_topology.py`, 31 tests)

A bridge *name* is not a network *definition*. `vmbr0` says nothing about
uplink, VLAN-awareness or subnet. This parses `/etc/network/interfaces`
(bridges, bonds, VLAN sub-interfaces, OVS) and `/etc/pve/sdn/*.cfg`.

SDN is the prize: zones carry the bridge, vnets the VLAN, subnets the
CIDR, gateway and DHCP range — exactly what synthesis needs, stated
rather than inferred. Every fact carries a `confidence`, and the host's
own address is reported as `host_addresses`, never as the segment's
subnet: on a VLAN-aware bridge they are routinely different networks.

**Still unverified against a real PVE node** — there is no authorized SSH
key on mox-1..4, so this is fixture-tested only.

### Gate 3 — mapping policy (`tools/netmap.py`, 61 tests)

Policy is a **choice**, not a fixed rule. Resolution is an ordered chain
of named resolvers; the first to answer wins:

| resolver | matches on |
|---|---|
| `explicit` | the operator's own segment -> vnet table |
| `alias` | an existing vnet named after the source's SDN alias |
| `vlan` | an existing vnet carrying the same VLAN |
| `name` | an existing vnet named after the segment, vnet or bridge |
| `synthesize` | emits a create-spec for a vnet the source implies |

`order` both selects and sequences them, so `[explicit]` is a strict
table, `[explicit, vlan, name]` is map-to-existing with a manual
override, and appending `synthesize` makes creation the last resort.
Disabling is by omission. **The default order omits `synthesize`** —
creating networks is opt-in.

Two rules hold under every policy:

* **Ambiguity is never guessed.** Two vnets on one VLAN is a refusal
  naming both, and it *stops the chain* rather than falling through to
  synthesis, which would add a third network beside the two that already
  collided.
* **Synthesis is gated on confidence.** A bridge known only by name
  cannot produce a subnet; inventing one puts guests on a network that
  silently does not route.

### Gate 4 — blast radius (`tools/netguard.py`, 43 tests)

This is the reason the feature was unbuilt, and it is a separate stage on
purpose. netmap answers *which vnet this NIC belongs on* — a topology
question with a correct answer even while the source runs. Whether it is
**safe to attach right now** is a different question with a different
input, and conflating the two is how a DR rehearsal takes down the
production it was rehearsing for.

Replication preserves MACs by design. During a rehearsal the source is
still up. Attach its twin to a vnet on the same layer 2 and two hosts
share one MAC and one IP.

| mode | behaviour |
|---|---|
| `detached` (default) | NICs created, connected to nothing; intended target recorded |
| `bubble` | everything forced onto one isolated vnet |
| `live` | real attachment — only when the source is **confirmed down** |

Anything not clearly down — including *unknown* and *not supplied* —
counts as running. Absence of evidence is not evidence of absence when
the failure mode is an outage. The override is **per-VM by design**: a
global "yes" gets ticked once and then applies forever, to VMs nobody was
thinking about. The guard also refuses duplicate MACs within a wave and
MACs already live on a different VergeOS VM, while treating a re-run
against the same VM as idempotent.

Bubble isolation is reported as an operator **assertion**, not a check —
the tool cannot see the bubble's uplink, and claiming verification would
be worse than saying nothing.

### Gate 5 — the lab (`probes/gate5_network_verify.yml`)

Runs the real pipeline against conundrum-lab with throwaway objects,
removed in an `always:` block. **Green: `failed=0`.** Verified: the guard
refuses a live attach over a running source; the synthesised vnet is
created and is idempotent; the VLAN lands as `layer2_type`/`layer2_id`;
DHCP keeps its upper bound; a NIC re-applied twice does not duplicate and
keeps its MAC, vnet and model.

It earned its keep by finding four things no unit test could. Each was
then re-tested against collection dev HEAD
(`fix/issues-18-19-network-field-mappings` @ `c4f7397`) and tracked:

1. **`network_type: vlan` does not exist.** VergeOS vnet types are
   `internal | external | dmz`; VLAN tagging is `layer2_type` +
   `layer2_id`, orthogonal to the vnet's role. My synthesiser emitted
   `vlan` as a type. **Already fixed on dev** — the argument spec now
   reads `choices=['internal', 'external', 'dmz']`, and a live ladder
   asserts it. Tracked by collection issue #18; nothing to do.
2. **The source subnet's gateway is the vnet's `ip_address`, not its
   `gateway`.** In Proxmox SDN, `gateway 10.0.20.1` is the address the
   SDN router holds on that subnet; in VergeOS the vnet *is* that router.
   Posting `network` + `gateway` with no address is rejected outright
   with *"Gateway is outside of network"*, because gateway is validated
   against the vnet's own address — which is still empty at that point. A
   vnet's `gateway` means its **upstream** router, which a source subnet
   does not describe, so it is now never inferred and is emitted only
   when an operator supplies one.
3. **`filter=name eq <value>` needs the value quoted.** `name eq
   External` works; `name eq netmap-gate5-tmp` returns 422 *"Invalid
   argument"* — any hyphen or space breaks the unquoted token.
4. **`vm_nic_info` is not in the released collection**, only the
   maintainers' working tree. Re-confirmed: still absent from
   `origin/main` @ v2.0.1. The probe reads NICs over the API instead,
   because a probe that needs an unreleased module cannot verify
   anybody's estate. Porting is already tracked by collection issue #57
   via PR #23; nothing new to file.

**Two defects found and filed upstream**, not worked around:

* **collection issue #59** — `vergeio.vergeos.nic` reports `changed=true`
  forever when `mac_address` is supplied **uppercase**. Isolated with a
  three-arm control: uppercase never converges, lowercase converges, no
  MAC converges. `update_nic()` compares the API's lowercase `macaddress`
  against the parameter case-sensitively. The NIC itself is correct, so it
  is a reporting defect — but uppercase is the *normal* path for migration
  tooling, because that is what `qm config` and OVF `rasd:Address` emit.
  Fourth recurrence of the #8/#10/#18 class, different root cause.
* **collection issue #60** — the `network` module cannot set a vnet's
  uplink (`interface_vnet`), so **every vnet it creates is unreachable
  from the physical fabric**. See below.

The probe asserts the invariants that matter and prints the defects rather
than hiding them.

### What is still unproven

* Gate 2 against a real PVE node (no SSH access to mox-1..4).
* Nothing is wired into the cutover playbooks yet. The pipeline is
  `capture -> topology -> plan -> guard`; applying a guarded plan is the
  remaining orchestration.
* Trunk ports are refused rather than translated. A trunk carries several
  VLANs and no single vnet represents it; that needs several vnets and a
  guest-side decision.
* `rate_limit` exists on a VergeOS vnet (`mbytes/second`, a clean match
  for Proxmox's `rate=`), but the collection's `network` module does not
  expose it, so the source's rate limit is captured and then dropped.
* **Synthesised vnets have no uplink** (collection issue #60). A VergeOS
  vnet reaches the fabric through `interface_vnet`, pointing at a
  `physical` vnet; the module does not expose it, and nothing is
  auto-assigned. The API accepts `interface_vnet` perfectly well — only
  the module lacks the parameter. So a synthesised VLAN vnet carries the
  right tag onto **nothing**, and guests land in an isolated layer 2.
  `netmap` now marks such specs `isolated: true` and the plan warns,
  because silently shipping "we reproduced your network" would be false.
  Until #60 lands, the honest options are to map onto operator-built
  vnets, or attach the uplink by hand after synthesis.

---

## Papercut found along the way

`--chunk` is a **global** option for `seed`/`apply` but a **subcommand**
option for `digest`/`resync`/`merkle`, with different defaults (1 MiB vs
4 MiB). Easy to trip over — it cost a test failure. Not churned, because
playbooks already depend on the current shape, but it should be
unified.
