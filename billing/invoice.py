#!/usr/bin/env python3
"""
invoice.py — deterministic statement from the allocation ledger + price book.

    python3 billing/invoice.py --ledger allocations.jsonl \
        --pricebook billing/pricebook.yaml --tenants platform/tenants.yaml \
        --tenant tenant-direct --from 2026-09-01T00:00:00Z --to 2026-10-01T00:00:00Z \
        [--dedicated-nodes dgx03,dgx04 --dedicated-from ... --dedicated-to ...] \
        [--tenant-kind customer|internal] [--allow-broken] > statement.csv

Deterministic: same ledger + same book + same window => byte-identical CSV.
That is the property a dispute needs, and the reason nothing here reads the
clock or the local timezone (all timestamps are UTC via calendar.timegm; the
first cut used time.mktime and produced different statements per host).
Money is integer micro-dollars throughout; the CSV shows dollars only in the
final column, rounded once, half-up.

What it prices, and the rules that were found the hard way (review 2026-08-27):
  - gpu-hour.*   from ledger intervals — CLOSED ones and OPEN ones alike.
                 An open interval is billed up to the window end and marked
                 "open at statement time"; the next window clips at its start,
                 so nothing double-counts. (Unbilled-until-deleted dev
                 machines were a month of zero revenue.)
  - dedicated    a DIRECT customer pays the node-month for their reserved
                 node(s); GPU-hour lines whose `node` is inside the
                 reservation span are listed at $0 with a note — never charged
                 twice for the same hardware.
  - pro-rating   per DAY, each day at 1/days-in-ITS-calendar-month of the
                 monthly price; the dedicated span is clipped to the window.
  - price        selected per SEGMENT: an interval is split at every
                 effective_from boundary inside it, so a rate change applies
                 from the moment it takes effect, not from the next pod.
  - unpriced     an interval with no effective rate is NOT PRICED and the
                 process exits 2 — it never invents a rate.
  - chain        the ledger's hash chain is verified first; a broken chain
                 exits 3 unless --allow-broken (then every line carries a
                 warning). A truncated TAIL is undetectable from the file
                 alone — the meter exports its seq/head as a metric so a
                 regression is visible; pass --expect-seq from that anchor.
  - tenant kind  resolved from platform/tenants.yaml (--tenants), never
                 guessed; --tenant-kind is an explicit override echoed in
                 the statement.

Standard library only. No network.
"""
import argparse
import calendar
import csv
import hashlib
import json
import math
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

FMT = "%Y-%m-%dT%H:%M:%SZ"
GENESIS = "0" * 64


def parse_ts(s: str) -> int:
    import time
    return int(calendar.timegm(time.strptime(s, FMT)))


def fmt_ts(epoch: int) -> str:
    import time
    return time.strftime(FMT, time.gmtime(epoch))


# ------------------------------------------------------------ price book ---
def load_pricebook(path: str) -> dict:
    """Minimal reader for the price book's fixed shape (stdlib only). A
    malformed book must FAIL, never price at zero."""
    skus, cur, currency = [], None, "USD"
    for raw in open(path, encoding="utf-8"):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("currency:"):
            currency = line.split(":", 1)[1].strip(); continue
        if line.strip().startswith("- sku:"):
            cur = {"sku": line.split(":", 1)[1].strip()}; skus.append(cur); continue
        if cur is not None and line.startswith("    ") and ":" in line:
            k, v = line.strip().split(":", 1)
            v = v.strip().strip('"')
            if k == "tenant_kinds":
                v = [x.strip() for x in v.strip("[]").split(",") if x.strip()]
            elif k in ("unit_price_micros", "bill_granularity_seconds"):
                v = int(v)
            cur[k] = v
    seen = set()
    for s in skus:
        for need in ("sku", "unit", "unit_price_micros", "effective_from", "tenant_kinds"):
            if need not in s:
                raise SystemExit(f"pricebook: sku entry missing {need}: {s}")
        for kind in s["tenant_kinds"]:
            key = (s["sku"], kind, s["effective_from"])
            if key in seen:
                raise SystemExit(f"pricebook: two entries tie on {key}; one entry per "
                                 f"(sku, kind, effective_from) is the rule")
            seen.add(key)
    return {"currency": currency, "skus": skus}


