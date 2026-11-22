"""Unit tests for candidate-19 mechanisms and safety invariants."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from selector import fixture_fields, heavy, impact, result_replay, runtime_paths


class TestCandidate19(unittest.TestCase):
    def test_fixture_exact_parsed_comment_and_meta_spacing(self):
        base = (
            "-- header comment\n"
            "# title: foo\n"
            "SELECT 1;\n"
            "SELECT 1;\n"
        )
        same_comment = (
            "-- header comment\n"
            "-- extra comment\n"
            "\n"
            "# title:foo\n"
            "SELECT 1;\n"
            "SELECT 1;\n"
        )
        changed_sql = (
            "-- header comment\n"
            "# title: foo\n"
            "SELECT 2;\n"
            "SELECT 1;\n"
        )
        self.assertEqual(
            fixture_fields._exact_parsed_fixture(base, paired=True),
            fixture_fields._exact_parsed_fixture(same_comment, paired=True),
        )
        self.assertNotEqual(
            fixture_fields._exact_parsed_fixture(base, paired=True),
            fixture_fields._exact_parsed_fixture(changed_sql, paired=True),
        )

    def test_tpc_consumers_rejects_malformed_meta(self):
        entry = {"raw": [], "reads": {"tests/test_executor.py::TestExecutor::test_execute_tpch": [2]}}
        valid = "# execute: false\nSELECT 1;\nSELECT 1;\n"
        self.assertEqual(
            fixture_fields.tpc_consumers(valid, entry),
            {"tests/test_executor.py::TestExecutor::test_execute_tpch"},
        )
        bad = "# malformed_without_colon\nSELECT 1;\nSELECT 1;\n"
        with self.assertRaises(ValueError):
            fixture_fields.tpc_consumers(bad, entry)

    def test_join_budget_exceeded_is_base_exception(self):
        # Must not be caught by ``except Exception:`` in patched code.
        self.assertTrue(issubclass(result_replay._JoinBudgetExceeded, BaseException))
        self.assertFalse(issubclass(result_replay._JoinBudgetExceeded, Exception))

    def test_needs_parse_check(self):
        self.assertFalse(result_replay._needs_parse_check(["sqlglot.optimizer.simplify"]))
        self.assertFalse(result_replay._needs_parse_check(["sqlglot.generators.duckdb", "sqlglot.typing.duckdb"]))
        self.assertFalse(result_replay._needs_parse_check(["sqlglot.executor.python", "sqlglot.planner"]))
        self.assertTrue(result_replay._needs_parse_check(["sqlglot.optimizer.scope"]))
        self.assertTrue(result_replay._needs_parse_check(["sqlglot.expressions.datatypes"]))
        self.assertTrue(result_replay._needs_parse_check(["sqlglot.serde"]))
        self.assertTrue(result_replay._needs_parse_check(["sqlglot.parser"]))
        self.assertTrue(result_replay._needs_parse_check([]))

    def test_method_tail_queries_rejects_early_close_injection(self):
        from selector.patch import parse

        path = "sqlglot/schema.py"
        before = impact._sources()["src"][path]
        scopes = impact._index()["scopes"][path][1]
        fn = next(s for s in scopes if s[0] == "def" and not s[3].rsplit(".", 1)[-1].startswith("__"))
        start, end, _name, body_start = fn[1], fn[2], fn[3], fn[4]
        lines = before.splitlines()
        # Craft a fake patch that injects an unindented class-level statement at end of method.
        indent = lines[start - 1][: len(lines[start - 1]) - len(lines[start - 1].lstrip())]
        injected = lines[:end] + [indent + "_INJECTED = 1"] + lines[end:]
        after = "\n".join(injected) + "\n"
        diff = (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -{end},1 +{end},2 @@\n"
            f" {lines[end - 1]}\n"
            f"+{indent}_INJECTED = 1\n"
        )
        fd = parse(diff)[0]
        rows = [{"calls": [(path, start)]}]
        self.assertIsNone(heavy._method_tail_queries(path, after, fd, rows))
        # A genuine body-only insertion inside the same method returns [0].
        body_indent = lines[body_start - 1][: len(lines[body_start - 1]) - len(lines[body_start - 1].lstrip())]
        ok_lines = lines[:body_start] + [body_indent + "pass"] + lines[body_start:]
        ok_after = "\n".join(ok_lines) + "\n"
        ok_diff = (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -{body_start},1 +{body_start},2 @@\n"
            f" {lines[body_start - 1]}\n"
            f"+{body_indent}pass\n"
        )
        ok_fd = parse(ok_diff)[0]
        self.assertEqual(heavy._method_tail_queries(path, ok_after, ok_fd, rows), [0])

    def test_needs_transpile_check(self):
        self.assertFalse(result_replay._needs_transpile_check(["sqlglot.optimizer.simplify"]))
        self.assertFalse(result_replay._needs_transpile_check(["sqlglot.executor.python", "sqlglot.planner"]))
        self.assertTrue(result_replay._needs_transpile_check(["sqlglot.optimizer.scope"]))
        self.assertTrue(result_replay._needs_transpile_check(["sqlglot.generators.duckdb"]))
        self.assertTrue(result_replay._needs_transpile_check([]))

    def test_bundle_trim_preserves_subtest_and_failures(self):
        bundle_script = Path(__file__).resolve().parents[1] / "selector" / "bundle.py"
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp) / "tests"
            tdir.mkdir()
            (tdir / "__init__.py").write_text("")
            (tdir / "test_dummy.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertEqual(1, 1)\n"
                "    def test_fail(self):\n"
                "        self.assertEqual(1, 2)\n"
                "    def test_sub_fail(self):\n"
                "        with self.subTest('a'):\n"
                "            self.assertEqual(1, 1)\n"
                "        with self.subTest('b'):\n"
                "            self.assertEqual(1, 2)\n"
                "    def test_skip(self):\n"
                "        self.skipTest('nope')\n"
            )
            nodes = [
                "tests/test_dummy.py::T::test_ok",
                "tests/test_dummy.py::T::test_fail",
                "tests/test_dummy.py::T::test_sub_fail",
                "tests/test_dummy.py::T::test_skip",
            ]
            r = subprocess.run(
                [sys.executable, str(bundle_script), "trim", json.dumps(nodes)],
                cwd=tmp,
                capture_output=True,
                text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            passed = json.loads((Path(tmp) / ".blast-passed.json").read_text())
            self.assertEqual(passed, ["tests/test_dummy.py::T::test_ok"])

    def test_inert_new_class_attrs(self):
        src = impact._sources()["src"]["sqlglot/dialects/dialect.py"]
        idx = src.index("class Dialect(")
        line_end = src.index("\n", idx) + 1
        added = (
            src[:line_end]
            + "    _BRAND_NEW_UNREAD_FLAG_999 = False\n"
            + '    """Docstring for brand-new flag."""\n'
            + src[line_end:]
        )
        self.assertTrue(heavy._inert_new_class_attrs("sqlglot/dialects/dialect.py", added))
        # Existing attribute name must NOT be considered inert.
        existing = src[:line_end] + "    NORMALIZE_FUNCTIONS = False\n" + src[line_end:]
        self.assertFalse(heavy._inert_new_class_attrs("sqlglot/dialects/dialect.py", existing))

    def test_bundle_no_exec_setup_flag(self):
        bundle_script = Path(__file__).resolve().parents[1] / "selector" / "bundle.py"
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp) / "tests"
            tdir.mkdir()
            (tdir / "__init__.py").write_text("")
            (tdir / "test_executor.py").write_text(
                "import unittest\n"
                "class TestExecutor(unittest.TestCase):\n"
                "    @classmethod\n"
                "    def setUpClass(cls):\n"
                "        raise RuntimeError('should be skipped for non-TPC tests')\n"
                "    def test_unit(self):\n"
                "        self.assertEqual(2, 2)\n"
                "    def test_execute_tpch(self):\n"
                "        self.assertEqual(2, 2)\n"
            )
            # Non-TPC test skips setUpClass when no-exec-setup flag is passed.
            r1 = subprocess.run(
                [sys.executable, str(bundle_script), "bundle",
                 json.dumps(["tests/test_executor.py::TestExecutor::test_unit"]), "no-exec-setup"],
                cwd=tmp, capture_output=True, text=True,
            )
            self.assertEqual(r1.returncode, 0, r1.stderr)
            # TPC test MUST NOT skip setUpClass even with no-exec-setup flag.
            r2 = subprocess.run(
                [sys.executable, str(bundle_script), "bundle",
                 json.dumps(["tests/test_executor.py::TestExecutor::test_execute_tpch"]), "no-exec-setup"],
                cwd=tmp, capture_output=True, text=True,
            )
            self.assertNotEqual(r2.returncode, 0)

    def test_dict_keys_narrowing(self):
        import ast
        from selector import dict_keys

        o = ast.parse("TRANSFORMS = {**Base.TRANSFORMS, exp.Abs: fn1, exp.WithoutRowidProperty: fn2}\n").body
        n = ast.parse("TRANSFORMS = {**Base.TRANSFORMS, exp.Abs: fn1}\n").body
        self.assertEqual(dict_keys.changed_keys("TRANSFORMS", o, n), {"E:WithoutRowidProperty"})
        # Dynamic unpack change must fall back (return None).
        n_bad = ast.parse("TRANSFORMS = {**Other.TRANSFORMS, exp.Abs: fn1}\n").body
        self.assertIsNone(dict_keys.changed_keys("TRANSFORMS", o, n_bad))
        # DictComp in EXPRESSION_METADATA
        om = ast.parse("EXPRESSION_METADATA = {**BASE, **{k: {'returns': exp.DType.INT} for k in {exp.Abs, exp.Ceil}}}\n").body
        nm = ast.parse("EXPRESSION_METADATA = {**BASE, **{k: {'returns': exp.DType.INT} for k in {exp.Abs, exp.Ceil, exp.Floor}}}\n").body
        self.assertEqual(dict_keys.changed_keys("EXPRESSION_METADATA", om, nm), {"E:Floor"})
        # Narrowing shrinks full bitmask dramatically while keeping bulk observers.
        full_bits = (1 << len(impact._index()["tests"])) - 1
        narrowed = dict_keys.narrow_bits("TRANSFORMS", full_bits, o, n)
        self.assertIsNotNone(narrowed)
        self.assertLess(bin(narrowed).count("1"), 50)
        self.assertGreater(bin(narrowed).count("1"), 0)

    def test_runtime_paths_defaults(self):
        self.assertIsInstance(runtime_paths.APP_DIR, Path)
        self.assertIsInstance(runtime_paths.DATA_DIR, Path)


if __name__ == "__main__":
    unittest.main()
