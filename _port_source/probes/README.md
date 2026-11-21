# Lab probes

Three questions this suite's design rests on were answered on **VergeOS
4.13** in Aug 2026. The lab now runs **26.1.8**, and our supported floor
is 26.0. Each answer below may have changed, and each one is currently
load-bearing for a design decision. Re-run these before building
anything on top of them.

Order matters: probe 1 or 2 landing positively makes the current
failback design obsolete, so do them before touching failback code.

---

## Probe 1 — What does the oVirt API actually expose?

**The premise under test.** `README-DR.md` justifies a full-copy
failback with "VergeOS exposes no changed-block API". VergeOS 26.1.2+
ships an optional `oVirt-engine` package, and Veeam B&R 13.1 backs up
VergeOS through it *with CBT incremental backups*. If VergeOS implements
oVirt's `imagetransfers` (block-level disk read/write) and per-VM
`checkpoints` (the CBT anchor), the premise is dead and failback should
be redesigned around a delta.

Nobody has documented which subset is implemented. The marketing pages
don't say; `docs.verge.io`'s Veeam page explicitly declines to enumerate
capabilities.

```bash
# Prereq: oVirt-engine package installed, VergeOS 26.1.7+
#   (26.1.4 fixed early token-reuse and tenant-GUID bugs -- do not
#    test on 26.1.2/.3 and conclude anything)
export VERGEOS_HOST=<lab>
export VERGEOS_USERNAME=admin
export VERGEOS_PASSWORD=...

./probes/probe_ovirt_api.py            # human-readable
./probes/probe_ovirt_api.py --json     # for attaching to a ticket
```

Read-only: every request is a GET, and the write-capable endpoints are
only ever listed. The script prints one of three verdicts and tells you
what each implies.

**If `checkpoints` is PRESENT**, the follow-up question is whether the
checkpoint is a real CBT anchor or a stub that reports the whole disk
dirty. Test that by taking two checkpoints across a known small write
and comparing the reported dirty extent against the bytes written —
exactly the shape of assertion `tools/tests/test_nbd_pump_bitmap.py`
already makes on the Proxmox side.

---

### RESULT — run 2026-09-21 on conundrum-lab (26.1.8)

**Both endpoints present and fully functional.** oVirt Engine
`4.5.6-1.el9.vos26.1.8`, already enabled — no package install needed.
The full chain was exercised: full backup -> checkpoint -> incremental
backup anchored on it -> image transfer -> dirty extents -> byte read.
On a busy VM across 90 s: 1195 extents, 597 dirty, 165 MiB of 50 GiB
(0.32 %), 64 KiB-aligned. Bytes read back real content. Details and the
anti-self-deception checks are in `README-DR.md`'s failback section.

**Conclusion: delta failback is possible.** Redesign failback; do not
build on the exporter-VM path.

Two things this probe got wrong on its first run, now fixed in the
script:

1. **The shim returns HTTP 200 for every path**, including
   `/vms/<id>/unicorns`, carrying the error in the body. The original
   status-code classification reported everything as PRESENT. It now
   classifies on the body.
2. With that fixed, `/storagedomains` and `/events` turn out to be
   **absent** — they were false positives. Anything relying on oVirt's
   event stream for job progress will not work.

**Checkpoint durability: TESTED, passes.** Live migration (node2 ->
node1, VM stayed up) and a full power cycle both preserve the
checkpoint, and tracking continues across them — a checkpoint taken
while `debian13-golden` was *powered off* correctly reported 12.06 MiB
dirty (6 of 13 extents) after booting it. That is the property
Proxmox's `blockdev-mirror` lacks. Details in `README-DR.md`.

Still untested: **node reboot**. Deliberately not attempted — the lab
has two nodes, so rebooting one changes vSAN redundancy. Needs a
human decision and a maintenance window.

**New defect found while testing:** the oVirt `migrate` action is
advertised in the VM's `actions` list but does not work —
`POST /ovirt-engine/api/vms/{id}/migrate` returns HTTP 201 with
`{"error_code":"server_error","error":"No such file or directory"}` and
the VM never moves. The native call works:
`POST /api/v4/vm_actions {"vm":<key>,"action":"migrate","node":<n>}`.
Worth filing: an advertised-but-broken action is worse than an absent
one, because a backup vendor's oVirt driver will believe it.

---

