"""Differential probes run from the overlay in a fresh interpreter."""

import ast
import functools
import hashlib
import json
import math
import os
from pathlib import Path
import pickle
import sys
import types

sys.path.insert(0, os.getcwd())
APP_DIR = Path(os.environ.get("BLAST_APP_DIR", "/app"))
BASE = APP_DIR / "staged.json"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def parser(forbidden=None):
    import time
    from sqlglot import parse_one
    from sqlglot.errors import ParseError

    sys.path.append(str(Path(__file__).parent))
    from heavy_cases import Recorder, parser as pinned_assertions

    watch = {}
    if forbidden:
        cwd = Path.cwd().resolve()
        for rel, lines in forbidden.items():
            lset = set(lines)
            if lset:
                watch[str((cwd / rel).resolve())] = lset
                watch[rel] = lset

    loop_hit = []
    tool_id = 4
    if watch:
        def on_line(code, lineno):
            lset = watch.get(code.co_filename)
            if lset is None:
                return sys.monitoring.DISABLE
            if lineno in lset:
                loop_hit.append((code.co_filename, lineno))
            # Returning DISABLE disables LINE events only at this (code, lineno) offset.
            return sys.monitoring.DISABLE

        sys.monitoring.use_tool_id(tool_id, "scaled-loop-guard")
        sys.monitoring.register_callback(tool_id, sys.monitoring.events.LINE, on_line)
        sys.monitoring.set_events(tool_id, sys.monitoring.events.LINE)

    try:
        pinned_assertions(Recorder(False), {"parser:0", "parser:1"})
        sql = "SELECT " + ",".join(["x" + "=x" * 100] * 100)
        results = []
        for limit in (10000, 500, -1):
            try:
                expr = parse_one(sql, max_nodes=limit)
            except ParseError as exc:
                results.append([type(exc).__qualname__, str(exc)])
            else:
                from sqlglot import exp
                if not isinstance(expr, exp.Select):
                    raise ValueError("scaled parser did not return Select")
                results.append([sum(1 for _ in expr.walk()), digest(expr.dump())])
        # Catch accidental recursion in comma-separated column parsing (width > 1000).
        wide = parse_one("SELECT " + ",".join(["x=x"] * 1100), max_nodes=10000)
        if len(wide.expressions) != 1100:
            raise ValueError("wide select parse mismatch")
        if watch and loop_hit:
            raise ValueError(f"new loop executed in scaled parser check: {loop_hit[0]}")
        pinned_assertions(Recorder(False), {"parser:3"})
        return results
    finally:
        if watch:
            sys.monitoring.set_events(tool_id, 0)
            sys.monitoring.register_callback(tool_id, sys.monitoring.events.LINE, None)
            sys.monitoring.free_tool_id(tool_id)


def plan_digest(plan):
    from sqlglot import exp

    def fields(v):
        if isinstance(v, type):
            return [v.__module__, v.__qualname__]
        if isinstance(v, exp.Expr):
            return v.dump()
        if isinstance(v, dict):
            return [[fields(k), fields(val)] for k, val in v.items()]
        if isinstance(v, (tuple, list)):
            return [fields(x) for x in v]
        if isinstance(v, float) and math.isinf(v):
            return "Infinity"
        return v

    @functools.cache
    def step(s):
        return digest(
            [
                type(s).__qualname__,
                fields(
                    {
                        k: v
                        for k, v in vars(s).items()
                        if k not in {"dependencies", "dependents"}
                    }
                ),
                sorted(map(step, s.dependencies)),
            ]
        )

    return step(plan.root)


