#!/usr/bin/env python3
"""Decide which VergeOS vnet each migrating NIC belongs on.

Pure planning. Consumes the NIC facts from a source connector, the host
topology facts, an inventory of existing VergeOS vnets, and a policy;
emits a PLAN. It never talks to anything -- applying the plan is the
playbook's job, and keeping the decision separable is what makes it
reviewable before anything is created or attached.

Mapping policy is a CHOICE, not a fixed rule
--------------------------------------------
Sites differ too much for one answer. A lift-and-shift into a prepared
VergeOS estate wants "attach to the vnets I already built". A greenfield
DR target wants "build me what the source had". Most real waves want some
of each, with the operator pinning the handful that matter.

So resolution is an ORDERED CHAIN of named resolvers. Each one either
answers or abstains, and the first answer wins:

  explicit     operator's own segment -> vnet table. Always authoritative.
  alias        match an existing vnet by the source's SDN alias.
  vlan         match an existing vnet carrying the same VLAN id.
  name         match an existing vnet by (normalised) segment or bridge name.
  synthesize   emit a create-spec for a vnet the source implies.

`order` selects and sequences them, so `[explicit]` is a strict table,
`[explicit, vlan, name]` is map-to-existing with a manual override, and
`[explicit, alias, vlan, name, synthesize]` is best-effort with creation
as the fallback. Disabling is by omission.

Two rules hold regardless of policy:

  * Ambiguity is never resolved by guessing. If a resolver finds more than
    one candidate, the NIC is REFUSED and the candidates are reported.
  * Synthesis is gated on the CONFIDENCE of the topology facts. A bridge
    known only by name cannot produce a subnet, and inventing one would
    put guests on a network that silently does not route.
"""

import argparse
import ipaddress
import json
import re
import sys

RESOLVERS = ("explicit", "alias", "vlan", "name", "synthesize")

# Ordered weakest -> strongest. Synthesis compares against the policy's
# floor; see pve_topology.py for how each level is earned.
CONFIDENCE_RANK = {"name-only": 0, "host": 1, "sdn": 2, "authoritative": 3}

# The vnet roles VergeOS accepts. Confirmed against the lab API: the
# collection dropped 'vlan' and 'overlay' as types in 2.1.0 because the
# server never had them.
VNET_TYPES = ("internal", "external", "dmz")

DEFAULT_POLICY = {
    "order": ["explicit", "alias", "vlan", "name"],
    "explicit": {},
    # refuse | detach | bubble -- what to do with a NIC nothing resolved.
    # 'refuse' is the default because a wrongly-attached NIC is worse than
    # a stopped migration.
    "on_unmapped": "refuse",
    "bubble_network": "",
    "synthesize": {
        "require_confidence": "authoritative",
        "name_template": "{segment}",
        # VergeOS vnet types are internal | external | dmz. There is no
        # 'vlan' type: VLAN tagging is layer2_type=vlan + layer2_id, which
        # is orthogonal to the vnet's role. Verified against the lab API,
        # where a vnet row carries type/layer2_type/layer2_id separately.
        "network_type": "internal",
        "dhcp": "inherit",
        "allow_trunk": False,
        # A VergeOS vnet's `gateway` is its upstream router. A source
        # subnet says nothing about that, so it is never inferred.
        "upstream_gateway": "",
    },
    # Gate 4. detached | bubble | live
    "attach_mode": "detached",
    "allow_live_attach": False,
}


class PolicyError(ValueError):
    """Policy that cannot be honoured. Never silently downgraded."""


