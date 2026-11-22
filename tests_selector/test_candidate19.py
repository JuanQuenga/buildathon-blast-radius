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

    def test_full_keyword_and_date_maps_loaded(self):
        from selector import token_options

        kw = token_options.keyword_reads()
        dt = token_options.date_reads()
        self.assertGreater(len(kw["profiles"]), 1500)
        self.assertGreater(len(dt), 1500)
        self.assertIn("VALUES", kw["excluded"])

    def test_parser2_disjoint_and_errors_scaled(self):
        from selector.patch import parse

        # errors.py edit (no numeric/loop keywords) -> eligible_file('parser') is True
        err_src = impact._sources()["src"]["sqlglot/errors.py"].splitlines()
        idx = next(i for i, ln in enumerate(err_src) if "highlight_start > previous_part_end" in ln)
        diff = (
            "diff --git a/sqlglot/errors.py b/sqlglot/errors.py\n"
            "--- a/sqlglot/errors.py\n"
            "+++ b/sqlglot/errors.py\n"
            f"@@ -{idx+1},1 +{idx+1},1 @@\n"
            f"-{err_src[idx]}\n"
            f"+{err_src[idx].replace('>', '>=')}\n"
        )
        e = heavy.stage_edit(diff, "parser")
        self.assertIsNotNone(e)
        self.assertEqual(e[0], "scaled")
        # dialects/__init__.py edit (no Parser/max_nodes/setattr) -> small
        d_src = impact._sources()["src"]["sqlglot/dialects/__init__.py"].splitlines()
        d_idx = next(i for i, ln in enumerate(d_src) if '"Dune"' in ln)
        d_diff = (
            "diff --git a/sqlglot/dialects/__init__.py b/sqlglot/dialects/__init__.py\n"
            "--- a/sqlglot/dialects/__init__.py\n"
            "+++ b/sqlglot/dialects/__init__.py\n"
            f"@@ -{d_idx+1},1 +{d_idx+1},0 @@\n"
            f"-{d_src[d_idx]}\n"
        )
        de = heavy.stage_edit(d_diff, "parser")
        self.assertIsNotNone(de)
        self.assertEqual(de[0], "small")

    def test_doc_path_harmless_and_pytest_level_blocked(self):
        self.assertTrue(heavy._is_doc_path("README.md"))
        self.assertTrue(heavy._is_doc_path("docs/guide.rst"))
        self.assertFalse(heavy._is_doc_path("pyproject.toml"))
        self.assertFalse(heavy._is_doc_path("sqlglot/parser.py"))
        self.assertFalse(heavy._is_doc_path("tests/test_parser.py"))

    def test_rule_corpus_divergent_cost_helper(self):
        from selector import rule_corpus

        cost_all = rule_corpus._divergent_exec_cost(set(range(99)))
        self.assertGreater(cost_all, 5.0)
        self.assertEqual(rule_corpus._divergent_exec_cost(set()), 0.0)

    def test_full_branch_and_operand_maps_loaded(self):
        sites, _tail = impact._branch_map()
        ops = impact._operand_map()
        self.assertGreater(len(sites), 5000)
        self.assertGreater(len(ops), 400)
        # Verify test_execute_tpcds (exact single-process baseline) is represented
        tpcds_i = impact._index()["tests"].index(
            "tests/test_executor.py::TestExecutor::test_execute_tpcds"
        )
        tpcds_bit = 1 << tpcds_i
        self.assertTrue(any((b0 | b1) & tpcds_bit for b0, b1 in sites.values()))

    def test_dict_keys_fromkeys_and_sql_method_bits(self):
        import ast
        from selector import dict_keys

        o = ast.parse('FUNCTIONS = {**Parser.FUNCTIONS, "A": f1, "B": f2}\n').body
        n = ast.parse('FUNCTIONS = {**Parser.FUNCTIONS, **dict.fromkeys(("A", "B", "C"), f1)}\n').body
        self.assertEqual(dict_keys.changed_keys("FUNCTIONS", o, n), {"S:B", "S:C"})
        # sql_method_bits narrows brand-new unknown expr key to bulk-only (very small)
        brand_new = dict_keys.sql_method_bits("nonexistentexprxyz_sql")
        self.assertIsNotNone(brand_new)
        self.assertLess(bin(brand_new).count("1"), 20)
        # Known expr key includes tests that look up E:CurrentTime in TRANSFORMS
        ct_bits = dict_keys.sql_method_bits("currenttime_sql")
        self.assertIsNotNone(ct_bits)
        self.assertLess(bin(ct_bits).count("1"), 80)
        self.assertGreater(bin(ct_bits).count("1"), 0)

    def test_named_added_brand_new_helper_and_sql_dispatch(self):
        # Adding a brand-new private helper on Parser must NOT pull in all 1618 Parser tests.
        p_src = impact._sources()["src"]["sqlglot/parser.py"].splitlines()
        idx = next(i for i, ln in enumerate(p_src) if ln.startswith("    def _parse("))
        diff = (
            "diff --git a/sqlglot/parser.py b/sqlglot/parser.py\n"
            "--- a/sqlglot/parser.py\n"
            "+++ b/sqlglot/parser.py\n"
            f"@@ -{idx+1},1 +{idx+1},4 @@\n"
            "+    def _brand_new_helper_xyz(self):\n"
            "+        return None\n"
            "+\n"
            f" {p_src[idx]}\n"
        )
        cat = [{"number": i, "id": t, "seconds": 0.01} for i, t in enumerate(impact._index()["tests"])]
        res = impact.select(diff, cat)
        self.assertFalse(res.get("full"))
        self.assertEqual(len(res.get("required", ())), 0)

    def test_cross_file_new_tokentype_member_no_full_fallback(self):
        tc_src = impact._sources()["src"]["sqlglot/tokenizer_core.py"].splitlines()
        idx_tc = next(i for i, ln in enumerate(tc_src) if "ALIAS = auto()" in ln)
        sf_src = impact._sources()["src"]["sqlglot/dialects/snowflake.py"].splitlines()
        idx_sf = next(i for i, ln in enumerate(sf_src) if '"ACCOUNT":' in ln or "KEYWORDS = {" in ln)
        diff = (
            "diff --git a/sqlglot/tokenizer_core.py b/sqlglot/tokenizer_core.py\n"
            "--- a/sqlglot/tokenizer_core.py\n"
            "+++ b/sqlglot/tokenizer_core.py\n"
            f"@@ -{idx_tc+1},1 +{idx_tc+1},2 @@\n"
            f" {tc_src[idx_tc]}\n"
            "+    BRAND_NEW_TOK_XYZ = auto()\n"
            "diff --git a/sqlglot/dialects/snowflake.py b/sqlglot/dialects/snowflake.py\n"
            "--- a/sqlglot/dialects/snowflake.py\n"
            "+++ b/sqlglot/dialects/snowflake.py\n"
            f"@@ -{idx_sf+1},1 +{idx_sf+1},2 @@\n"
            f" {sf_src[idx_sf]}\n"
            '+            "BRAND_NEW_KW_XYZ": TokenType.BRAND_NEW_TOK_XYZ,\n'
        )
        cat = [{"number": i, "id": t, "seconds": 0.01} for i, t in enumerate(impact._index()["tests"])]
        res = impact.select(diff, cat)
        self.assertFalse(res.get("full"), res.get("reasons"))
        self.assertLess(len(res.get("required", ())), 50)

    def test_expr_subclass_arg_types_tail_is_empty(self):
        from selector.patch import parse

        path = "sqlglot/expressions/core.py"
        src = impact._sources()["src"][path]
        old_line = '        "unnest": False,\n'
        self.assertIn(old_line, src)
        after = src.replace(old_line, "", 1)
        ln = src.splitlines().index('        "unnest": False,') + 1
        diff = (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -{ln},1 +{ln},0 @@\n"
            f"-{old_line}"
        )
        fd = parse(diff)[0]
        self.assertEqual(heavy.tail_cases(path, after, fd, strict=True), [])

    def test_bundle_setup_cost_charges_tpc_setup(self):
        cost, uses_heavy = heavy._bundle_setup_cost(
            {"tests/test_executor.py"},
            ["tests/test_executor.py::TestExecutor::test_execute_tpch"],
            no_exec_setup=True,
        )
        self.assertGreater(cost, 0.5)
        self.assertTrue(uses_heavy)

    def test_dialects_comment_word_not_hazard(self):
        self.assertFalse(heavy._has_hazard_token("    # leaks into other dialects"))
        self.assertTrue(heavy._has_hazard_token("    x = DIALECTS"))
        self.assertTrue(heavy._has_hazard_token("    self._cache = {}"))

    def test_parser_scaled_eligible_guards_thresholds_and_loops(self):
        from selector.patch import parse

        # Benign parser.py edit with small int (< 50) outside loop -> eligible
        d_ok = (
            "diff --git a/sqlglot/parser.py b/sqlglot/parser.py\n"
            "--- a/sqlglot/parser.py\n"
            "+++ b/sqlglot/parser.py\n"
            "@@ -100,1 +100,1 @@\n"
            "-    x = self._tokens[self._index - 2]\n"
            "+    x = self._tokens[self._index - 1]\n"
        )
        self.assertTrue(heavy._parser_scaled_eligible(parse(d_ok)[0]))
        # Numeric literal >= 50 -> rejected
        d_big = (
            "diff --git a/sqlglot/parser.py b/sqlglot/parser.py\n"
            "--- a/sqlglot/parser.py\n"
            "+++ b/sqlglot/parser.py\n"
            "@@ -100,1 +100,1 @@\n"
            "-    x = 1\n"
            "+    x = 50000\n"
        )
        self.assertFalse(heavy._parser_scaled_eligible(parse(d_big)[0]))
        # max_nodes mention -> rejected
        d_mn = (
            "diff --git a/sqlglot/parser.py b/sqlglot/parser.py\n"
            "--- a/sqlglot/parser.py\n"
            "+++ b/sqlglot/parser.py\n"
            "@@ -100,1 +100,1 @@\n"
            "-    x = self.max_nodes\n"
            "+    x = None\n"
        )
        self.assertFalse(heavy._parser_scaled_eligible(parse(d_mn)[0]))

    def test_core_file_disjoint_from_gen_loop(self):
        from selector.patch import parse

        # Edit inside a *_sql method far outside Generator.__init__ -> disjoint
        d_sql = (
            "diff --git a/sqlglot/generator.py b/sqlglot/generator.py\n"
            "--- a/sqlglot/generator.py\n"
            "+++ b/sqlglot/generator.py\n"
            "@@ -3900,1 +3900,1 @@\n"
            "-        return 'A'\n"
            "+        return 'B'\n"
        )
        self.assertTrue(heavy._core_file_disjoint_from_gen_loop(parse(d_sql)[0]))
        # Edit inside Generator.__init__ (L970) -> NOT disjoint
        d_init = (
            "diff --git a/sqlglot/generator.py b/sqlglot/generator.py\n"
            "--- a/sqlglot/generator.py\n"
            "+++ b/sqlglot/generator.py\n"
            "@@ -970,1 +970,1 @@\n"
            "-        self.normalize = normalize\n"
            "+        self.normalize = True\n"
        )
        self.assertFalse(heavy._core_file_disjoint_from_gen_loop(parse(d_init)[0]))

    def test_pure_immutable_dispatch_dict_deletion_no_full_fallback(self):
        src = impact._sources()["src"]["sqlglot/parsers/druid.py"].splitlines()
        idx = next(i for i, ln in enumerate(src) if "FUNCTION_PARSERS = {" in ln)
        end = next(i for i in range(idx, len(src)) if src[i].strip() == "}")
        removed = "\n".join(f"-{src[i]}" for i in range(idx, end + 1))
        diff = (
            "diff --git a/sqlglot/parsers/druid.py b/sqlglot/parsers/druid.py\n"
            "--- a/sqlglot/parsers/druid.py\n"
            "+++ b/sqlglot/parsers/druid.py\n"
            f"@@ -{idx+1},{end-idx+1} +{idx+1},1 @@\n"
            f"{removed}\n"
            "+    pass\n"
        )
        cat = [{"number": i, "id": t, "seconds": 0.01} for i, t in enumerate(impact._index()["tests"])]
        res = impact.select(diff, cat)
        self.assertFalse(res.get("full"), res.get("reasons"))

    def test_duckdb_sql_ws_equivalent(self):
        from selector import result_replay

        self.assertTrue(
            result_replay._duckdb_sql_ws_equivalent(
                "SELECT a, b FROM t WHERE x = 'hello  world'",
                "SELECT\n  a,\n  b\nFROM t\nWHERE\n  x = 'hello  world'",
            )
        )
        # String literal whitespace change -> NOT equivalent
        self.assertFalse(
            result_replay._duckdb_sql_ws_equivalent(
                "SELECT 'a b'",
                "SELECT 'a  b'",
            )
        )
        # Comment present -> rejected conservatively
        self.assertFalse(
            result_replay._duckdb_sql_ws_equivalent(
                "SELECT 1 -- hi",
                "SELECT 1 -- hi",
            )
        )

    def test_companion_test_method_disjoint_from_heavy_target(self):
        from selector.patch import parse

        ex_src = impact._sources()["src"]["tests/test_executor.py"].splitlines()
        idx_py = next(i for i, l in enumerate(ex_src) if "def test_py_dialect(" in l)
        # Adding an assertion inside unrelated test_py_dialect -> disjoint from ds
        d_ok = (
            "diff --git a/tests/test_executor.py b/tests/test_executor.py\n"
            "--- a/tests/test_executor.py\n"
            "+++ b/tests/test_executor.py\n"
            f"@@ -{idx_py+1},2 +{idx_py+1},3 @@\n"
            f" {ex_src[idx_py]}\n"
            f" {ex_src[idx_py+1]}\n"
            "+        self.assertTrue(True)\n"
        )
        self.assertTrue(heavy._test_file_disjoint_from_target(parse(d_ok)[0], "ds"))
        # Editing setUpClass -> MUST NOT be disjoint
        idx_su = next(i for i, l in enumerate(ex_src) if "def setUpClass(" in l)
        d_bad = (
            "diff --git a/tests/test_executor.py b/tests/test_executor.py\n"
            "--- a/tests/test_executor.py\n"
            "+++ b/tests/test_executor.py\n"
            f"@@ -{idx_su+1},2 +{idx_su+1},3 @@\n"
            f" {ex_src[idx_su]}\n"
            f" {ex_src[idx_su+1]}\n"
            "+        pass\n"
        )
        self.assertFalse(heavy._test_file_disjoint_from_target(parse(d_bad)[0], "ds"))

    def test_attr_refs_scopes_test_class_to_importers(self):
        """Editing a class attribute on a leaf test class (e.g. TestDrill.dialect)
        must only select tests in files that can import/subclass it, not global readers."""
        from selector import impact

        src = impact._sources()["src"]["tests/dialects/test_drill.py"].splitlines()
        idx = next(i for i, l in enumerate(src) if 'dialect = "drill"' in l)
        diff = (
            "diff --git a/tests/dialects/test_drill.py b/tests/dialects/test_drill.py\n"
            "--- a/tests/dialects/test_drill.py\n"
            "+++ b/tests/dialects/test_drill.py\n"
            f"@@ -{idx+1},1 +{idx+1},1 @@\n"
            f"-{src[idx]}\n"
            '+    dialect = "dRill"\n'
        )
        cat = [{"number": i, "id": tid, "seconds": 0.01} for i, tid in enumerate(impact._index()["tests"])]
        res = impact.select(diff, cat)
        self.assertFalse(res["full"])
        selected_ids = {cat[n]["id"] for n in res["required"]}
        self.assertTrue(any("test_drill.py" in tid for tid in selected_ids))
        self.assertNotIn("tests/test_generator.py::TestGenerator::test_generator_memory", selected_ids)
        self.assertLess(len(selected_ids), 20)

    def test_env_py_function_tail_slicing(self):
        """Top-level pure functions in sqlglot/executor/env.py (like intdiv) slice
        to only the TPC-DS queries that call them; decorators stay conservative."""
        from selector import heavy, impact
        from selector.patch import parse

        src = impact._sources()["src"]["sqlglot/executor/env.py"].splitlines()
        # Edit inside def cast (uncalled by TPC-DS)
        idx = next(i for i, l in enumerate(src) if "def cast(" in l)
        diff_ok = (
            "diff --git a/sqlglot/executor/env.py b/sqlglot/executor/env.py\n"
            "--- a/sqlglot/executor/env.py\n"
            "+++ b/sqlglot/executor/env.py\n"
            f"@@ -{idx+2},1 +{idx+2},2 @@\n"
            "+    _unused = 1\n"
            f" {src[idx+1]}\n"
        )
        fd_ok = parse(diff_ok)[0]
        after_ok = "\n".join(src[: idx + 1] + ["    _unused = 1"] + src[idx + 1 :]) + "\n"
        self.assertEqual(heavy.tail_cases("sqlglot/executor/env.py", after_ok, fd_ok), [])
        # Edit inside decorator null_if_any -> must remain conservative (all_cases)
        idx_dec = next(i for i, l in enumerate(src) if "def null_if_any(" in l)
        diff_dec = (
            "diff --git a/sqlglot/executor/env.py b/sqlglot/executor/env.py\n"
            "--- a/sqlglot/executor/env.py\n"
            "+++ b/sqlglot/executor/env.py\n"
            f"@@ -{idx_dec+2},1 +{idx_dec+2},2 @@\n"
            "+    _unused = 1\n"
            f" {src[idx_dec+1]}\n"
        )
        fd_dec = parse(diff_dec)[0]
        after_dec = "\n".join(src[: idx_dec + 1] + ["    _unused = 1"] + src[idx_dec + 1 :]) + "\n"
        self.assertEqual(len(heavy.tail_cases("sqlglot/executor/env.py", after_dec, fd_dec)), 99)

    def test_mixed_code_and_tpcds_expected_only_inert_for_ds(self):
        """An expected-only edit to tpc-ds.sql is unreachable by test_execute_tpcds,
        whereas a source-SQL edit MUST remain protected."""
        from selector import heavy, verify
        from selector.patch import parse

        src = (Path(verify.PRISTINE) / "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql").read_text().splitlines()
        # Find first query ending with ';' (line 26) and edit the expected SQL line after it
        sep = next(i for i, l in enumerate(src) if l.rstrip().endswith(";"))
        diff_exp = (
            "diff --git a/tests/fixtures/optimizer/tpc-ds/tpc-ds.sql b/tests/fixtures/optimizer/tpc-ds/tpc-ds.sql\n"
            "--- a/tests/fixtures/optimizer/tpc-ds/tpc-ds.sql\n"
            "+++ b/tests/fixtures/optimizer/tpc-ds/tpc-ds.sql\n"
            f"@@ -{sep+2},1 +{sep+2},1 @@\n"
            f"-{src[sep+1]}\n"
            f"+{src[sep+1]} /* changed expected */\n"
        )
        self.assertTrue(heavy._target_unreachable(parse(diff_exp)[0], "ds"))
        # Source line edit (line right before first ';') MUST NOT be unreachable
        diff_src = (
            "diff --git a/tests/fixtures/optimizer/tpc-ds/tpc-ds.sql b/tests/fixtures/optimizer/tpc-ds/tpc-ds.sql\n"
            "--- a/tests/fixtures/optimizer/tpc-ds/tpc-ds.sql\n"
            "+++ b/tests/fixtures/optimizer/tpc-ds/tpc-ds.sql\n"
            f"@@ -{sep},1 +{sep},1 @@\n"
            f"-{src[sep-1]}\n"
            f"+{src[sep-1]} /* changed source */\n"
        )
        self.assertFalse(heavy._target_unreachable(parse(diff_src)[0], "ds"))

    def test_parser_monitored_loop_guard_blocks_executed_loop(self):
        """staged.parser(forbidden=...) must raise if any monitored loop line executes."""
        import json
        import os
        import subprocess
        import sys
        import tempfile
        from selector import verify
        from selector.runtime_paths import APP_DIR

        pin_parser = Path(verify.PRISTINE) / "sqlglot/parser.py"
        lines = pin_parser.read_text().splitlines()
        idx = next(i for i, l in enumerate(lines) if "def _parse_conjunction(" in l)
        # Inject a tiny loop that WILL execute on SELECT x=x=x...
        injected = lines[: idx + 1] + ["        for _ in range(1):", "            _ = 1"] + lines[idx + 1 :]
        with tempfile.TemporaryDirectory() as work:
            verify.build_overlay(work, {"sqlglot/parser.py": "\n".join(injected) + "\n"})
            forbidden = {"sqlglot/parser.py": [idx + 3]}
            env = dict(os.environ, BLAST_APP_DIR=str(APP_DIR))
            proc = subprocess.run(
                [sys.executable, "-sB", str(Path("selector/staged.py").resolve()), "scaled", json.dumps(forbidden)],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("new loop executed", proc.stderr)

    def test_parser_scaled_allows_sql_limit_and_monitors_big_numbers(self):
        """SQL LIMIT edits in parser.py are eligible for scaled(), while big-number
        edits in p2_lines require exact-reachability monitoring (catching executed lines)."""
        from selector import heavy, impact
        from selector.patch import parse

        src = impact._sources()["src"]["sqlglot/parser.py"].splitlines()
        idx = next(i for i, l in enumerate(src) if "def _parse_conjunction(" in l)
        # SQL LIMIT comment/identifier in parser.py is NOT rejected by _parser_scaled_eligible
        d_lim = (
            "diff --git a/sqlglot/parser.py b/sqlglot/parser.py\n"
            "--- a/sqlglot/parser.py\n"
            "+++ b/sqlglot/parser.py\n"
            f"@@ -{idx+1},1 +{idx+1},2 @@\n"
            f" {src[idx]}\n"
            "+        _sql_limit = exp.Limit\n"
        )
        self.assertTrue(heavy._parser_scaled_eligible(parse(d_lim)[0]))
        # Numeric >= 50 requires allow_monitored_edits=True and produces watched lines
        d_num = (
            "diff --git a/sqlglot/parser.py b/sqlglot/parser.py\n"
            "--- a/sqlglot/parser.py\n"
            "+++ b/sqlglot/parser.py\n"
            f"@@ -{idx+1},1 +{idx+1},2 @@\n"
            f" {src[idx]}\n"
            "+        if len(self._tokens) > 20000: raise ValueError('boom')\n"
        )
        fd_num = parse(d_num)[0]
        self.assertFalse(heavy._parser_scaled_eligible(fd_num))
        self.assertTrue(heavy._parser_scaled_eligible(fd_num, allow_monitored_edits=True))
        after = "\n".join(src[: idx + 1] + ["        if len(self._tokens) > 20000: raise ValueError('boom')"] + src[idx + 1 :]) + "\n"
        self.assertIn(idx + 2, heavy._parser_edited_lines(fd_num, after))

    def test_expr_core_non_tail_attr_and_new_expr_subclass_tail_empty(self):
        """Non-tail Expr class attrs (is_data_type) and brand-new Expr subclasses
        return [] in tail_cases, while Expr.__hash__ remains conservative (99)."""
        from selector import heavy, impact
        from selector.patch import parse

        src = impact._sources()["src"]["sqlglot/expressions/core.py"].splitlines()
        idx = next(i for i, l in enumerate(src) if "is_data_type" in l)
        d_attr = (
            "diff --git a/sqlglot/expressions/core.py b/sqlglot/expressions/core.py\n"
            "--- a/sqlglot/expressions/core.py\n"
            "+++ b/sqlglot/expressions/core.py\n"
            f"@@ -{idx+1},1 +{idx+1},1 @@\n"
            f"-{src[idx]}\n"
            "+    is_data_type: t.ClassVar[bool] = True\n"
        )
        after_attr = "\n".join(src[:idx] + ["    is_data_type: t.ClassVar[bool] = True"] + src[idx + 1 :]) + "\n"
        self.assertEqual(heavy.tail_cases("sqlglot/expressions/core.py", after_attr, parse(d_attr)[0]), [])
        # Brand-new Expr subclass appended to sqlglot/expressions/query.py
        qsrc = impact._sources()["src"]["sqlglot/expressions/query.py"].splitlines()
        d_cls = (
            "diff --git a/sqlglot/expressions/query.py b/sqlglot/expressions/query.py\n"
            "--- a/sqlglot/expressions/query.py\n"
            "+++ b/sqlglot/expressions/query.py\n"
            f"@@ -{len(qsrc)},1 +{len(qsrc)},3 @@\n"
            f" {qsrc[-1]}\n"
            "+class BrandNewDummyExpr(Expr):\n"
            "+    arg_types = {'this': False}\n"
        )
        after_cls = "\n".join(qsrc + ["class BrandNewDummyExpr(Expr):", "    arg_types = {'this': False}"]) + "\n"
        self.assertEqual(heavy.tail_cases("sqlglot/expressions/query.py", after_cls, parse(d_cls)[0]), [])

    def test_docstring_only_tail_cases_empty(self):
        """Docstring-only edits to sqlglot/executor/env.py return [] in tail_cases."""
        from selector import heavy, impact
        from selector.patch import parse

        src = impact._sources()["src"]["sqlglot/executor/env.py"].splitlines()
        d_doc = (
            "diff --git a/sqlglot/executor/env.py b/sqlglot/executor/env.py\n"
            "--- a/sqlglot/executor/env.py\n"
            "+++ b/sqlglot/executor/env.py\n"
            "@@ -1,1 +1,2 @@\n"
            '+"""Module docstring."""\n'
            f" {src[0]}\n"
        )
        after = '"""Module docstring."""\n' + "\n".join(src) + "\n"
        self.assertEqual(heavy.tail_cases("sqlglot/executor/env.py", after, parse(d_doc)[0]), [])


if __name__ == "__main__":
    unittest.main()