## Probe 2 — Is the VM export volume readable over NFS? (finding #22)

**The premise under test.** Finding #22 says a VergeOS VM export
reports success and a real byte count, but its `ybvm` output is not
visible on the target NAS volume through NFS, the volume browser API, or
`used_bytes` — so nothing outside VergeOS can read it. That single
finding is why failback uses a bespoke exporter VM.

Current docs contradict it: they state you "set up a **CIFS or NFS
share** for the NAS volume" to reach exported snapshots, that exports
land in timestamped folders with an optional `current` folder for stable
absolute paths, and that they can be pushed offsite via NAS volume sync.

So either this was a 4.13 bug since fixed, or the original test missed
the `Allow Export` / share configuration step. Both are cheap to
distinguish.

```
1. NAS service running. Create a volume of type "Verge.io VM Export".
2. On a test VM, ENABLE "Allow Export".            <-- easy to miss
3. Share that volume over NFS, root access allowed from the controller.
4. Trigger the export (manual, or a task schedule).
5. From the controller:
     mount -t nfs <nas-ip>:/<volume> /mnt/probe
     find /mnt/probe -maxdepth 3 -printf '%10s  %p\n' | sort
6. Confirm: a timestamped folder exists; it contains a .ybvm (or .ovf)
   AND the disk payload; file sizes are non-zero; and a checksum of a
   disk file is stable across two reads.
```

**If the payload is readable**, failback collapses to: export → read
over NFS → `qemu-img convert` into the original Proxmox disk. That
deletes the exporter-VM path, its cloud-init template branch, and the
`fb_standby_override` workaround. Note it is still a *full* copy — an
export is a snapshot, not a delta — so it only beats the current design
on simplicity, not on bytes moved. Probe 1 is the one that changes bytes
moved.

---

### STATUS — not run (2026-09-21)

Blocked, not skipped: conundrum-lab has exactly one volume
(`system-logs`, ext4) and no NAS service, so there is no VM export
volume to test. Running this probe means provisioning a NAS service,
an export volume and a share first.

Lower priority now: Probe 1 succeeded, and an export is a snapshot
(full copy) whereas the oVirt path gives a delta. This probe now only
matters as a simpler fallback, not as the preferred design.

---

## Probe 3 — Does the first-boot panic still reproduce?

**The premise under test.** On 4.13, the first boot from
freshly-replicated disks reproducibly panicked in early init
("Attempted to kill init", ~1.45 s, identical RIP across runs), with the
next boot always clean and the disk verified intact. Reproduced 2/2.
Failover therefore power-cycles every VM once
(`dr_first_boot_cycle: true`, `dr_settle_pause: 45`).

That workaround is **the single largest controllable chunk of failover
RTO** — roughly 90 s per VM, on every failover, forever. If 26.x doesn't
reproduce it, deleting the cycle is the cheapest RTO win available.

```bash
# On a protected, converged, NON-production standby:
./vergeos-dr status                      # confirm IN SYNC first

ansible-playbook dr_site.yml -t failover \
  -e failover_vms=<vmid> -e confirm_failover=yes \
  -e dr_first_boot_cycle=false           # <-- workaround disabled

# Watch the FIRST boot on the VM's console. Record:
#   - does it panic in early init?
#   - if so, at what elapsed time, and is the RIP identical across runs?
#   - does the second boot come up clean?
#   - does the guest filesystem verify (fsck clean / data markers intact)?
```

Repeat at least 3 times before concluding. The original was 2/2, so a
single clean boot proves nothing — and a flaky panic is worse than a
reliable one, because the settle cycle currently hides it.

**Keep `dr_first_boot_cycle: true` as the default until this is done.**
It costs 90 s; being wrong costs a failed failover during an outage.

### STATUS — not run (2026-09-21)

Requires a protected standby fed by a live Proxmox source. The lab does
have a 4-node PVE cluster (`mox-1`..`mox-4`, 192.168.1.100-103,
welchums.lab), but nothing in `vms.yml` is currently protected and the
agent's sandbox blocks SSH to the PVE nodes, so neither this probe nor
any QEMU-side claim could be measured. Needs a human-run session.

---

## Recording results

Write outcomes into `UPSTREAM-FINDINGS.md` with the platform version
they were measured on, the way the existing entries do. A finding
without a version is unactionable a year later — which is exactly the
position these three are in now.
