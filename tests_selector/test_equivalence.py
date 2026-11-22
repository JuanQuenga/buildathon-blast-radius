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


class TestJsonPathTransformsNarrowing(unittest.TestCase):
    """Cherry-picked from candidate 37: JSON_PATH_PART_TRANSFORMS edits narrow via per-key dispatch observations."""

    def test_literal_edit_does_not_select_every_test(self):
        idx = impact._index()
        cat = [{"number": i, "id": t} for i, t in enumerate(idx["tests"])]
        lines = impact._sources()["src"]["sqlglot/jsonpath.py"].splitlines()
        ln = next(i for i, line in enumerate(lines, 1) if '"" if p is False' in line)
        patch = ("--- a/sqlglot/jsonpath.py\n+++ b/sqlglot/jsonpath.py\n"
                 f"@@ -{ln},1 +{ln},1 @@\n-{lines[ln - 1]}\n+{lines[ln - 1].replace('False', '0')}\n")
        result = select(patch, cat)
        self.assertFalse(result["full"], result["reasons"])
        self.assertLess(len(result["required"]), 100, result["reasons"])


class TestBoolAndReturnSubstitution(unittest.TestCase):
    """and/or swaps of truth-tested two-operand BoolOps and ``return X`` -> ``return None and X``."""

    def setUp(self):
        impact._equiv_bool_tests.cache_clear()
        impact._equiv_sites.cache_clear()
        tests = impact._index()["tests"]
        self.catalog = [{"number": i, "id": t, "file": t.split("::")[0], "seconds": 0.001} for i, t in enumerate(tests)]
        self.positions = {t: i for i, t in enumerate(tests)}

    @staticmethod
    def _locate(site, kind):
        srcs = _sources()["src"]
        module, line, col = site.rsplit("-", 2)
        for path in (module.replace(".", "/") + ".py", module.replace(".", "/") + "/__init__.py"):
            if path in srcs:
                break
        else:
            return None
        src = srcs[path]
        text = src.split("\n")[int(line) - 1]
        for node in ast.walk(ast.parse(src)):
            if getattr(node, "lineno", None) != int(line):
                continue
            if kind == "bool" and isinstance(node, ast.BoolOp) and node.col_offset == int(col):
                a, b = node.values
                if a.end_lineno != node.lineno or b.lineno != node.lineno:
                    return None
                seg = text[a.end_col_offset:b.col_offset]
                op = seg.strip()
                if op not in ("and", "or"):
                    return None
                new = text[:a.end_col_offset] + seg.replace(op, "or" if op == "and" else "and") + text[b.col_offset:]
                return path, src, text, new
            if kind == "ret" and isinstance(node, ast.Return) and node.col_offset == int(col):
                if node.value.lineno != node.lineno:
                    return None
                v = node.value.col_offset
                return path, src, text, text[:v] + "None and " + text[v:]
        return None

    def _check(self, kind, sites, expect_tests):
        for site in sorted(sites):
            found = self._locate(site, kind)
            if found is None:
                continue
            path, src, text, new_line = found
            if src.count(text) != 1:
                continue
            result = select(_patch(path, src, src.replace(text, new_line, 1)), self.catalog)
            self.assertFalse(result["full"], result["reasons"])
            self.assertTrue(any(f"{kind} substitution at {site}" in r for r in result["reasons"]), result["reasons"])
            self.assertEqual(result["required"], {self.positions[t] for t in expect_tests(site)}, result["reasons"])
            return True
        self.skipTest(f"no single-line {kind} site")

    def test_globally_certified_bool_site_selects_nothing(self):
        self._check("bool", impact._equiv_sites("bool"), lambda site: [])

    def test_per_test_bool_site_keeps_disagreeing_tests(self):
        small = {s for s, t in impact._equiv_bool_tests()["bool"].items() if 0 < len(t) <= 5}
        self._check("bool", small, lambda site: impact._equiv_bool_tests()["bool"][site])

    def test_return_none_site_keeps_tests_that_returned_a_value(self):
        sites = set(impact._equiv_sites("ret")) | {s for s, t in impact._equiv_bool_tests()["ret"].items() if len(t) <= 5}
        self._check("ret", sites, lambda site: [] if site in impact._equiv_sites("ret") else impact._equiv_bool_tests()["ret"][site])

    def test_synthetic_trees(self):
        old = ast.parse("def f(a, b):\n    if a and b:\n        return a\n    return b\n")
        swapped = ast.parse("def f(a, b):\n    if a or b:\n        return a\n    return b\n")
        self.assertEqual(impact._bool_swap_site("sqlglot/x.py", old, swapped), "sqlglot.x-2-7")
        both = ast.parse("def f(a, b):\n    if a or b:\n        return b\n    return b\n")
        self.assertIsNone(impact._bool_swap_site("sqlglot/x.py", old, both))
        ret = ast.parse("def f(a, b):\n    if a and b:\n        return None and a\n    return b\n")
        self.assertEqual(impact._return_none_site("sqlglot/x.py", old, ret), "sqlglot.x-3-8")
        ret2 = ast.parse("def f(a, b):\n    if a and b:\n        return None or a\n    return b\n")
        self.assertIsNone(impact._return_none_site("sqlglot/x.py", old, ret2))


