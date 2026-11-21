"""Differential probes run from the overlay in a fresh interpreter."""

import functools
import hashlib
import json
import math
import os
from pathlib import Path
import pickle
import sys

sys.path.insert(0, os.getcwd())
BASE = Path("/app/staged.json")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def parser():
    from sqlglot import parse_one
    from sqlglot.errors import ParseError

    sys.path.append(str(Path(__file__).parent))
    from heavy_cases import Recorder, parser as pinned_assertions

    pinned_assertions(Recorder(False), {"parser:0", "parser:1"})
    sql = "SELECT " + ",".join(["x" + "=x" * 100] * 100)
    results = []
    for limit in (10000, 500, -1):
        try:
            expr = parse_one(sql, max_nodes=limit)
        except ParseError as exc:
            results.append([type(exc).__qualname__, str(exc)])
        else:
            results.append([sum(1 for _ in expr.walk()), digest(expr.dump())])
    pinned_assertions(Recorder(False), {"parser:3"})
    return results


def plan_digest(plan, executable=False):
    from sqlglot import exp

    def fields(v):
        if isinstance(v, type):
            return [v.__module__, v.__qualname__]
        if isinstance(v, exp.Expr):
            rows = v.dump()
            if executable:
                # Keep type, comments and semantic metadata. Source offsets
                # locate input tokens and do not participate in evaluation.
                for row in rows:
                    if "m" in row:
                        row["m"] = {k: val for k, val in row["m"].items()
                                    if k not in {"line", "col", "start", "end"}}
                        if not row["m"]:
                            del row["m"]
            return rows
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

    if executable:
        return digest([step(plan.root), fields(plan.ctes)])
    return step(plan.root)


def execution_key(plan):
    return plan_digest(plan, executable=True)


@functools.cache
def execution_inputs():
    import gzip
    with gzip.open(Path(__file__).parent / "maps/executor-inputs.pkl.gz", "rb") as stream:
        return pickle.load(stream)


@functools.cache
def execution_keys():
    return json.loads((Path(__file__).parent / "maps/plan-keys.json").read_text())


@functools.cache
def reference_rows():
    return json.loads(BASE.read_text())["ds"]


def check_optimized(i, expression):
    from phase import mark
    from executor_replay import full_table
    from result_replay import table_digest
    from sqlglot.executor import PythonExecutor, Plan, ensure_tables

    mark("plan")
    # execute() eagerly renders the debug SQL even with debug logging disabled.
    expression.sql(pretty=True)
    plan = Plan(expression)
    if execution_key(plan) == execution_keys()[str(i)]:
        return
    plans = execution_inputs()[0]
    _, names = pickle.loads(plans[i])
    mark("execute")
    engine = PythonExecutor(tables=ensure_tables({n: full_table(n) for n in names}))
    from query_budget import query_budget
    with query_budget(i):
        table = pickle.loads(pickle.dumps(engine.execute(plan)))
    if table_digest(table) != reference_rows()[i][-1]:
        raise ValueError("full fixture result changed")


@functools.cache
def rule_query_check():
    """Set up the pin's worker function without importing the DataFrame comparator.

    Rule replay is restricted to edits inside optimizer methods. Setup, teardown
    and execution-tail coverage must exclude them before admission.
    """
    import ast
    import types
    import sqlglot
    import sqlglot.executor as ex
    from tests.helpers import TPCDS_SCHEMA
    from result_replay import run

    test = types.SimpleNamespace()
    cache = BASE.with_name("pin-cache")
    test.tpcds_sqls = pickle.loads((cache / "query-fixtures.pkl").read_bytes())

    class Tables(dict):
        def __missing__(self, name):
            if not isinstance(name, str) or not name.isidentifier():
                raise KeyError(name)
            self[name] = table = pickle.loads((cache / "tables" / name).read_bytes())
            return table

    tables = Tables()
    path = Path("tests/test_executor.py")
    worker = next(node for node in ast.parse(path.read_text()).body
                  if isinstance(node, ast.FunctionDef) and node.name == "mp_execute")
    scope = dict(find_tables=sqlglot.find_tables, execute=ex.execute,
                 _tables=tables, _schema=TPCDS_SCHEMA)
    exec(compile(ast.Module([worker], []), str(path), "exec"), scope)
    tests = types.SimpleNamespace(parse_one=sqlglot.parse_one, transpile=sqlglot.transpile,
                                  mp_execute=scope["mp_execute"])
    reference = json.loads(BASE.read_text())["ds"]

    def check(i):
        selection = {"active": [i], "worker": [i]}
        run(test, tests, ex, reference, [], selection, digest, plan_digest)

    return check


def executor(record, expected, full, selection=None):
    if selection is not None and not selection["active"]:
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
    changed_module = os.environ.get("BLAST_CHANGED_MODULE", "")
    pristine_setup = (
        not record
        and expected
        and "," not in changed_module
        and (
            changed_module == "sqlglot.parser"
            or changed_module.startswith(("sqlglot.parsers.", "sqlglot.optimizer."))
        )
    )
    if pristine_setup:
        setup = json.loads(Path("/app/pin-cache/ds-setup.json").read_text())
        pristine_setup = changed_module.replace(".", "/") + ".py" not in setup
    # A query slice requires both the inline and the original spawned setup
    # traces to miss every changed region. Its table inputs are pinned.
    pristine_setup = pristine_setup or selection is not None
    tables_file = BASE.with_name("staged-tables.pkl")
    if pristine_setup:
        with tables_file.open("rb") as stream:
            test.tpcds_sqls, test.tpcds_tables = pickle.load(stream)
    else:
        test.setUpClass()
        if record:
            with tables_file.open("wb") as stream:
                pickle.dump((test.tpcds_sqls, test.tpcds_tables), stream)
    tests.initializer(tests.TPCDS_SCHEMA, test.tpcds_tables)
    if not record:
        sys.path.append(str(Path(__file__).parent))
        from result_replay import run
        try:
            run(test, tests, ex, expected, full, selection, digest, plan_digest)
        finally:
            if not pristine_setup:
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
        if not pristine_setup:
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
    data = json.loads(BASE.read_text())
    if group == "small":
        sys.path.append(str(Path(__file__).parent))
        from heavy_cases import Recorder, parser as pinned
        pinned(Recorder(False), {"parser:0", "parser:1", "parser:3"})
    elif group == "slice":
        selection = json.loads(sys.argv[2])
        if "scalar" in selection:
            from scalar_replay import equivalent
            verified = equivalent(selection)
            selection["active"] = [i for i in selection["active"] if i not in verified]
        executor(False, data["ds"], selection["execute"], selection)
    elif group == "scaled":
        if parser() != data["parser"]:
            raise ValueError("scaled parser outcomes changed")
    else:
        executor(
            False,
            data["ds"],
            json.loads(sys.argv[2]) if group == "partial" else group == "full",
        )


if __name__ == "__main__":
    main()
