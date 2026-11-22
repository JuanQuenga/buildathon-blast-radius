import ast
import difflib
import unittest
from unittest import mock

from selector import impact
from selector.impact import _same_observed_order, _same_observed_comparison, _sources, select

SITE_SRC = "def f(a, b):\n    if a < b:\n        return 1\n    return a > b and b\n"


def _patch(path, old, new):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}"))


class TestObservedOrder(unittest.TestCase):
    def setUp(self):
        impact._order_sites.cache_clear()

    def tearDown(self):
        impact._order_sites.cache_clear()

    def check(self, new_src, sites):
        with mock.patch.object(impact, "_order_sites", return_value=frozenset(sites)):
            return _same_observed_order("sqlglot/x.py", ast.parse(SITE_SRC), ast.parse(new_src))

    def test_strict_twin_at_recorded_site(self):
        self.assertTrue(self.check(SITE_SRC.replace("a < b", "a <= b"), {"sqlglot.x-2-7"}))
        self.assertTrue(self.check(SITE_SRC.replace("a > b", "a >= b"), {"sqlglot.x-4-11"}))

    def test_rejects_unrecorded_site_and_other_operators(self):
        self.assertFalse(self.check(SITE_SRC.replace("a < b", "a <= b"), {"sqlglot.x-4-11"}))
        self.assertFalse(self.check(SITE_SRC.replace("a < b", "a > b"), {"sqlglot.x-2-7"}))
        self.assertFalse(self.check(SITE_SRC.replace("a < b", "a == b"), {"sqlglot.x-2-7"}))
        self.assertFalse(self.check(SITE_SRC.replace("a < b", "b <= a"), {"sqlglot.x-2-7"}))

    def test_rejects_any_other_change(self):
        new = SITE_SRC.replace("a < b", "a <= b").replace("return 1", "return 2")
        self.assertFalse(self.check(new, {"sqlglot.x-2-7"}))
        new = SITE_SRC.replace("a < b", "a <= b").replace("a > b", "a >= b")
        self.assertFalse(self.check(new, {"sqlglot.x-2-7", "sqlglot.x-4-11"}))


class TestRecordedEquivalenceMaps(unittest.TestCase):
    """End-to-end through select() on real pinned sources and the committed maps."""

    @staticmethod
    def _catalog():
        tests = impact._index()["tests"]
        return [{"number": i, "id": t, "file": t.split("::")[0], "seconds": 0.001} for i, t in enumerate(tests)]

    def _first_site(self, sites, op_text):
        srcs = _sources()["src"]
        for site in sorted(sites):
            module, line, col = site.rsplit("-", 2)
            path = module.replace(".", "/") + ".py"
            if path not in srcs:
                path = module.replace(".", "/") + "/__init__.py"
            if path not in srcs:
                continue
            lines = srcs[path].split("\n")
            text = lines[int(line) - 1]
            tree = ast.parse(srcs[path])
            for node in ast.walk(tree):
                if (isinstance(node, ast.Compare) and node.lineno == int(line) and node.col_offset == int(col)
                        and len(node.ops) == 1 and node.end_lineno == node.lineno):
                    seg = text[node.left.end_col_offset:node.comparators[0].col_offset]
                    if seg.strip() == op_text and text.count(seg) == 1:
                        return path, srcs[path], text, seg
        return None

    def test_order_map_sites_select_nothing(self):
        found = self._first_site(impact._order_sites(), "<")
        if found is None:
            self.skipTest("no single-line '<' site in order.json")
        path, src, text, seg = found
        new = src.replace(text, text.replace(seg, seg.replace("<", "<="), 1), 1)
        result = select(_patch(path, src, new), self._catalog())
        self.assertFalse(result["full"], result["reasons"])
        self.assertEqual(result["required"], set(), result["reasons"])
        self.assertTrue(any("ordering agreed" in r for r in result["reasons"]), result["reasons"])

    def test_identity_map_sites_select_nothing(self):
        found = self._first_site(impact._identity_sites(), "is not")
        if found is None:
            self.skipTest("no single-line 'is not' site in identity.json")
        path, src, text, seg = found
        new = src.replace(text, text.replace(seg, seg.replace("is not", "!="), 1), 1)
        result = select(_patch(path, src, new), self._catalog())
        self.assertFalse(result["full"], result["reasons"])
        self.assertEqual(result["required"], set(), result["reasons"])
        self.assertTrue(any("identity and equality agreed" in r for r in result["reasons"]), result["reasons"])


if __name__ == "__main__":
    unittest.main()


class TestPerTestSubstitution(unittest.TestCase):
    """A substitution at a site with recorded per-test disagreement keeps exactly those tests."""

    PATH = "sqlglot/tokenizer_core.py"
    SITE = "sqlglot.tokenizer_core-1015-19"

    def setUp(self):
        impact._order_sites.cache_clear()
        impact._equiv_tests.cache_clear()
        src = _sources()["src"][self.PATH]
        lines = src.split("\n")
        old = lines[1014]
        self.assertIn("self._current + 1 < self.size", old)
        lines[1014] = old.replace("self._current + 1 < self.size", "self._current + 1 <= self.size")
        self.patch = _patch(self.PATH, src, "\n".join(lines))
        tests = impact._index()["tests"]
        self.catalog = [{"number": i, "id": t, "file": t.split("::")[0], "seconds": 0.001} for i, t in enumerate(tests)]
        self.kept = tests[0]

    def tearDown(self):
        impact._order_sites.cache_clear()
        impact._equiv_tests.cache_clear()

    def select(self, tests):
        with mock.patch.object(impact, "_order_sites", return_value=frozenset()), \
                mock.patch.object(impact, "_equiv_tests", return_value={"identity": {}, "order": {self.SITE: tests}}):
            return select(self.patch, self.catalog)

    def test_keeps_only_disagreeing_tests(self):
        result = self.select([self.kept])
        self.assertFalse(result["full"], result["reasons"])
        self.assertEqual(result["required"], {0}, result["reasons"])
        self.assertTrue(any("diverged in 1 pinned tests" in r for r in result["reasons"]), result["reasons"])

    def test_empty_disagreement_selects_nothing(self):
        self.assertEqual(self.select([])["required"], set())

    def test_unknown_test_falls_back_to_static_analysis(self):
        result = self.select(["tests/test_nowhere.py::T::test_missing"])
        self.assertGreater(len(result["required"]), 100, result["reasons"])
