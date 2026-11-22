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


def main():
    group = sys.argv[1]
    if group == "record":
        rows, tail = executor(True, None, True)
        BASE.write_text(json.dumps(dict(ds=rows, tail=tail, parser=parser())))
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
