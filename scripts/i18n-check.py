#!/usr/bin/env python3
"""i18n locale-parity gate (L0, static).

The console's i18n is now structural: vue-i18n looks up keyed messages at render
time (t('nav.devMachines')), so there is no post-render DOM replacement and none
of the old text-node/attribute/reversibility failure modes exist. The one thing
that can still go wrong is a key present in one locale but not the other — it
renders raw (English fallback shows the Chinese, or a key path leaks). This gate
fails when web/src/i18n/locales/zh.ts and en.ts drift out of lockstep.

Node loads the two locale modules (they are pure object literals) and dumps their
flattened key paths; Python diffs the sets.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOC = REPO / "web" / "src" / "i18n" / "locales"

NODE = r"""
const { readFileSync } = require('fs');
function load(f) {
  // strip the ES export so the pure object literal can be evaluated directly
  const s = readFileSync(f, 'utf8').replace(/export\s+default/, 'return');
  return (new Function(s))();
}
function flat(o, p, out) {
  for (const k of Object.keys(o)) {
    const v = o[k];
    const key = p ? p + '.' + k : k;
    if (v && typeof v === 'object') flat(v, key, out);
    else out.push(key);
  }
}
const zh = [], en = [];
flat(load(process.argv[1]), '', zh);
flat(load(process.argv[2]), '', en);
console.log(JSON.stringify({ zh: zh.sort(), en: en.sort() }));
"""


def main():
    zh, en = LOC / "zh.ts", LOC / "en.ts"
    if not zh.exists() or not en.exists():
        print(f"i18n-check: locale files missing under {LOC}")
        sys.exit(1)
    try:
        r = subprocess.run(["node", "-e", NODE, str(zh), str(en)],
                           capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        print("i18n-check: node not found (needed to parse the locale modules)")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("i18n-check: locale parse timed out")
        sys.exit(1)
    if r.returncode != 0:
        print("i18n-check: failed to parse locales:\n" + r.stderr[:600])
        sys.exit(1)
    d = json.loads(r.stdout)
    zset, eset = set(d["zh"]), set(d["en"])
    miss_en = sorted(zset - eset)
    miss_zh = sorted(eset - zset)
    if miss_en or miss_zh:
        print("i18n-check: FAIL — zh/en locale key sets are not in lockstep:")
        for k in miss_en:
            print(f"  present in zh, MISSING in en: {k}")
        for k in miss_zh:
            print(f"  present in en, MISSING in zh: {k}")
        sys.exit(1)
    print(f"i18n-check: PASS — zh/en locales in lockstep ({len(zset)} keys)")


if __name__ == "__main__":
    main()