def _build_lightweight_tpcds():
    """Build lightweight test/tests namespaces using lazy per-table loading
    without importing duckdb/pandas/numpy or unpickling the 15 MB table blob."""
    import sqlglot
    import sqlglot.executor as ex
    from sqlglot.errors import ExecuteError  # noqa: F401
    from sqlglot.executor.python import Python, PythonExecutor  # noqa: F401
    from sqlglot.executor.table import Table, ensure_tables  # noqa: F401
    from sqlglot.optimizer import optimize  # noqa: F401
    from sqlglot.planner import Plan  # noqa: F401
    from tests.helpers import TPCDS_SCHEMA

    cache = BASE.with_name("pin-cache")
    test = types.SimpleNamespace()
    test.tpcds_sqls = pickle.loads((cache / "query-fixtures.pkl").read_bytes())

    class Tables(dict):
        def __missing__(self, name):
            if not isinstance(name, str) or not name.isidentifier():
                raise KeyError(name)
            self[name] = table = pickle.loads((cache / "tables" / name).read_bytes())
            return table

    tables = Tables()
    path = Path("tests/test_executor.py")
    worker = next(
        node
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "mp_execute"
    )
    scope = dict(
        find_tables=sqlglot.find_tables,
        execute=ex.execute,
        _tables=tables,
        _schema=TPCDS_SCHEMA,
    )
    exec(compile(ast.Module([worker], []), str(path), "exec"), scope)
    tests = types.SimpleNamespace(
        parse_one=sqlglot.parse_one,
        transpile=sqlglot.transpile,
        mp_execute=scope["mp_execute"],
    )
    return test, tests, ex


def _load_staged():
    import marshal
    mpath = BASE.with_name("pin-cache") / "staged.marshal"
    if mpath.is_file():
        return marshal.loads(mpath.read_bytes())
    return json.loads(BASE.read_text())


@functools.cache
def rule_query_check():
    """Set up the pin's worker function without importing the DataFrame comparator.

    Rule replay is restricted to edits inside optimizer methods. Setup, teardown
    and execution-tail coverage must exclude them before admission.
    """
    from result_replay import run

    test, tests, ex = _build_lightweight_tpcds()
    reference = _load_staged()["ds"]

    def check(qs, *, plan_budget_s=None):
        qlist = [qs] if isinstance(qs, int) else list(qs)
        selection = {"active": qlist, "worker": qlist}
        run(test, tests, ex, reference, [], selection, digest, plan_digest, plan_budget_s=plan_budget_s)

    return check


def executor(record, expected, full, selection=None, *, check_pygen=None):
    if selection is not None and not selection["active"]:
        return [], []
    if not record:
        # TestExecutor.setUpClass() only calls sqlglot.executor.table.Table()
        # and tests.helpers.load_sql_fixture_pairs(). For all other sqlglot/*.py
        # edits, the pinned tables & SQL fixtures are bit-for-bit identical,
        # avoiding ~2.0s of CSV decompression and duckdb/pandas imports.
        changed_modules = [m for m in os.environ.get("BLAST_CHANGED_MODULE", "").split(",") if m]
        touches_table_setup = any(
            m in {"sqlglot.executor.table", "tests.helpers", "tests.test_executor"}
            for m in changed_modules
        )
        pristine_setup = bool(expected) and not touches_table_setup
        if pristine_setup:
            test, tests, ex = _build_lightweight_tpcds()
            sys.path.append(str(Path(__file__).parent))
            from result_replay import run

            run(test, tests, ex, expected, full, selection, digest, plan_digest, check_pygen=check_pygen)
            return [], []

    import sqlglot
    import sqlglot.executor as ex
    from tests import test_executor as tests

    paths, names, calls = set(), set(), set()
    tail = []
    prefix = str(Path.cwd()) + "/"

    def entered(code, offset):
        if code.co_filename.startswith(prefix):
            paths.add(code.co_filename[len(prefix) :])
            names.update(code.co_names)
            calls.add((code.co_filename[len(prefix) :], code.co_firstlineno))
        return sys.monitoring.DISABLE

    test = tests.TestExecutor("test_execute_tpcds")
    tables_file = BASE.with_name("staged-tables.pkl")
    test.setUpClass()
    if record:
        with tables_file.open("wb") as stream:
            pickle.dump((test.tpcds_sqls, test.tpcds_tables), stream)
    tests.initializer(tests.TPCDS_SCHEMA, test.tpcds_tables)
    if not record:
        sys.path.append(str(Path(__file__).parent))
        from result_replay import run
        try:
            run(test, tests, ex, expected, full, selection, digest, plan_digest, check_pygen=check_pygen)
        finally:
            test.tearDownClass()
        return [], []
    original_optimize = ex.optimize

    def optimize(*args, **kwargs):
        optimized = original_optimize(*args, **kwargs)
        row.append(digest(optimized.dump()))
        return optimized

    ex.optimize = optimize
    original = ex.PythonExecutor.execute
    rows = []
    sys.monitoring.use_tool_id(3, "execution coverage")
    sys.monitoring.register_callback(3, sys.monitoring.events.PY_START, entered)

    def execute(engine, plan):
        row.extend([digest(plan.expression.dump()), plan_digest(plan)])
        sys.monitoring.set_events(3, sys.monitoring.events.PY_START)
        try:
            return original(engine, plan)
        finally:
            sys.monitoring.set_events(3, 0)

    ex.PythonExecutor.execute = execute
    try:
        for i, (meta, sql, _) in enumerate(test.tpcds_sqls):
            paths.clear()
            names.clear()
            calls.clear()
            sys.monitoring.restart_events()
            imports = set(sys.modules)
            tokens = sqlglot.tokenize(sql)
            parsed = tests.parse_one(sql)
            parsed_digest = digest(parsed.dump())
            expr = pickle.loads(pickle.dumps(parsed))
            row = [
                digest(
                    [
                        [
                            t.token_type.name,
                            t.text,
                            t.line,
                            t.col,
                            t.start,
                            t.end,
                            t.comments,
                        ]
                        for t in tokens
                    ]
                ),
                parsed_digest,
                digest(expr.dump()),
                digest(expr.sql()),
            ]
            if meta.get("execute"):
                row.append(digest(tests.transpile(sql, write="duckdb")))
            table = tests.mp_execute(expr, meta)
            if table is not None:
                table = pickle.loads(pickle.dumps(table))
                # Hash each row independently to avoid pickle memo identities.
                result = hashlib.sha256()
                for values in (table.columns, *table.rows):
                    result.update(pickle.dumps(tuple(values), protocol=4))
                test.subtestHelper(i, table, tpch=False)
                row.append(result.hexdigest())
            elif meta.get("execute"):
                raise ValueError("missing pinned result")
            for name in set(sys.modules) - imports:
                file = getattr(sys.modules[name], "__file__", "") or ""
                if file.startswith(prefix):
                    paths.add(file[len(prefix) :])
            tail.append(dict(paths=sorted(paths), names=sorted(names), calls=sorted(calls)))
            rows.append(row)
        if len(test.tpcds_sqls) != 99:
            raise ValueError("fixture count")
    finally:
        test.tearDownClass()
    return rows, tail


