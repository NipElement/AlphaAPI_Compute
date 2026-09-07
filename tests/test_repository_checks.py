#!/usr/bin/env python3
"""The locale gate must read the shipped catalogs and reject malformed ones."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("i18n_check", REPO / "scripts/i18n-check.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class LocaleGateTests(unittest.TestCase):
    def catalogs(self, en, zh):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for lang, text in (("en", en), ("zh", zh)):
            (root / f"{lang}.json").write_text(text, encoding="utf-8")
        return root

    def test_actual_frontend_catalogs(self):
        self.assertGreater(gate.check(), 0)
        loader = (REPO / "web/src/i18n/index.ts").read_text()
        for lang in ("en", "zh"):
            self.assertIn(f"./locales/{lang}.json", loader)

    def test_nested_parity(self):
        self.assertEqual(gate.check(self.catalogs('{"nav":{"home":"Home"}}',
                                                 '{"nav":{"home":"首页"}}')), 1)

    def test_missing_key(self):
        with self.assertRaisesRegex(ValueError, "missing in zh: nav.jobs"):
            gate.check(self.catalogs('{"nav":{"home":"Home","jobs":"Jobs"}}',
                                     '{"nav":{"home":"首页"}}'))

    def test_duplicate_key(self):
        with self.assertRaisesRegex(ValueError, "duplicate translation key"):
            gate.check(self.catalogs('{"home":"Home","home":"Duplicate"}', '{"home":"首页"}'))

    def test_invalid_values(self):
        for invalid in (None, 1, [], "", {}, {"bad.key": "text"}):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                gate.check(self.catalogs(json.dumps({"home": invalid}), '{"home":"首页"}'))


if __name__ == "__main__":
    unittest.main()