def normalise(name):
    """Fold names for comparison only -- never for creation."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def merge_policy(user):
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    for key, value in (user or {}).items():
        if isinstance(value, dict) and isinstance(policy.get(key), dict):
            policy[key].update(value)
        else:
            policy[key] = value

    unknown = [r for r in policy["order"] if r not in RESOLVERS]
    if unknown:
        raise PolicyError(
            "unknown resolver(s) %s; valid: %s"
            % (", ".join(unknown), ", ".join(RESOLVERS)))
    if not policy["order"]:
        raise PolicyError("policy 'order' is empty -- nothing would resolve")
    if policy["on_unmapped"] not in ("refuse", "detach", "bubble"):
        raise PolicyError("on_unmapped must be refuse, detach or bubble")
    if policy["on_unmapped"] == "bubble" and not policy["bubble_network"]:
        raise PolicyError("on_unmapped=bubble requires bubble_network")
    if policy["attach_mode"] not in ("detached", "bubble", "live"):
        raise PolicyError("attach_mode must be detached, bubble or live")
    if policy["attach_mode"] == "bubble" and not policy["bubble_network"]:
        raise PolicyError("attach_mode=bubble requires bubble_network")
    floor = policy["synthesize"].get("require_confidence")
    if floor not in CONFIDENCE_RANK and floor != "any":
        raise PolicyError("require_confidence must be 'any' or one of %s"
                          % ", ".join(CONFIDENCE_RANK))
    return policy


# --------------------------------------------------------------- inventory
def index_inventory(networks):
    """Index existing VergeOS vnets for lookup, tolerating key spellings."""
    by_name, by_vlan = {}, {}
    entries = []
    for net in networks or []:
        name = net.get("name") or ""
        vlan = net.get("vlan_id", net.get("vlan", net.get("vlanid")))
        try:
            vlan = int(vlan)
        except (TypeError, ValueError):
            vlan = None
        entry = {"name": name, "vlan": vlan,
                 "type": net.get("network_type") or net.get("type"),
                 "network": net.get("network"),
                 "raw": net}
        entries.append(entry)
        by_name.setdefault(normalise(name), []).append(entry)
        if vlan is not None:
            by_vlan.setdefault(vlan, []).append(entry)
    return {"entries": entries, "by_name": by_name, "by_vlan": by_vlan}


# --------------------------------------------------------------- resolvers
def _hit(resolver, target, why):
    return {"resolver": resolver, "target": target, "why": why}


def _ambiguous(resolver, candidates, why):
    return {"resolver": resolver, "ambiguous": [c["name"] for c in candidates],
            "why": why}


def r_explicit(nic, facts, inv, policy):
    table = policy.get("explicit") or {}
    for key in (nic.get("segment"), nic.get("bridge")):
        if key and key in table:
            return _hit("explicit", table[key],
                        "operator table maps %s" % key)
    return None


def r_alias(nic, facts, inv, policy):
    alias = (facts or {}).get("alias")
    if not alias:
        return None
    cands = inv["by_name"].get(normalise(alias), [])
    if len(cands) == 1:
        return _hit("alias", cands[0]["name"],
                    "source SDN alias %r matches an existing vnet" % alias)
    if len(cands) > 1:
        return _ambiguous("alias", cands, "alias %r matches %d vnets"
                          % (alias, len(cands)))
    return None


def r_vlan(nic, facts, inv, policy):
    vlan = nic.get("tag")
    if vlan is None:
        vlan = (facts or {}).get("tag")
    if vlan is None:
        return None
    if nic.get("trunks"):
        return None
    cands = inv["by_vlan"].get(vlan, [])
    if len(cands) == 1:
        return _hit("vlan", cands[0]["name"],
                    "VLAN %d matches an existing vnet" % vlan)
    if len(cands) > 1:
        return _ambiguous("vlan", cands,
                          "VLAN %d matches %d vnets" % (vlan, len(cands)))
    return None


def r_name(nic, facts, inv, policy):
    for key, label in ((nic.get("segment"), "segment"),
                       ((facts or {}).get("sdn_vnet"), "SDN vnet"),
                       (nic.get("bridge"), "bridge")):
        if not key:
            continue
        cands = inv["by_name"].get(normalise(key), [])
        if len(cands) == 1:
            return _hit("name", cands[0]["name"],
                        "%s name %r matches an existing vnet" % (label, key))
        if len(cands) > 1:
            return _ambiguous("name", cands, "%s %r matches %d vnets"
                              % (label, key, len(cands)))
    return None


def r_synthesize(nic, facts, inv, policy):
    cfg = policy["synthesize"]
    facts = facts or {}
    floor = cfg.get("require_confidence", "authoritative")
    have = facts.get("confidence")

    if nic.get("trunks") and not cfg.get("allow_trunk"):
        return {"resolver": "synthesize", "blocked": True,
                "why": "segment is a VLAN trunk carrying %s; a single vnet "
                       "cannot represent it" % nic["trunks"]}
    if not facts:
        return {"resolver": "synthesize", "blocked": True,
                "why": "no host topology facts for segment %r -- nothing to "
                       "build from" % nic.get("segment")}
    if floor != "any":
        if have is None or CONFIDENCE_RANK.get(have, -1) < CONFIDENCE_RANK[floor]:
            return {"resolver": "synthesize", "blocked": True,
                    "why": "topology confidence %r is below the required %r; "
                           "synthesising would invent a subnet"
                           % (have, floor)}

    name = cfg.get("name_template", "{segment}").format(
        segment=nic.get("segment") or "",
        bridge=nic.get("bridge") or "",
        vlan=nic.get("tag") if nic.get("tag") is not None
        else (facts.get("tag") or ""),
        alias=facts.get("alias") or "",
        vnet=facts.get("sdn_vnet") or "")
    name = name.strip("-. ")
    if not name:
        return {"resolver": "synthesize", "blocked": True,
                "why": "name_template produced an empty name"}

    spec = {"name": name, "state": "present"}
    vlan = nic.get("tag") if nic.get("tag") is not None else facts.get("tag")
    ntype = cfg.get("network_type", "internal")
    if ntype not in VNET_TYPES:
        return {"resolver": "synthesize", "blocked": True,
                "why": "network_type %r is not a VergeOS vnet type (%s); "
                       "VLANs are expressed with layer2_type, not a type"
                       % (ntype, ", ".join(VNET_TYPES))}
    spec["network_type"] = ntype
    if vlan is not None:
        spec["layer2_type"] = "vlan"
        spec["vlan_id"] = vlan
    if facts.get("mtu"):
        spec["mtu"] = facts["mtu"]

    cidr = facts.get("cidr")
    if cidr:
        spec["network"] = cidr

    # The source subnet's gateway becomes the vnet's OWN address, not its
    # `gateway`. In Proxmox SDN, `gateway 10.0.20.1` is the address the SDN
    # router holds on that subnet; in VergeOS the vnet IS that router, so
    # the equivalent field is ip_address.
    #
    # Measured on the API, rather than assumed. `gateway` is validated
    # against `network`, and BOTH `network` and `ipaddress` must be present
    # for it to validate at all:
    #
    #   network + gateway, no ipaddress   -> Gateway is outside of network
    #   ipaddress + gateway, no network   -> Gateway is outside of network
    #   ipaddress + network + gw inside   -> accepted
    #   ipaddress + network + gw outside  -> Gateway is outside of network
    #   ipaddress outside network         -> IP address is outside of the network
    #
    # `gateway` on a VergeOS vnet means its UPSTREAM router, which a source
    # subnet definition simply does not describe. It is therefore emitted
    # only when the operator supplies one.
    if cidr and facts.get("gateway"):
        spec["ip_address"] = facts["gateway"]
    upstream = cfg.get("upstream_gateway")
    if upstream and cidr:
        spec["gateway"] = upstream
    dhcp_mode = cfg.get("dhcp", "inherit")
    dhcp = facts.get("dhcp")
    if dhcp_mode == "on" or (dhcp_mode == "inherit" and dhcp):
        spec["dhcp_enabled"] = True
        if dhcp:
            # The module takes dhcp_end and translates it to the API's
            # dhcp_stop; sending dhcp_end straight at the API is silently
            # ignored, which yields a DHCP range with no upper bound.
            spec["dhcp_start"] = dhcp.get("start")
            spec["dhcp_end"] = dhcp.get("end")
    elif dhcp_mode == "off":
        spec["dhcp_enabled"] = False

    if cidr:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
            for field in ("ip_address", "gateway", "dhcp_start", "dhcp_end"):
                value = spec.get(field)
                if value and ipaddress.ip_address(value) not in net:
                    return {"resolver": "synthesize", "blocked": True,
                            "why": "%s %s is outside %s; the source topology "
                                   "is inconsistent" % (field, value, cidr)}
        except ValueError as exc:
            return {"resolver": "synthesize", "blocked": True,
                    "why": "unusable address in topology: %s" % exc}

    # A vnet's uplink is `interface_vnet`, and the collection's network
    # module does not expose it -- confirmed on the API, where a vnet
    # created through the module comes back with interface_vnet=null. So a
    # synthesised VLAN vnet carries the right tag onto NOTHING: guests land
    # in an isolated layer 2, not on VLAN N of the physical fabric. The
    # spec is still correct as far as it goes, but saying it reproduces the
    # source network would be false, so it is marked.
    spec_note = ("vnet will have NO uplink (interface_vnet is not settable "
                 "through the collection); guests land in an isolated "
                 "layer 2 until an uplink is attached by hand")
    return _hit("synthesize", name,
                "built from %s facts (confidence %s)"
                % (facts.get("source"), have)) | {"create": spec,
                                                  "isolated": True,
                                                  "note": spec_note}


RESOLVER_FUNCS = {"explicit": r_explicit, "alias": r_alias, "vlan": r_vlan,
                  "name": r_name, "synthesize": r_synthesize}


# ------------------------------------------------------------------ plan
def plan(vms, segments, inventory, policy=None):
    """Produce the mapping plan. `vms` is {vm_name: [nic, ...]}."""
    policy = merge_policy(policy)
    inv = index_inventory(inventory)
    segments = segments or {}

    decisions = []
    creates = {}
    for vm_name in sorted(vms):
        for nic in vms[vm_name]:
            decisions.append(
                _decide(vm_name, nic, segments, inv, policy, creates))

    refusals = [d for d in decisions if d["action"] == "refuse"]
    warnings = []
    isolated = sorted({d["target"] for d in decisions if d.get("isolated")})
    if isolated:
        warnings.append(
            "%d synthesised vnet(s) will have no uplink and cannot carry "
            "their VLAN onto the physical fabric: %s. The collection's "
            "network module cannot set interface_vnet; attach an uplink in "
            "the UI, or map to an existing vnet instead of synthesising."
            % (len(isolated), ", ".join(isolated)))
    return {
        "policy": policy,
        "decisions": decisions,
        "creates": [creates[k] for k in sorted(creates)],
        "refusals": refusals,
        "warnings": warnings,
        "ok": not refusals,
        "summary": _summarise(decisions, creates),
    }


def _decide(vm_name, nic, segments, inv, policy, creates):
    base = {"vm": vm_name, "nic": nic.get("key"), "mac": nic.get("mac"),
            "model": nic.get("model"), "segment": nic.get("segment"),
            "tried": []}

    if not nic.get("connected"):
        # Deliberately isolated on the source. Honouring that is the whole
        # reason the parser stopped defaulting to vmbr0.
        return dict(base, action="detach", target=None, resolver="none",
                    why="source NIC has no bridge; it is intentionally "
                        "unconnected and stays that way")

    facts = segments.get(nic.get("segment")) or {}
    for name in policy["order"]:
        result = RESOLVER_FUNCS[name](nic, facts, inv, policy)
        if result is None:
            base["tried"].append({"resolver": name, "result": "abstained"})
            continue
        if result.get("ambiguous"):
            base["tried"].append({"resolver": name, "result": "ambiguous",
                                  "candidates": result["ambiguous"]})
            return dict(base, action="refuse", target=None, resolver=name,
                        why="%s -- refusing rather than guessing between %s"
                            % (result["why"], ", ".join(result["ambiguous"])))
        if result.get("blocked"):
            base["tried"].append({"resolver": name, "result": "blocked",
                                  "why": result["why"]})
            continue
        spec = result.get("create")
        if spec:
            existing = creates.get(spec["name"])
            if existing and existing != spec:
                return dict(base, action="refuse", target=None,
                            resolver=name,
                            why="two segments would both create vnet %r with "
                                "different settings" % spec["name"])
            creates[spec["name"]] = spec
        base["tried"].append({"resolver": name, "result": "matched"})
        decided = dict(base, action="create" if spec else "map",
                       target=result["target"], resolver=name,
                       why=result["why"],
                       confidence=facts.get("confidence"))
        if result.get("isolated"):
            decided["isolated"] = True
            decided["note"] = result["note"]
        return decided

    fallback = policy["on_unmapped"]
    if fallback == "detach":
        return dict(base, action="detach", target=None, resolver="fallback",
                    why="no resolver matched; policy on_unmapped=detach")
    if fallback == "bubble":
        return dict(base, action="bubble",
                    target=policy["bubble_network"], resolver="fallback",
                    why="no resolver matched; policy on_unmapped=bubble")
    return dict(base, action="refuse", target=None, resolver="fallback",
                why="no resolver matched segment %r and on_unmapped=refuse"
                    % nic.get("segment"))


def _summarise(decisions, creates):
    counts = {}
    for d in decisions:
        counts[d["action"]] = counts.get(d["action"], 0) + 1
    return {"nics": len(decisions), "by_action": counts,
            "vnets_to_create": len(creates)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vms", required=True,
                    help="JSON: {vm_name: [nic, ...]} from the connector")
    ap.add_argument("--segments", help="JSON from pve_topology.py "
                                       "(the 'segments' object)")
    ap.add_argument("--inventory", help="JSON list of existing VergeOS vnets")
    ap.add_argument("--policy", help="JSON policy document")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero when any NIC is refused")
    args = ap.parse_args()

    def load(path, default):
        if not path:
            return default
        with open(path) as fh:
            return json.load(fh)

    try:
        result = plan(load(args.vms, {}), load(args.segments, {}),
                      load(args.inventory, []), load(args.policy, {}))
    except PolicyError as exc:
        print("FATAL: %s" % exc, file=sys.stderr)
        return 2

    json.dump(result, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    if args.strict and not result["ok"]:
        print("REFUSED: %d NIC(s) could not be mapped"
              % len(result["refusals"]), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
