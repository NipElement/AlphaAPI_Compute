#!/usr/bin/env python3
"""Fingerprint alert rules so a reload can be VERIFIED, not just reported.

`prometheus-reload.sh` used to compare rule-group NAMES. Editing an alert's
expression leaves every group name identical, so the reload reported
"4 rule groups LIVE" while Prometheus was still evaluating the old
expression — observed on the lab cluster 2026-08-31, when a repointed
NodeQuarantined stayed silent through a successful-looking reload.

Each alert is reduced to group/name plus a hash of everything that decides
whether it fires and who it wakes: the expression, the `for` window and the
severity label. Output is one space-free line per rule, so the shell can diff
two lists with plain sort/comm.

    prometheus-rule-fingerprint.py desired < <configmap .data as json>
    prometheus-rule-fingerprint.py live    < <GET /api/v1/rules>
"""
import hashlib
import json
import re
import sys

import yaml

_U = {"ms": 0.001, "s": 1, "m": 60, "h": 3600,
      "d": 86400, "w": 604800, "y": 31536000}


def secs(v) -> float:
    """"2m" (the manifest) and 120 (the rules API) are the same `for:`."""
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"(\d+)(ms|s|m|h|d|w|y)", str(v).strip())
    return float(m.group(1)) * _U[m.group(2)] if m else 0.0


def norm(expr) -> str:
    """Prometheus reprints an expression canonically — whitespace collapses
    and range durations are rewritten (24h -> 1d). Compare the meaning."""
    s = re.sub(r"\s+", "", str(expr))
    return re.sub(r"\[(\d+)(ms|s|m|h|d|w|y)\]",
                  lambda m: "[%gs]" % (int(m.group(1)) * _U[m.group(2)]), s)


def fp(expr, for_, severity) -> str:
    body = f"{norm(expr)}|{secs(for_)}|{severity}"
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def main() -> int:
    mode = sys.argv[1]
    doc = json.load(sys.stdin)
    out = []
    if mode == "desired":
        for value in doc.values():
            try:
                groups = (yaml.safe_load(value) or {}).get("groups", [])
            except yaml.YAMLError:
                continue
            for g in groups:
                out.append(f"group:{g['name']}")
                for r in g.get("rules", []):
                    if "alert" not in r:
                        continue
                    sev = (r.get("labels") or {}).get("severity", "-")
                    out.append(f"{g['name']}/{r['alert']}:"
                               f"{fp(r['expr'], r.get('for', 0), sev)}")
    elif mode == "live":
        for g in doc["data"]["groups"]:
            out.append(f"group:{g['name']}")
            for r in g["rules"]:
                if r.get("type") != "alerting":
                    continue
                sev = (r.get("labels") or {}).get("severity", "-")
                out.append(f"{g['name']}/{r['name']}:"
                           f"{fp(r['query'], r.get('duration', 0), sev)}")
    else:
        print(f"unknown mode {mode!r}", file=sys.stderr)
        return 2
    if not out:
        print("no rules found — refusing to call an empty comparison a match",
              file=sys.stderr)
        return 1
    print("\n".join(sorted(set(out))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