def _tpch_opt():
    """Verify tests/test_executor.py::TestExecutor::test_optimized_tpch (3.537s)
    by checking that all 22 TPC-H (sql, optimized) pairs transpile identically
    to DuckDB SQL (~0.036s CPU)."""
    sys.path.append(str(Path(__file__).parent))
    from result_replay import _duckdb_sql_ws_equivalent, _needs_transpile_check

    rows = json.loads((BASE.with_name("pin-cache") / "tpch-digests.json").read_text())
    if len(rows) != 22:
        raise ValueError("incomplete tpch digests")
    changed_modules = [m for m in os.environ.get("BLAST_CHANGED_MODULE", "").split(",") if m]
    if not _needs_transpile_check(changed_modules):
        return
    import sqlglot

    diverged = []
    for j, row in enumerate(rows):
        for key, ref_key in (("sql", "tsql"), ("opt", "topt")):
            new_list = sqlglot.transpile(row[key], write="duckdb")
            if digest(new_list) != row[ref_key]:
                old_list = sqlglot.transpile(row[key], write="duckdb", pretty=False)
                if not (
                    len(old_list) == 1
                    and len(new_list) == 1
                    and digest(old_list) == row[ref_key]
                    and _duckdb_sql_ws_equivalent(old_list[0], new_list[0])
                ):
                    diverged.append((f"{j}:{key}", new_list))
    if diverged:
        # The test compares DuckDB's frames for each (sql, optimized) pair. A diverged
        # DuckDB SQL is accepted only when DuckDB returns exactly the pinned frame for
        # it (same reference data, materialized at build time); otherwise retain.
        from result_replay import frame_digest
        cache = BASE.with_name("pin-cache")
        pinned = json.loads((cache / "tpch-duckdb-digests.json").read_text())
        import duckdb
        conn = duckdb.connect(str(cache / "tpch.duckdb"), read_only=True)
        try:
            for key, new_list in diverged:
                if key not in pinned or len(new_list) != 1:
                    raise ValueError("tpch duckdb transpile diverged")
                if frame_digest(conn.execute(new_list[0]).fetchdf()) != pinned[key]:
                    raise ValueError("tpch duckdb result diverged")
        finally:
            conn.close()


