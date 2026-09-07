#!/usr/bin/env python3
"""Validate the actual JSON catalogs consumed by Vue, without Node or eval."""
import json
from pathlib import Path
import sys

LOC = Path(__file__).resolve().parent.parent / "web/src/i18n/locales"


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate translation key: {key}")
        result[key] = value
    return result


def flatten(value, prefix=""):
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{prefix or 'catalog'} must be a nonempty object")
    out = set()
    for key, text in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if not key or '.' in key:
            raise ValueError(f"invalid translation key: {path}")
        if isinstance(text, dict):
            out.update(flatten(text, path))
        elif isinstance(text, str) and text:
            out.add(path)
        else:
            raise ValueError(f"{path} must be nonempty text")
    return out


def check(directory=LOC):
    catalogs = {
        lang: flatten(json.loads((Path(directory) / f"{lang}.json").read_text(encoding="utf-8"),
                                object_pairs_hook=unique_keys))
        for lang in ("zh", "en")
    }
    if catalogs['zh'] != catalogs['en']:
        missing = [f"missing in {lang}: {', '.join(sorted(catalogs[other] - catalogs[lang]))}"
                   for lang, other in (("zh", "en"), ("en", "zh"))
                   if catalogs[other] - catalogs[lang]]
        raise ValueError("; ".join(missing))
    return len(catalogs['zh'])


def main():
    try:
        count = check()
    except (OSError, ValueError) as exc:
        print(f"i18n-check: FAIL — {exc}")
        return 1
    print(f"i18n-check: PASS — zh/en locales in lockstep ({count} keys)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
