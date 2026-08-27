#!/usr/bin/env python3
"""
invoice.py — deterministic statement from the allocation ledger + price book.

    python3 billing/invoice.py --ledger allocations.jsonl \
        --pricebook billing/pricebook.yaml --tenant tenant-direct \
        --from 2026-09-01T00:00:00Z --to 2026-10-01T00:00:00Z \
        [--tenant-kind customer] [--dedicated-days N] > statement.csv

Deterministic: same ledger + same book + same window => byte-identical CSV.
That is the property a dispute needs, and the reason nothing here reads the
clock. Money is integer micro-dollars throughout; the CSV shows dollars only
in the final column, rounded once, half-up.

What it prices:
  - gpu-hour.*   from CLOSED ledger intervals, clipped to the window, rounded
                 UP to the SKU's granularity (per-minute billing means a
                 61-second run is 2 minutes — the offer says "by the minute",
                 and that is the conventional reading).
  - node-month.* only if --dedicated-days is given (the DIRECT reservation
                 span is a NodeOwnership fact, not a ledger fact; the caller
                 supplies it until the RentalAgreement record exists).

Standard library only. No network.
"""
import argparse
import csv
import json
import math
import os
import sys
import time
from decimal import Decimal, ROUND_HALF_UP


def parse_ts(s: str) -> int:
    return int(time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone)


def load_pricebook(path: str) -> dict:
    """Minimal YAML reader for the price book's fixed shape (stdlib only).
    The book is a controlled file with one list of flat mappings; a full YAML
    parser is not worth a dependency here. A malformed book must FAIL, never
    price at zero — hence the strict shape checks."""
    skus, cur = [], None
    currency = "USD"
    for raw in open(path, encoding="utf-8"):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("currency:"):
            currency = line.split(":", 1)[1].strip()
            continue
        if line.strip().startswith("- sku:"):
            cur = {"sku": line.split(":", 1)[1].strip()}
            skus.append(cur)
            continue
        if cur is not None and line.startswith("    ") and ":" in line:
            k, v = line.strip().split(":", 1)
            v = v.strip().strip('"')
            if k == "tenant_kinds":
                v = [x.strip() for x in v.strip("[]").split(",") if x.strip()]
            elif k in ("unit_price_micros", "bill_granularity_seconds"):
                v = int(v)
            cur[k] = v
    for s in skus:
        for need in ("sku", "unit", "unit_price_micros", "effective_from", "tenant_kinds"):
            if need not in s:
                raise SystemExit(f"pricebook: sku entry missing {need}: {s}")
    return {"currency": currency, "skus": skus}


def pick_price(book: dict, sku: str, at_epoch: int, tenant_kind: str):
    """Newest entry for this sku+kind whose effective_from <= at. None if the
    sku was not priced yet at that instant (then the line is NOT billable,
    and the statement says so instead of inventing a rate)."""
    best = None
    for s in book["skus"]:
        if s["sku"] != sku or tenant_kind not in s["tenant_kinds"]:
            continue
        eff = parse_ts(s["effective_from"])
        if eff <= at_epoch and (best is None or eff > parse_ts(best["effective_from"])):
            best = s
    return best


def load_intervals(ledger_path: str, tenant: str):
    """A missing ledger is an EMPTY ledger, not an error: a dedicated-only
    customer with no on-demand pods has nothing in it, and their statement is
    still valid (the node-month line)."""
    opened, closed = {}, []
    if not os.path.exists(ledger_path):
        return closed, opened
    for line in open(ledger_path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("tenant") != tenant:
            continue
        if r["event"] == "open":
            opened[r["pod_uid"]] = r
        elif r["event"] == "close":
            closed.append(r)
    return closed, opened


def micros_to_dollars(m: int) -> str:
    return str((Decimal(m) / Decimal(1_000_000)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--pricebook", required=True)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--tenant-kind", default="customer")
    ap.add_argument("--dedicated-days", type=int, default=0,
                    help="days of DIRECT reservation inside the window (from NodeOwnership)")
    ap.add_argument("--gpu-sku", default="gpu-hour.b300.on-demand")
    ap.add_argument("--node-sku", default="node-month.dgx-b300.dedicated")
    args = ap.parse_args()

    book = load_pricebook(args.pricebook)
    w0, w1 = parse_ts(args.start), parse_ts(args.end)
    if w1 <= w0:
        raise SystemExit("window must be non-empty")
    closed, still_open = load_intervals(args.ledger, args.tenant)

    w = csv.writer(sys.stdout, lineterminator="\n")
    w.writerow(["tenant", "sku", "pod", "pod_uid", "opened_at", "closed_at",
                "billed_seconds", "gpus", "unit_price_usd", "amount_usd", "note"])
    total = 0
    unpriced = 0
    for r in sorted(closed, key=lambda x: (x["opened_at"], x["pod_uid"])):
        a, b = parse_ts(r["opened_at"]), parse_ts(r["at"])
        a, b = max(a, w0), min(b, w1)              # clip to the window
        if b <= a or r["gpu"] <= 0:
            continue
        price = pick_price(book, args.gpu_sku, a, args.tenant_kind)
        if price is None:
            unpriced += 1
            w.writerow([args.tenant, args.gpu_sku, r["pod"], r["pod_uid"],
                        r["opened_at"], r["at"], b - a, r["gpu"], "", "",
                        "NOT PRICED: no rate effective at interval start"])
            continue
        gran = int(price.get("bill_granularity_seconds", 60))
        billed = math.ceil((b - a) / gran) * gran
        amount = billed * r["gpu"] * price["unit_price_micros"] // 3600
        total += amount
        w.writerow([args.tenant, args.gpu_sku, r["pod"], r["pod_uid"],
                    r["opened_at"], r["at"], billed, r["gpu"],
                    micros_to_dollars(price["unit_price_micros"]),
                    micros_to_dollars(amount), ""])

    if args.dedicated_days:
        price = pick_price(book, args.node_sku, w0, args.tenant_kind)
        if price is None:
            unpriced += 1
            w.writerow([args.tenant, args.node_sku, "", "", args.start, args.end,
                        "", "", "", "", "NOT PRICED"])
        else:
            days_in_window = max(1, round((w1 - w0) / 86400))
            amount = price["unit_price_micros"] * args.dedicated_days // days_in_window
            total += amount
            w.writerow([args.tenant, args.node_sku, "", "", args.start, args.end,
                        "", "", micros_to_dollars(price["unit_price_micros"]),
                        micros_to_dollars(amount),
                        f"pro-rated {args.dedicated_days}/{days_in_window} days"])

    for uid, r in still_open.items():
        if uid not in {c["pod_uid"] for c in closed}:
            w.writerow([args.tenant, args.gpu_sku, r["pod"], uid, r["at"], "",
                        "", r["gpu"], "", "",
                        "STILL OPEN at statement time — bills next period"])

    w.writerow(["TOTAL", "", "", "", args.start, args.end, "", "", "",
                micros_to_dollars(total),
                f"{unpriced} unpriced line(s)" if unpriced else ""])
    return 2 if unpriced else 0


if __name__ == "__main__":
    sys.exit(main())