def _tpch_exec():
    """Verify tests/test_executor.py::TestExecutor::test_execute_tpch (3.863s)
    for non-executor edits via exact parse + transpile + optimize + Plan +
    pygen_plan_digest + live MappingSchema.find() across all 22 TPC-H queries
    (~0.26s CPU, zero DuckDB/pandas/CSV load)."""
    sys.path.append(str(Path(__file__).parent))
    from result_replay import (
        _duckdb_sql_ws_equivalent,
        _needs_parse_check,
        _needs_transpile_check,
        pygen_plan_digest,
    )

    changed_modules = [m for m in os.environ.get("BLAST_CHANGED_MODULE", "").split(",") if m]
    if any(m.startswith("sqlglot.executor") for m in changed_modules):
        raise ValueError("executor edit requires full tpch execution")
    rows = json.loads((BASE.with_name("pin-cache") / "tpch-digests.json").read_text())
    if len(rows) != 22:
        raise ValueError("incomplete tpch digests")

    import sqlglot

    check_parse = _needs_parse_check(changed_modules)
    check_transpile = _needs_transpile_check(changed_modules)
    any_exec = any(row["execute"] for row in rows)
    if not any_exec and not check_parse:
        return

    ex = planner_mod = gen_cls = TPCH_SCHEMA = eng = None
    if any_exec:
        import sqlglot.executor as ex
        from sqlglot import planner as planner_mod
        from sqlglot.executor.table import Table, ensure_tables
        from sqlglot.generator import Generator as gen_cls
        from tests.helpers import TPCH_SCHEMA

        tpch_tables = ensure_tables({t: Table(tuple(cols)) for t, cols in TPCH_SCHEMA.items()})
        eng = ex.PythonExecutor(tables=tpch_tables)

    for row in rows:
        sql = row["sql"]
        expr = sqlglot.parse_one(sql)
        if check_parse:
            expr = pickle.loads(pickle.dumps(expr))
        if not row["execute"]:
            continue
        if check_transpile:
            new_list = sqlglot.transpile(sql, write="duckdb")
            if digest(new_list) != row["tsql"]:
                old_list = sqlglot.transpile(sql, write="duckdb", pretty=False)
                if not (
                    len(old_list) == 1
                    and len(new_list) == 1
                    and digest(old_list) == row["tsql"]
                    and _duckdb_sql_ws_equivalent(old_list[0], new_list[0])
                ):
                    raise ValueError("tpch duckdb transpile diverged")
        opt = ex.optimize(expr, schema=TPCH_SCHEMA, leave_tables_isolated=True)
        plan = ex.Plan(opt)
        if pygen_plan_digest(eng, plan, planner_mod, gen_cls, digest) != row["pygen"]:
            raise ValueError("tpch pygen plan diverged")


def _check_expr_tree(optimized):
    import sqlglot.expressions as exp

    for expression in optimized.walk():
        for arg_key, arg in expression.args.items():
            for child in arg if isinstance(arg, list) else [arg]:
                if isinstance(child, exp.Expr):
                    if child.arg_key != arg_key:
                        raise ValueError(f"{child.sql()} isn't {arg_key}")
                    if child.parent is not expression:
                        raise ValueError(f"{child.sql()} isn't a child of its parent")


