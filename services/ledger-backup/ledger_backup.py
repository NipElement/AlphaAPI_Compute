#!/usr/bin/env python3
"""Copy the allocation ledger and PROVE the copy is usable.

Runs hourly from platform/overlays/dgx/ledger-backup.yaml. Three properties,
each of which has failed for somebody before:

  1. The copy is complete enough to load. The ledger is append-only JSONL and
     the writer may be mid-append, so at most ONE trailing fragment may fail
     to parse — exactly the tolerance metering's own loader has. Two bad lines
     is corruption, not a torn tail, and fails the Job.
  2. The copy does not SHRINK. An append-only file that got shorter means
     records were deleted or the PVC was replaced; backing that up on top of a
     good copy would launder the loss into the retention window. Compared
     against the newest previous .meta, never against a value this script
     computes twice.
  3. The copy is described. A .meta carries the sha256, the record count and
     the chain head, so a restore can be checked against the Prometheus anchor
     (arise_metering_ledger_head_info) WITHOUT the HMAC key — the key never
     comes near this job.

Exit non-zero on any of them. A backup that reports success without reading
what it wrote is the failure mode this exists to remove.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

LEDGER = os.environ.get("LEDGER_PATH", "/ledger/allocations.jsonl")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backups")
KEEP = int(os.environ.get("KEEP", "168"))
PREFIX = "ledger-"


def log(level, msg, **kw):
    rec = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "level": level, "component": "ledger-backup", "msg": msg}
    rec.update(kw)
    print(json.dumps(rec), flush=True)


def previous_count() -> int:
    """Records in the newest previous backup, or -1 if this is the first."""
    metas = sorted(f for f in os.listdir(BACKUP_DIR)
                   if f.startswith(PREFIX) and f.endswith(".meta"))
    if not metas:
        return -1
    try:
        with open(os.path.join(BACKUP_DIR, metas[-1]), encoding="utf-8") as fh:
            return int(json.load(fh)["records"])
    except (OSError, ValueError, KeyError) as exc:
        # A meta we cannot read is not permission to skip the shrink check.
        log("ERROR", "previous backup meta unreadable; refusing to compare",
            file=metas[-1], error_class=type(exc).__name__)
        sys.exit(3)


def main() -> int:
    if not os.path.exists(LEDGER):
        # Before the first allocation there is genuinely nothing to back up,
        # but say so loudly rather than exiting 0 in silence forever.
        log("WARN", "no ledger file yet; nothing to back up", path=LEDGER)
        return 0
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = os.path.join(BACKUP_DIR, f"{PREFIX}{stamp}.jsonl")

    with tempfile.NamedTemporaryFile(dir=BACKUP_DIR, prefix=".inflight-",
                                     delete=False) as tmp:
        inflight = tmp.name
    try:
        with open(LEDGER, "rb") as src, open(inflight, "wb") as out:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())

        # ---- read it back ------------------------------------------------
        sha = hashlib.sha256()
        records = 0
        torn = 0
        head = ""
        with open(inflight, "rb") as fh:
            raw = fh.read()
        sha.update(raw)
        lines = raw.decode("utf-8", "replace").split("\n")
        if lines and lines[-1] == "":
            lines.pop()          # a clean trailing newline is not a record
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                if i == len(lines) - 1:
                    torn = 1     # the writer was mid-append: tolerated once
                    continue
                log("ERROR", "ledger copy has a bad line that is not the tail; "
                             "this is corruption, not a torn append",
                    line_number=i + 1)
                return 4
            records += 1
            head = rec.get("hash", head)

        prev = previous_count()
        if prev >= 0 and records < prev:
            log("ERROR", "ledger SHRANK since the last backup — records were "
                         "deleted or the PVC was replaced. Refusing to rotate "
                         "this copy in on top of a good one.",
                records=records, previous=prev)
            return 5
        if records == 0 and prev <= 0:
            log("WARN", "ledger has no complete records yet", torn=torn)

        os.replace(inflight, dest)
        meta = {"file": os.path.basename(dest), "sha256": sha.hexdigest(),
                "records": records, "torn_tail": torn, "head": head,
                "bytes": len(raw), "taken_at": stamp}
        meta_path = dest[:-len(".jsonl")] + ".meta"
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        if os.path.exists(inflight):
            os.unlink(inflight)

    # ---- retention -------------------------------------------------------
    copies = sorted(f for f in os.listdir(BACKUP_DIR)
                    if f.startswith(PREFIX) and f.endswith(".jsonl"))
    for old in copies[:-KEEP] if len(copies) > KEEP else []:
        os.unlink(os.path.join(BACKUP_DIR, old))
        old_meta = os.path.join(BACKUP_DIR, old[:-len(".jsonl")] + ".meta")
        if os.path.exists(old_meta):
            os.unlink(old_meta)

    log("INFO", "ledger backed up and read back",
        file=os.path.basename(dest), records=records, torn_tail=torn,
        head=head[:16], kept=min(len(copies), KEEP))
    return 0


if __name__ == "__main__":
    sys.exit(main())
