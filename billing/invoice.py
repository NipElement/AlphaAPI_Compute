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
Rates are integer micro-dollars; proration uses exact fractions. Displayed
lines round half-up to cents and TOTAL sums the displayed line amounts.

What it prices, and the rules that were found the hard way (review 2026-08-27):
  - gpu-hour.*   from ledger intervals — CLOSED ones and OPEN ones alike.
                 Rounding is per statement window: an interval that spans a
                 month boundary is clipped into both windows and each part
                 rounds up to the granularity, so the two statements together
                 bill one extra minute (measured 2026-08-30: $0.16/GPU, at
                 most ~12 times a year for a pod that never restarts). It is
                 disclosed in docs/customer/quickstart.md rather than carried
                 as remainder state across statements.
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
import hmac
import json
import os
from fractions import Fraction
from pathlib import Path
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
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
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
    if not skus or currency != "USD":
        raise SystemExit("pricebook must contain USD rates")
    seen = set()
    for s in skus:
        for need in ("sku", "unit", "unit_price_micros", "effective_from", "tenant_kinds"):
            if need not in s:
                raise SystemExit(f"pricebook: sku entry missing {need}: {s}")
        if (s["unit"] not in ("gpu-hour", "node-month", "gib-month") or
                s["unit_price_micros"] < 0 or not s["tenant_kinds"] or
                s.get("bill_granularity_seconds", 60) <= 0):
            raise SystemExit(f"pricebook: invalid unit, negative rate or invalid granularity: {s['sku']}")
        parse_ts(s["effective_from"])
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