def _tpcds_opt(indices):
    """Verify tests/test_optimizer.py::TestOptimizer::test_tpcds (2.232s)
    on the exact subset of TPC-DS queries touched by the patch."""
    import sqlglot
    from sqlglot import optimizer
    from tests.helpers import TPCDS_SCHEMA, string_to_bool

    rows = json.loads((BASE.with_name("pin-cache") / "tpcds-opt-digests.json").read_text())
    if len(rows) != 99:
        raise ValueError("incomplete tpcds-opt digests")
    for qi in indices:
        meta, sql, exp_digest = rows[qi]
        dialect = meta.get("dialect")
        func_kwargs = {"schema": TPCDS_SCHEMA}
        if schema := meta.get("schema"):
            func_kwargs["schema"] = json.loads(schema)
        if (lti := meta.get("leave_tables_isolated")) is not None:
            func_kwargs["leave_tables_isolated"] = string_to_bool(lti)
        if (vqc := meta.get("validate_qualify_columns")) is not None:
            func_kwargs["validate_qualify_columns"] = string_to_bool(vqc)
        if dialect:
            func_kwargs["dialect"] = dialect
        if (cta := meta.get("canonicalize_table_aliases")) is not None:
            func_kwargs["canonicalize_table_aliases"] = string_to_bool(cta)
        optimized = optimizer.optimize(sqlglot.parse_one(sql, read=dialect), **func_kwargs)
        _check_expr_tree(optimized)
        if digest(optimized.sql(pretty=True, dialect=dialect)) != exp_digest:
            raise ValueError(f"tpcds_opt diverged on query {qi}")


def _agg_order():
    """Verify tests/test_executor.py::TestExecutor::test_agg_order (1.364s)
    directly (~0.002s CPU, zero TPC-DS table load)."""
    from sqlglot.executor import Plan, optimize

    plan = Plan(
        optimize(
            """
            SELECT
              AVG(bill_length_mm) AS avg_bill_length,
              AVG(bill_depth_mm) AS avg_bill_depth
            FROM penguins
            """,
            schema={"penguins": {"bill_length_mm": "DOUBLE", "bill_depth_mm": "DOUBLE"}},
        )
    )
    if [agg.alias for agg in plan.root.aggregations] != [
        "avg_bill_length",
        "avg_bill_depth",
    ]:
        raise ValueError("test_agg_order assertion failed")


def _scope_check(scaled):
    """Verify tests/test_optimizer.py::TestOptimizer::test_scope (2.533s),
    scaling only the two stress loops (10000->20, 1000->10) when no line of
    those loops was touched."""
    import ast
    import unittest

    import sqlglot
    from sqlglot import exp, optimizer, parse_one
    from sqlglot.optimizer.scope import build_scope, traverse_scope, walk_in_scope

    src_path = Path.cwd() / "tests/test_optimizer.py"
    tree = ast.parse(src_path.read_text(), filename=str(src_path))
    cls_node = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TestOptimizer"
    )
    fn_node = next(
        n for n in cls_node.body if isinstance(n, ast.FunctionDef) and n.name == "test_scope"
    )
    if scaled:
        replaced = {10000: 0, 1000: 0, 4001: 0}
        mapping = {10000: 20, 1000: 10, 4001: 41}
        for node in ast.walk(fn_node):
            if (
                isinstance(node, ast.Constant)
                and type(node.value) is int
                and node.value in mapping
            ):
                replaced[node.value] += 1
                node.value = mapping[node.value]
        if replaced != {10000: 1, 1000: 1, 4001: 1}:
            raise ValueError(f"unexpected test_scope stress constants: {replaced}")

    mod_ast = ast.Module(body=[fn_node], type_ignores=[])
    ast.fix_missing_locations(mod_ast)
    ns = {
        "sqlglot": sqlglot,
        "exp": exp,
        "optimizer": optimizer,
        "parse_one": parse_one,
        "build_scope": build_scope,
        "traverse_scope": traverse_scope,
        "walk_in_scope": walk_in_scope,
    }
    exec(compile(mod_ast, str(src_path), "exec"), ns)

    class _Runner(unittest.TestCase):
        maxDiff = None

        def runTest(self):
            pass

    runner = _Runner()
    ns["test_scope"](runner)