def rates_for(book: dict, sku: str, kind: str):
    """[(effective_epoch, entry)] ascending, for this sku+kind."""
    out = [(parse_ts(s["effective_from"]), s) for s in book["skus"]
           if s["sku"] == sku and kind in s["tenant_kinds"]]
    return sorted(out, key=lambda x: x[0])


def segments(a: int, b: int, rates):
    """Split [a,b) at every effective_from inside it; yield (a, b, entry|None)."""
    cuts = [a] + [e for e, _ in rates if a < e < b] + [b]
    for x, y in zip(cuts, cuts[1:]):
        cur = None
        for e, s in rates:
            if e <= x:
                cur = s
        yield x, y, cur


# ---------------------------------------------------------------- ledger ---
def _canonical(rec: dict) -> bytes:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def load_ledger(path: str):
    """All records + chain verdict. Missing ledger = empty ledger (a
    dedicated-only customer has nothing in it and still gets a statement)."""
    recs, ok, broken, prev = [], True, None, GENESIS
    if not os.path.exists(path):
        return recs, ok, broken, prev
    for n, line in enumerate(open(path, encoding="utf-8"), 1):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        want = hashlib.sha256(prev.encode() + _canonical(r)).hexdigest()
        if ok and (r.get("prev") != prev or r.get("hash") != want):
            ok, broken = False, n
        prev = r.get("hash", want)
        recs.append(r)
    return recs, ok, broken, prev


def intervals_for(recs, tenant):
    """[(uid, pod, node, gpu, opened_at, closed_at|None, open_record)] in ledger order."""
    opened, out = {}, []
    for r in recs:
        if r.get("tenant") != tenant:
            continue
        if r["event"] == "open":
            opened[r["pod_uid"]] = r
        elif r["event"] == "close":
            o = opened.pop(r["pod_uid"], None)
            out.append((r["pod_uid"], r["pod"], r.get("node", ""), r["gpu"],
                        r.get("opened_at") or (o or {}).get("at"), r["at"], o or r))
    for uid, o in opened.items():
        out.append((uid, o["pod"], o.get("node", ""), o["gpu"], o["at"], None, o))
    return out


def load_tenant_kind(tenants_path: str, tenant: str):
    """`kind:` for this tenant from platform/tenants.yaml (stdlib, no PyYAML).
    The register is `spec.tenants: [ {namespace:, kind:, ...}, ... ]`; a list
    item starts at `- namespace:` and its scalar keys follow at a deeper
    indent. Returns None when the tenant is absent — the caller REFUSES to
    guess a rate card in that case."""
    if not tenants_path or not os.path.exists(tenants_path):
        return None
    cur = None
    for raw in open(tenants_path, encoding="utf-8"):
        line = raw.split("#", 1)[0].rstrip()
        s = line.strip()
        if not s:
            continue
        if s.startswith("- "):
            s = s[2:].strip()
            cur = s.split(":", 1)[1].strip() if s.startswith("namespace:") else None
            continue
        if cur == tenant and s.startswith("kind:"):
            return s.split(":", 1)[1].strip()
    return None