def load_ledger(path: str, chain_key: bytes = b""):
    """All records + chain verdict. An explicitly empty file is valid; a
    missing ledger is a data-loss error and must never become a zero invoice.

    The chain MODE is an input, never read from the file: an auditor who knows
    the ledger is keyed passes the key, so a downgrade (records rewritten with
    a plain sha256 chain) fails verification instead of passing it."""
    recs, ok, broken, prev = [], True, None, GENESIS
    if not os.path.isfile(path):
        raise ValueError("ledger file is missing; refusing to interpret lost data as zero usage")
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        body = prev.encode() + _canonical(r)
        want = (hmac.new(chain_key, body, hashlib.sha256).hexdigest() if chain_key
                else hashlib.sha256(body).hexdigest())
        if ok and (r.get("seq") != len(recs) + 1 or
                   r.get("prev") != prev or r.get("hash") != want):
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
    for raw in Path(tenants_path).read_text(encoding="utf-8").splitlines():
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
    exact = (Decimal(m.numerator) / Decimal(m.denominator)
             if isinstance(m, Fraction) else Decimal(m))
    return str((exact / Decimal(1_000_000)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


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
    ap.add_argument("--chain-key-file", default=None,
                    help="verify the chain as HMAC-SHA256 under this key (the "
                         "metering Secret). Without it the chain is plain "
                         "sha256 — which anyone who can write the file can forge.")
    ap.add_argument("--expect-head", default=None,
                    help="the chain head anchored OUTSIDE the ledger (the "
                         "arise_metering_ledger_head_info series Prometheus "
                         "kept, or the value recorded at the last statement). "
                         "A rewritten history has a different head: exit 3.")
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

    chain_key = b""
    if args.chain_key_file:
        with open(args.chain_key_file, "rb") as fh:
            chain_key = fh.read().strip()
        if not chain_key:
            raise SystemExit(f"{args.chain_key_file} is empty — refusing to fall back to an unkeyed chain")
    try:
        recs, chain_ok, broken, head = load_ledger(args.ledger, chain_key)
    except (ValueError, OSError, TypeError, AttributeError) as exc:
        print(f"LEDGER UNREADABLE: {exc}; refusing to bill", file=sys.stderr)
        return 3
    if not chain_ok and not args.allow_broken:
        print(f"LEDGER CHAIN BROKEN at record {broken}; refusing to bill from it "
              f"(--allow-broken to override, every line is then marked)", file=sys.stderr)
        return 3
    if args.expect_seq is not None and len(recs) < args.expect_seq:
        print(f"LEDGER SHORTER than the meter's anchor: {len(recs)} records < seq {args.expect_seq} "
              f"— the tail was truncated", file=sys.stderr)
        return 3
    if args.expect_head:
        # The anchor check. The head is the hash of the LAST record, so this
        # pins the ENTIRE history: a rewrite anywhere changes it. When
        # --expect-seq is also given, the anchored head must match the record
        # at that seq (the statement's own end-of-period anchor).
        if args.expect_seq is not None:
            at = [r for r in recs if r.get("seq") == args.expect_seq]
            got = at[0].get("hash") if at else None
            where = f"record seq {args.expect_seq}"
        else:
            got = head
            where = "the ledger head"
        if got != args.expect_head:
            print(f"ANCHOR MISMATCH: {where} is {got or 'absent'}, the anchor says "
                  f"{args.expect_head}. The history was rewritten, truncated or "
                  f"replaced — do not invoice from it (runbooks/incident-metering.md).",
                  file=sys.stderr)
            return 3
    warn = "" if chain_ok else f"CHAIN BROKEN@{broken} "

    ded_nodes = {n.strip() for n in args.dedicated_nodes.split(",") if n.strip()}
    d0 = parse_ts(args.dedicated_from) if args.dedicated_from else w0
    d1 = parse_ts(args.dedicated_to) if args.dedicated_to else w1
    d0, d1 = max(d0, w0), min(d1, w1)

    gpu_rates = rates_for(book, args.gpu_sku, kind)
    node_rates = rates_for(book, args.node_sku, kind)
    storage_rates = rates_for(book, args.storage_sku, kind)

    class StatementWriter:
        # Round each displayed line once. TOTAL must add up to the amounts the
        # customer actually sees, including multiple sub-cent allocations.
        def __init__(self):
            self.writer = csv.writer(sys.stdout, lineterminator="\n")
            self.total = Decimal("0.00")
        def writerow(self, row):
            if row[0] == "TOTAL":
                row[9] = str(self.total)
            elif row[0] != "tenant" and row[9] != "":
                self.total += Decimal(row[9])
            self.writer.writerow(row)
    w = StatementWriter()
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
        open_note = "" if closed else "OPEN at statement time — billed to window end; "
        # A pod can overlap only PART of a dedicated reservation. Split at
        # both reservation boundaries before pricing so that covered seconds
        # never acquire a GPU-hour charge as well as the node-month charge.
        cuts = sorted({a, b} | ({v for v in (d0, d1) if a < v < b} if node in ded_nodes else set()))
        for left, right in zip(cuts, cuts[1:]):
            if node in ded_nodes and d0 <= left and right <= d1:
                w.writerow([args.tenant, args.gpu_sku, pod, uid, fmt_ts(left), fmt_ts(right),
                            right - left, gpu, "", "0.00",
                            f"{warn}{open_note}covered by {args.node_sku} on {node}"])
                continue
            for x, y, rate in segments(left, right, gpu_rates):
                if rate is None:
                    unpriced += 1
                    w.writerow([args.tenant, args.gpu_sku, pod, uid, fmt_ts(x), fmt_ts(y), y - x, gpu,
                                "", "", f"{warn}NOT PRICED: no rate effective at segment start"])
                    continue
                gran = rate.get("bill_granularity_seconds", 60)
                billed = ((y - x + gran - 1) // gran) * gran
                amount = Fraction(billed * gpu * rate["unit_price_micros"], 3600)
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
        for x, y, rate in segments(a, b, storage_rates):
            if rate is None:
                unpriced += 1
                w.writerow([args.tenant, args.storage_sku, name, uid, fmt_ts(x), fmt_ts(y), y - x, "",
                            "", "", f"{warn}NOT PRICED: no storage rate at segment start"])
                continue
            gib_months = sum((Fraction(gib * (de - ds), dim * 86400)
                              for ds, de, dim in day_iter(x, y)), Fraction())
            amount = rate["unit_price_micros"] * gib_months
            total += amount
            open_note = "" if closed else "OPEN at statement time — to window end; "
            w.writerow([args.tenant, args.storage_sku, name, uid, fmt_ts(x), fmt_ts(y), y - x, "",
                        micros_to_dollars(rate["unit_price_micros"]), micros_to_dollars(amount),
                        f"{warn}{open_note}{gib} GiB {orec.get('storage_class','')} = {float(gib_months):.2f} GiB-month; "
                        f"{rate.get('note','')}".strip("; ")])

    if ded_nodes and d1 > d0:
        for node in sorted(ded_nodes):
            for x, y, rate in segments(d0, d1, node_rates):
                if rate is None:
                    unpriced += 1
                    w.writerow([args.tenant, args.node_sku, "", node, fmt_ts(x), fmt_ts(y), "", "", "", "",
                                f"{warn}NOT PRICED: no dedicated rate at segment start"])
                    continue
                amount = sum((Fraction(rate["unit_price_micros"] * (de - ds), dim * 86400)
                              for ds, de, dim in day_iter(x, y)), Fraction())
                total += amount
                days = round((y - x) / 86400, 2)
                w.writerow([args.tenant, args.node_sku, "", node, fmt_ts(x), fmt_ts(y), "", "",
                            micros_to_dollars(rate["unit_price_micros"]), micros_to_dollars(amount),
                            f"{warn}pro-rated {days} day(s), each at 1/days-in-its-month"])

    w.writerow(["TOTAL", "", "", "", args.start, args.end, "", "", "",
                micros_to_dollars(total),
                (f"{warn}{kind_note}; " + (f"{unpriced} unpriced line(s)" if unpriced else "all lines priced")).strip("; ")])
    return 2 if unpriced else 0


if __name__ == "__main__":
    sys.exit(main())