class TestBoolAndReturnObserver(unittest.TestCase):
    """The trace observer behind bool.json/ret.json only certifies swaps that change nothing."""

    SRC = (
        "calls = []\n"
        "def eff(x):\n"
        "    calls.append(x)\n"
        "    return True\n"
        "def skipped_effect(a):\n"
        "    if a and eff(1):\n"
        "        return 1\n"
        "def free(a, b):\n"
        "    if a and isinstance(b, int):\n"
        "        return 1\n"
        "def attr(o):\n"
        "    return o.missing\n"
        "def raises_in_b(a, o):\n"
        "    if a and o.missing:\n"
        "        return 1\n"
    )

    @classmethod
    def setUpClass(cls):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "tools" / "equiv_trace" / "sitecustomize.py"
        with mock.patch.dict("os.environ", {"BR_EQUIV_OUT": "", "BR_EQUIV_PERTEST": ""}):
            spec = importlib.util.spec_from_file_location("br_equiv_observer", path)
            cls.obs = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.obs)

    def setUp(self):
        import builtins
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        obs = self.obs
        obs.out, obs.seen = self.tmp.name, set()
        del obs._bstack[:]
        for name, fn in (("_br_bool_pre", obs.bool_pre), ("_br_bool_mid", obs.bool_mid),
                         ("_br_bool_post", obs.bool_post), ("_br_ret", obs.ret_obs)):
            patcher = mock.patch.object(builtins, name, fn, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

        class Loader:
            path = "sqlglot/fake.py"

            def get_data(_, _path):
                return self.SRC.encode()

        self.ns = {}
        exec(obs.get_code(Loader(), "sqlglot.fake"), self.ns)

    def statuses(self, prefix):
        import os
        return {name.rpartition(".")[2] for name in os.listdir(self.tmp.name) if name.startswith(prefix)}

    def test_effect_free_operands(self):
        names = self.obs._free_names(ast.parse("from sqlglot.helper import seq_get\ndef f(type): pass\n"))
        free = lambda src: self.obs._effect_free(ast.parse(src, mode="eval").body, names)  # noqa: E731
        self.assertTrue(free("isinstance(x, A) and x.args.get('k') and seq_get(x.expressions, 0)"))
        self.assertTrue(free("x.this.is_type('int') or x.name.upper() == 'A'"))
        for src in ("self._match(TokenType.L_PAREN)", "type(x)", "Dialect.get('duckdb')", "x.find(A)",
                    "len(*xs)", "x.append(1)", "(y := x)", "any(e for e in it)"):
            self.assertFalse(free(src), src)

    def test_skipped_side_effect_is_not_agreement(self):
        # ``a and eff(1)`` -> ``a or eff(1)``: same truthiness, but the swap skips eff's effect.
        self.assertEqual(self.ns["skipped_effect"](True), 1)
        self.assertEqual(self.ns["calls"], [1])
        self.assertEqual(self.statuses("bool-sqlglot.fake-6-7"), {"unknown"})

    def test_effect_free_operand_agrees(self):
        self.ns["free"](True, 3)
        self.assertEqual(self.statuses("bool-sqlglot.fake-9-7"), {"agree"})
        self.ns["free"](False, 3)  # the swap would evaluate isinstance(b, int): truthy, original falsy
        self.assertEqual(self.statuses("bool-sqlglot.fake-9-7"), {"agree", "disagree"})

    def test_return_value_that_raises_is_divergent(self):
        with self.assertRaises(AttributeError):
            self.ns["attr"](object())
        self.assertEqual(self.statuses("ret-sqlglot.fake-12-4"), {"disagree"})

    def test_second_operand_raising_is_not_agreement(self):
        with self.assertRaises(AttributeError):
            self.ns["raises_in_b"](True, object())
        self.obs.bool_flush()
        self.assertEqual(self.statuses("bool-sqlglot.fake-14-7"), {"unknown"})