def micros_to_dollars(m: int) -> str:
    return str((Decimal(m) / Decimal(1_000_000)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def day_iter(a: int, b: int):
    """Yield (day_start, day_end, days_in_that_month) for each UTC day
    overlapping [a,b), clipped."""
    import time
    d = a - (a % 86400)
    while d < b:
        y, m = time.gmtime(d).tm_year, time.gmtime(d).tm_mon
        yield max(d, a), min(d + 86400, b), calendar.monthrange(y, m)[1]
        d += 86400


# ------------------------------------------------------------------ main ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--pricebook", required=True)
    ap.add_argument("--tenants", default="platform/tenants.yaml",
                    help="tenant register (kind: customer|internal)")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--tenant-kind", default=None,
                    help="OVERRIDE the register's kind (echoed in the statement)")
    ap.add_argument("--dedicated-nodes", default="",
                    help="comma-separated node names reserved for this tenant (from NodeOwnership)")
    ap.add_argument("--dedicated-from", default=None, help="reservation start (ISO Z); default window start")
    ap.add_argument("--dedicated-to", default=None, help="reservation end (ISO Z); default window end")
    ap.add_argument("--gpu-sku", default="gpu-hour.b300.on-demand")
    ap.add_argument("--storage-sku", default="storage-gib-month.included")
    ap.add_argument("--node-sku", default="node-month.dgx-b300.dedicated")
    ap.add_argument("--allow-broken", action="store_true")
    ap.add_argument("--expect-seq", type=int, default=None,
                    help="ledger seq from the meter's head anchor; fewer records => truncated tail")
    args = ap.parse_args()

    book = load_pricebook(args.pricebook)
    w0, w1 = parse_ts(args.start), parse_ts(args.end)
    if w1 <= w0:
        raise SystemExit("window must be non-empty")

    kind_reg = load_tenant_kind(args.tenants, args.tenant)
    kind = args.tenant_kind or kind_reg
    if kind is None:
        raise SystemExit(f"tenant kind unknown: {args.tenant} is not in {args.tenants} and "
                         f"--tenant-kind was not given — refusing to guess a rate card")
    kind_note = (f"kind={kind}" + (" (OVERRIDE of register " + str(kind_reg) + ")"
                                   if args.tenant_kind and args.tenant_kind != kind_reg else ""))

    recs, chain_ok, broken, head = load_ledger(args.ledger)
    if not chain_ok and not args.allow_broken:
        print(f"LEDGER CHAIN BROKEN at record {broken}; refusing to bill from it "
              f"(--allow-broken to override, every line is then marked)", file=sys.stderr)
        return 3
    if args.expect_seq is not None and len(recs) < args.expect_seq:
        print(f"LEDGER SHORTER than the meter's anchor: {len(recs)} records < seq {args.expect_seq} "
              f"— the tail was truncated", file=sys.stderr)
        return 3
    warn = "" if chain_ok else f"CHAIN BROKEN@{broken} "

    ded_nodes = {n.strip() for n in args.dedicated_nodes.split(",") if n.strip()}
    d0 = parse_ts(args.dedicated_from) if args.dedicated_from else w0
    d1 = parse_ts(args.dedicated_to) if args.dedicated_to else w1
    d0, d1 = max(d0, w0), min(d1, w1)

    gpu_rates = rates_for(book, args.gpu_sku, kind)
    node_rates = rates_for(book, args.node_sku, kind)
    storage_rates = rates_for(book, args.storage_sku, kind)

    w = csv.writer(sys.stdout, lineterminator="\n")
    w.writerow(["tenant", "sku", "pod", "pod_uid", "opened_at", "closed_at",
                "billed_seconds", "gpus", "unit_price_usd", "amount_usd", "note"])
    total, unpriced = 0, 0
    volumes = []
    for uid, pod, node, gpu, opened, closed, orec in sorted(intervals_for(recs, args.tenant),
                                                            key=lambda x: (x[4] or "", x[0])):
        if orec.get("kind") == "volume":
            volumes.append((uid, pod, opened, closed, orec))
            continue
        if gpu <= 0 or not opened:
            continue
        a = max(parse_ts(opened), w0)
        b = min(parse_ts(closed) if closed else w1, w1)
        if b <= a:
            continue
        covered = ded_nodes and node in ded_nodes and a >= d0 and b <= d1
        open_note = "" if closed else "OPEN at statement time — billed to window end; "
        if covered:
            w.writerow([args.tenant, args.gpu_sku, pod, uid, opened, closed or "", b - a, gpu,
                        "", "0.00", f"{warn}{open_note}covered by {args.node_sku} on {node}"])
            continue
        for x, y, rate in segments(a, b, gpu_rates):
            if rate is None:
                unpriced += 1
                w.writerow([args.tenant, args.gpu_sku, pod, uid, fmt_ts(x), fmt_ts(y), y - x, gpu,
                            "", "", f"{warn}NOT PRICED: no rate effective at segment start"])
                continue
            gran = int(rate.get("bill_granularity_seconds", 60))
            billed = math.ceil((y - x) / gran) * gran
            amount = billed * gpu * rate["unit_price_micros"] // 3600
            total += amount
            seg_note = "" if (x, y) == (a, b) else f"segment (rate effective {rate['effective_from']}); "
            w.writerow([args.tenant, args.gpu_sku, pod, uid, fmt_ts(x), fmt_ts(y), billed, gpu,
                        micros_to_dollars(rate["unit_price_micros"]), micros_to_dollars(amount),
                        f"{warn}{open_note}{seg_note}".strip("; ")])

    # Volumes: GiB-months, pro-rated per day like the node-month; a $0 rate is
    # still a line (what the tenant HOLDS), a missing rate is still unpriced.
    for uid, name, opened, closed, orec in volumes:
        if not opened:
            continue
        a = max(parse_ts(opened), w0)
        b = min(parse_ts(closed) if closed else w1, w1)
        if b <= a:
            continue
        gib = int(orec.get("storage_gib", 0))
        amount, gib_months = 0, 0.0
        rate = None
        for ds, de, dim in day_iter(a, b):
            rate = None
            for e, s in storage_rates:
                if e <= ds:
                    rate = s
            gib_months += gib * (de - ds) / (dim * 86400)
            if rate is not None:
                amount += rate["unit_price_micros"] * gib * (de - ds) // (dim * 86400)
        if rate is None and storage_rates == []:
            unpriced += 1
            w.writerow([args.tenant, args.storage_sku, name, uid, opened, closed or "", b - a, "",
                        "", "", f"{warn}NOT PRICED: no {args.storage_sku} rate for kind={kind}"])
            continue
        total += amount
        open_note = "" if closed else "OPEN at statement time — to window end; "
        w.writerow([args.tenant, args.storage_sku, name, uid, fmt_ts(a), fmt_ts(b), b - a, "",
                    micros_to_dollars(storage_rates[-1][1]["unit_price_micros"]), micros_to_dollars(amount),
                    f"{warn}{open_note}{gib} GiB {orec.get('storage_class','')} = {gib_months:.2f} GiB-month; "
                    f"{storage_rates[-1][1].get('note','')}".strip("; ")])

    if ded_nodes and d1 > d0:
        for node in sorted(ded_nodes):
            amount = 0
            for ds, de, dim in day_iter(d0, d1):
                rate = None
                for e, s in node_rates:
                    if e <= ds:
                        rate = s
                if rate is None:
                    unpriced += 1
                    continue
                amount += rate["unit_price_micros"] * (de - ds) // (dim * 86400)
            total += amount
            days = round((d1 - d0) / 86400, 2)
            w.writerow([args.tenant, args.node_sku, "", node, fmt_ts(d0), fmt_ts(d1), "", "",
                        micros_to_dollars(node_rates[-1][1]["unit_price_micros"]) if node_rates else "",
                        micros_to_dollars(amount),
                        f"{warn}pro-rated {days} day(s), each at 1/days-in-its-month"])

    w.writerow(["TOTAL", "", "", "", args.start, args.end, "", "", "",
                micros_to_dollars(total),
                (f"{warn}{kind_note}; " + (f"{unpriced} unpriced line(s)" if unpriced else "all lines priced")).strip("; ")])
    return 2 if unpriced else 0


if __name__ == "__main__":
    sys.exit(main())
