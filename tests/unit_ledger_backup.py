"""Unit tests for services/ledger-backup/ledger_backup.py.

The ledger is the only record of what customers owe, and it lived on one
node-local PVC with no copy until 2026-08-31. A backup job is only worth
having if it FAILS on the states that matter, so each of these drives one:
a torn tail (tolerated — the writer appends while we copy), a bad line that
is not the tail (corruption, refused), a ledger that shrank (records deleted
or the PVC replaced — refused rather than rotated in on top of a good copy),
and a previous .meta we cannot read (refused rather than skipping the
comparison silently).
"""
import importlib.util, json, os, sys, tempfile

def load(ledger, backups, keep="168"):
    os.environ.update(LEDGER_PATH=ledger, BACKUP_DIR=backups, KEEP=keep)
    spec = importlib.util.spec_from_file_location(
        "lb", "services/ledger-backup/ledger_backup.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

def run(lines, backups=None, keep="168"):
    d = backups or tempfile.mkdtemp(prefix="lbk-")
    led = os.path.join(tempfile.mkdtemp(prefix="lgr-"), "allocations.jsonl")
    open(led, "w").write(lines)
    return load(led, d, keep).main(), d

FAILS = []
def check(name, got, want):
    if got == want: print(f"  ok   {name}")
    else: FAILS.append(name); print(f"  FAIL {name}: exit {got}, expected {want}")

good = "".join(json.dumps({"hash": f"h{i}", "n": i}) + "\n" for i in range(5))

rc, d = run(good); check("a clean ledger backs up", rc, 0)
meta = json.load(open(os.path.join(d, sorted(f for f in os.listdir(d) if f.endswith('.meta'))[-1])))
check("meta records the count", meta["records"], 5)
check("meta records the head", meta["head"], "h4")

rc, _ = run(good + '{"hash":"h5","n":5', keep="168")
check("a torn TAIL is tolerated (the writer was mid-append)", rc, 0)

rc, _ = run('{"bad"\n' + good)
check("a bad line that is NOT the tail fails", rc, 4)

# shrink: back up 5 records, then hand it a 3-record ledger
d2 = tempfile.mkdtemp(prefix="lbk2-")
run(good, backups=d2)
rc, _ = run("".join(json.dumps({"hash": f"h{i}"}) + "\n" for i in range(3)), backups=d2)
check("a SHRUNK ledger is refused (records deleted / PVC replaced)", rc, 5)

# an unreadable previous meta must not be silently skipped
d3 = tempfile.mkdtemp(prefix="lbk3-")
run(good, backups=d3)
open(os.path.join(d3, sorted(f for f in os.listdir(d3) if f.endswith('.meta'))[-1]), "w").write("{]")
try:
    rc, _ = run(good, backups=d3); rc_ = rc
except SystemExit as e:
    rc_ = e.code
check("an unreadable previous meta stops the job", rc_, 3)

# retention
d4 = tempfile.mkdtemp(prefix="lbk4-")
for _ in range(4):
    import time as _t; _t.sleep(1.05)
    run(good, backups=d4, keep="2")
kept = len([f for f in os.listdir(d4) if f.endswith(".jsonl")])
check("retention keeps KEEP copies", kept, 2)
check("retention prunes the orphan metas too",
      len([f for f in os.listdir(d4) if f.endswith(".meta")]), 2)

# no ledger at all
rc = load("/nonexistent/allocations.jsonl", tempfile.mkdtemp()).main()
check("no ledger yet is a WARN, not a crash", rc, 0)

print(f"FAIL: {len(FAILS)}/9" if FAILS else "PASS: 9/9")
sys.exit(1 if FAILS else 0)