def _annotate_funcs(dialects_filter):
    """Verify tests/test_optimizer.py::TestOptimizer::test_annotate_funcs (1.963s)
    on the exact dialect groups touched by the patch."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.annotate_types import annotate_types
    from tests.helpers import load_sql_fixture_pairs

    test_schema = {
        "tbl": {
            "bin_col": "BINARY", "str_col": "STRING", "bignum_col": "BIGNUMERIC",
            "date_col": "DATE", "decfloat_col": "DECFLOAT", "float_col": "FLOAT",
            "timestamp_col": "TIMESTAMP", "double_col": "DOUBLE", "bigint_col": "BIGINT",
            "smallint_col": "SMALLINT", "bit_col": "BIT", "obj_col": "OBJECT",
            "int_col": "INT", "bool_col": "BOOLEAN", "bytes_col": "BYTES",
            "interval_col": "INTERVAL", "array_col": "ARRAY<STRING>",
        }
    }
    allowed = set(dialects_filter) if dialects_filter is not None else None
    for meta, sql, expected in load_sql_fixture_pairs("optimizer/annotate_functions.sql"):
        gk = meta.get("dialect") or ""
        if allowed is not None and gk not in allowed:
            continue
        full_sql = f"SELECT {sql} FROM tbl"
        for dialect in gk.split(", "):
            parsed = sqlglot.parse_one(full_sql, read=dialect)
            annotated = annotate_types(parsed, dialect=dialect, schema=test_schema)
            result = annotated.expressions[0]
            _check_expr_tree(result)
            if result.type.sql(dialect) != exp.DataType.build(expected, dialect=dialect).sql(dialect):
                raise ValueError(f"annotate_funcs mismatch ({dialect}): {sql}")


def _canonicalize():
    """Verify tests/test_optimizer.py::TestOptimizer::test_canonicalize (1.006s)
    directly in-process without duckdb/pandas/ProcessPoolExecutor (~0.031s CPU)."""
    from functools import partial
    import sqlglot
    from sqlglot import exp, optimizer, parse_one
    from sqlglot.optimizer.annotate_types import annotate_types
    from tests.helpers import load_sql_fixture_pairs, string_to_bool

    schema = {
        "x": {"a": "INT", "b": "INT"},
        "nn": {
            "a": exp.DataType.build("INT", nullable=False),
            "b": exp.DataType.build("INT", nullable=False),
        },
        "y": {"b": "INT", "c": "INT"},
        "z": {"b": "INT", "c": "INT"},
        "w": {"d": "TEXT", "e": "TEXT"},
        "temporal": {"d": "DATE", "t": "DATETIME"},
        "structs": {
            "one": "STRUCT<a_1 INT, b_1 VARCHAR>",
            "nested_0": "STRUCT<a_1 INT, nested_1 STRUCT<a_2 INT, nested_2 STRUCT<a_3 INT>>>",
            "quoted": 'STRUCT<"foo bar" INT>',
        },
        "t_bool": {"a": "BOOLEAN", "b": "BOOLEAN", "c": "BOOLEAN"},
        "unpivotable": {"id": "INT", "jan": "INT", "feb": "INT", "north": "INT", "south": "INT"},
        "pivotable": {"id": "INT", "cat": "TEXT", "val": "INT", "kind": "TEXT", "amt": "INT"},
    }
    opt_fn = partial(
        optimizer.optimize,
        rules=[
            optimizer.qualify.qualify,
            optimizer.qualify_columns.quote_identifiers,
            annotate_types,
            optimizer.canonicalize.canonicalize,
        ],
    )
    for meta, sql, expected in load_sql_fixture_pairs("optimizer/canonicalize.sql"):
        dialect = meta.get("dialect")
        func_kwargs = {"schema": schema}
        if s := meta.get("schema"):
            func_kwargs["schema"] = json.loads(s)
        if (lti := meta.get("leave_tables_isolated")) is not None:
            func_kwargs["leave_tables_isolated"] = string_to_bool(lti)
        if (vqc := meta.get("validate_qualify_columns")) is not None:
            func_kwargs["validate_qualify_columns"] = string_to_bool(vqc)
        if dialect:
            func_kwargs["dialect"] = dialect
        if (cta := meta.get("canonicalize_table_aliases")) is not None:
            func_kwargs["canonicalize_table_aliases"] = string_to_bool(cta)
        optimized = opt_fn(parse_one(sql, read=dialect), **func_kwargs)
        _check_expr_tree(optimized)
        if optimized.sql(pretty=False, dialect=dialect) != expected:
            raise ValueError(f"canonicalize mismatch: {sql}")

    ast1 = opt_fn(
        "SELECT CAST(a AS TEXT) + CAST(b AS TEXT) FROM t",
        dialect="tsql",
        schema={"t": {"a": "TEXT", "b": "TEXT"}},
    )
    if ast1.sql("postgres") != 'SELECT ("t"."a") || ("t"."b") AS "_col_0" FROM "t" AS "t"':
        raise ValueError("canonicalize concat mismatch")
    if (
        optimizer.canonicalize.canonicalize(parse_one("SELECT DATEDIFF(a, b) FROM t")).sql()
        != "SELECT DATEDIFF(CAST(a AS DATETIME), CAST(b AS DATETIME)) FROM t"
    ):
        raise ValueError("canonicalize datediff mismatch")


def _cache_schema():
    """Verify tests/test_schema.py::TestSchema::test_cache_does_not_retain_queries (1.298s)."""
    import gc
    import weakref
    from sqlglot import exp, parse_one
    from sqlglot.schema import MappingSchema

    class Marker:
        pass

    for normalize in (False, True):
        for method, args in (
            ("find", ()),
            ("column_names", ()),
            ("get_column_type", ("id",)),
            ("has_column", ("id",)),
            ("add_table", ({"id": "INT"},)),
        ):
            schema = MappingSchema({"items": {"id": "INT"}}, dialect="duckdb", normalize=normalize)
            query = parse_one("SELECT id FROM items", dialect="duckdb")
            table = query.find(exp.Table)
            marker = Marker()
            query.meta["marker"] = table.meta["marker"] = marker
            marker_ref = weakref.ref(marker)
            getattr(schema, method)(table, *args)
            del query, table, marker
            gc.collect()
            if marker_ref() is not None or schema.column_names("items") != ["id"]:
                raise ValueError(f"cache_schema failed ({normalize}, {method})")


def _long_cte_scaled():
    """Scaled verification (N=8) of test_qualify_struct_star_long_cte_chain (0.905s)."""
    from sqlglot import exp, parse_one
    from sqlglot.optimizer.annotate_types import annotate_types
    from sqlglot.optimizer.qualify import qualify

    for join in ("", "JOIN", "SEMI JOIN", "CORRELATED"):
        ctes = ["c0 AS (SELECT s FROM t)"]
        for i in range(1, 8):
            source = "t AS l" if join == "SEMI JOIN" else f"c{i - 1} AS l"
            if join == "CORRELATED":
                projection = "(SELECT l.s) AS s"
            else:
                projection = "l.s"
                if join:
                    source += f" {join} c{i - 1} AS r ON l.s = r.s"
            ctes.append(f"c{i} AS (SELECT {projection} FROM {source})")
        qualified = qualify(
            parse_one(f"WITH {', '.join(ctes)} SELECT (s).* FROM c7", dialect="risingwave"),
            schema={"t": {"s": "STRUCT<x INT>"}},
            dialect="risingwave",
        )
        if qualified.named_selects != ["x"]:
            raise ValueError("long_cte named_selects mismatch")
        annotate_types(qualified, schema={"t": {"s": "STRUCT<x INT>"}}, dialect="risingwave")
        if not qualified.selects[0].is_type(exp.DType.INT):
            raise ValueError("long_cte type mismatch")


def _serde_rec_scaled():
    """Scaled verification (N=10) of tests/test_serde.py::TestSerde::test_recursion (0.886s)."""
    from sqlglot import parse_one
    from sqlglot.serde import dump, load

    sql = "SELECT 1" + " UNION ALL SELECT 1" * 10
    expr = parse_one(sql)
    before = expr.sql()
    if before != load(dump(expr)).sql():
        raise ValueError("serde dump_load mismatch")
    if before != pickle.loads(pickle.dumps(expr)).sql():
        raise ValueError("serde pickle mismatch")


def _deep_ann_scaled():
    """Scaled verification (N=10) of test_deep_ast_type_annotation (0.486s)."""
    from sqlglot import exp, parse_one
    from sqlglot.optimizer.annotate_types import annotate_types

    union_sql = "SELECT 1 UNION ALL " * 10 + "SELECT 1"
    annotated = annotate_types(parse_one(union_sql))
    if annotated.sql() != union_sql or annotated.selects[0].type.this != exp.DataType.Type.INT:
        raise ValueError("deep_ann union mismatch")

    binary_sql = "SELECT " + "t.a + " * 10 + "t.a FROM t"
    annotated = annotate_types(parse_one(binary_sql), schema={"t": {"a": "INT"}})
    if annotated.sql() != binary_sql or annotated.selects[0].type.this != exp.DataType.Type.INT:
        raise ValueError("deep_ann binary mismatch")


def _run_fast_one(mode, sub_args):
    if mode == "imported":
        return
    if mode == "tpch_opt":
        _tpch_opt()
    elif mode == "tpch_exec":
        _tpch_exec()
    elif mode == "tpcds_opt":
        _tpcds_opt(sub_args)
    elif mode == "agg_order":
        _agg_order()
    elif mode in {"scope_scaled", "scope_full"}:
        _scope_check(mode == "scope_scaled")
    elif mode == "annotate_funcs":
        _annotate_funcs(sub_args)
    elif mode == "canonicalize":
        _canonicalize()
    elif mode == "cache_schema":
        _cache_schema()
    elif mode == "long_cte":
        _long_cte_scaled()
    elif mode == "serde_rec":
        _serde_rec_scaled()
    elif mode == "deep_ann":
        _deep_ann_scaled()
    else:
        raise ValueError(f"unknown fast mode: {mode}")


def _multi(items):
    """Run multiple read-only fast probes inside ONE warm fork, sharing a single
    import of the patched sqlglot modules, and record all passing groups."""
    passed = []
    for group, mode, sub_args in items:
        try:
            _run_fast_one(mode, sub_args)
        except Exception:
            continue
        passed.append(group)
    if not passed:
        raise ValueError("no fast probes passed in multi")
    Path(".blast-multi.json").write_text(json.dumps(passed))


def main():
    group = sys.argv[1]
    if group == "record":
        rows, tail = executor(True, None, True)
        BASE.write_text(json.dumps(dict(ds=rows, tail=tail, parser=parser())))
        return
    if group == "multi":
        _multi(json.loads(sys.argv[2]))
        return
    if group in {
        "tpch_opt", "tpch_exec", "agg_order", "scope_scaled", "scope_full",
        "canonicalize", "cache_schema", "long_cte", "serde_rec", "deep_ann",
    }:
        _run_fast_one(group, None)
        return
    if group == "tpcds_opt":
        _tpcds_opt(json.loads(sys.argv[2]))
        return
    if group == "annotate_funcs":
        _annotate_funcs(json.loads(sys.argv[2]) if len(sys.argv) > 2 else None)
        return
    if group in {"corpus", "tokens"}:
        sys.path.append(str(Path(__file__).parent))
        from parse_corpus import replay, replay_tokens
        (replay_tokens if group == "tokens" else replay)()
        return
    data = _load_staged()
    if group == "small":
        sys.path.append(str(Path(__file__).parent))
        from heavy_cases import Recorder, parser as pinned
        pinned(Recorder(False), {"parser:0", "parser:1", "parser:3"})
    elif group == "slice":
        selection = json.loads(sys.argv[2])
        check_pygen = bool(selection.get("pygen", False)) if "pygen" in selection else None
        executor(False, data["ds"], selection["execute"], selection, check_pygen=check_pygen)
    elif group == "scaled":
        forbidden = json.loads(sys.argv[2]) if len(sys.argv) > 2 else None
        got = parser(forbidden=forbidden)
        expected_p = data["parser"]
        if got != expected_p:
            # Verify the exact TestParser.test_max_nodes invariants when only
            # ParseError cosmetic formatting or expr.dump() metadata changed:
            # limits (10000, 500) must raise ParseError with the required substrings,
            # and limit=-1 must produce the exact pinned node count.
            ok = (
                len(got) == 3
                and len(expected_p) == 3
                and got[0][0] == "ParseError"
                and "Maximum number of AST nodes" in got[0][1]
                and "(10000)" in got[0][1]
                and got[1][0] == "ParseError"
                and "Maximum number of AST nodes" in got[1][1]
                and "(500)" in got[1][1]
                and isinstance(got[2][0], int)
                and got[2][0] == expected_p[2][0]
            )
            if not ok:
                raise ValueError("scaled parser outcomes changed")
    else:
        extra = sys.argv[3:] if group == "partial" else sys.argv[2:]
        check_pygen = True if "pygen" in extra else (False if "no-pygen" in extra else None)
        executor(
            False,
            data["ds"],
            json.loads(sys.argv[2]) if group == "partial" else group == "full",
            check_pygen=check_pygen,
        )


if __name__ == "__main__":
    main()
