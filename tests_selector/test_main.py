import io
import json
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import selector.main as M


def make_data(tmp: Path, n_patches=5):
    (tmp / "commits").mkdir()
    tests = [{"number": i, "id": f"tests/t.py::T::t{i}", "file": "tests/t.py",
              "seconds": s} for i, s in enumerate([28.817, 21.454, 6.686, 0.5, 0.25, 0.001])]
    (tmp / "tests.json").write_text(json.dumps({"tests": tests, "skipped": []}))
    for i in range(n_patches):
        (tmp / "commits" / f"p{i}.patch").write_text(
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n")
    return tests


def run_with(select_fn, n_patches=5):
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        make_data(tmp, n_patches)
        M.DATA = tmp
        mod = types.ModuleType("selector.impact")
        if select_fn is not None:
            mod.select = select_fn
        sys.modules["selector.impact"] = mod
        import selector
        selector.impact = mod
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                M.main()
        finally:
            del sys.modules["selector.impact"]
            del selector.impact
        lines = out.getvalue().splitlines()
        return [json.loads(l) for l in lines], err.getvalue()


FULL = {0, 1, 2, 3, 4, 5}


class TestMain(unittest.TestCase):
    def check(self, objs, n=5):
        self.assertEqual(len(objs), n)
        self.assertEqual(len({o["commit"] for o in objs}), n)
        for o in objs:
            flat = [t for w in o["workers"] for t in w]
            self.assertEqual(len(flat), len(set(flat)))

    def test_raises(self):
        def sel(text, cat):
            raise RuntimeError("boom")
        objs, _ = run_with(sel)
        self.check(objs)
        for o in objs:
            self.assertEqual({t for w in o["workers"] for t in w}, FULL)

    def test_missing_select(self):
        objs, _ = run_with(None)
        self.check(objs)

    def test_bad_numbers(self):
        objs, _ = run_with(lambda t, c: {"required": {99}, "plausible": set(), "full": False})
        self.check(objs)
        for o in objs:
            self.assertEqual({t for w in o["workers"] for t in w}, FULL)

    def test_timeout(self):
        M.PER_PATCH_S = 0.1
        def sel(t, c):
            time.sleep(5)
        t0 = time.monotonic()
        objs, _ = run_with(sel, 3)
        self.assertLess(time.monotonic() - t0, 2)
        self.check(objs, 3)
        for o in objs:
            self.assertEqual({t for w in o["workers"] for t in w}, FULL)

    def test_narrow(self):
        objs, _ = run_with(lambda t, c: {"required": {3}, "plausible": {4}, "full": False})
        self.check(objs)
        for o in objs:
            flat = {t for w in o["workers"] for t in w}
            self.assertIn(3, flat)
            self.assertNotIn(0, flat)  # 28.8 s test is not free

    def test_empty(self):
        objs, _ = run_with(lambda t, c: {"required": set(), "plausible": {4}, "full": False})
        self.check(objs)
        for o in objs:
            self.assertEqual(o["workers"], [])


if __name__ == "__main__":
    unittest.main()
