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


def executor(record, expected, full, selection=None):
    if selection is not None and not selection["active"]:
        return [], []
    import sqlglot
    import sqlglot.executor as ex
    from tests import test_executor as tests

    paths, names, calls = set(), set(), set()
    tail = []
    affected = set(full) if isinstance(full, list) else None
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
        and not full
        and "," not in changed_module
        and (
            changed_module == "sqlglot.parser"
            or changed_module.startswith(("sqlglot.parsers.", "sqlglot.optimizer."))
        )
    )
    if pristine_setup:
        setup = json.loads(Path("/app/pin-cache/ds-setup.json").read_text())
        pristine_setup = changed_module.replace(".", "/") + ".py" not in setup
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
    original_optimize = ex.optimize

    def optimize(*args, **kwargs):
        optimized = original_optimize(*args, **kwargs)
        row.append(digest(optimized.dump()))
        return optimized

    ex.optimize = optimize
    original = ex.PythonExecutor.execute
    rows = []
    if record:
        sys.monitoring.use_tool_id(3, "execution coverage")
        sys.monitoring.register_callback(3, sys.monitoring.events.PY_START, entered)

    def execute(engine, plan):
        row.extend([digest(plan.expression.dump()), plan_digest(plan)])
        if record or (i in affected if affected is not None else full):
            if record:
                sys.monitoring.set_events(3, sys.monitoring.events.PY_START)
            try:
                return original(engine, plan)
            finally:
                if record:
                    sys.monitoring.set_events(3, 0)
        return None

    ex.PythonExecutor.execute = execute
    try:
        for i, (meta, sql, _) in enumerate(test.tpcds_sqls):
            if selection is not None:
                if i not in selection["active"]:
                    continue
                if i not in selection["worker"]:
                    parsed = tests.parse_one(sql)
                    expr = pickle.loads(pickle.dumps(parsed))
                    if digest(expr.dump()) != expected[i][2]:
                        raise ValueError("parsed expression changed")
                    if meta.get("execute") and digest(tests.transpile(sql, write="duckdb")) != expected[i][4]:
                        raise ValueError("reference query changed")
                    continue
            if record:
                paths.clear()
                names.clear()
                calls.clear()
                sys.monitoring.restart_events()
            imports = set(sys.modules) if record else ()
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
            if expected and row != expected[i][: len(row)]:
                raise ValueError("input changed")
            table = tests.mp_execute(expr, meta)
            if table is not None:
                table = pickle.loads(pickle.dumps(table))
                # Hash each row independently to avoid pickle memo identities.
                result = hashlib.sha256()
                for values in (table.columns, *table.rows):
                    result.update(pickle.dumps(tuple(values), protocol=4))
                if record:
                    test.subtestHelper(i, table, tpch=False)
                row.append(result.hexdigest())
            elif meta.get("execute") and expected:
                if i in affected if affected is not None else full:
                    raise ValueError("missing execution result")
                row.append(expected[i][-1])
            if record:
                # Conservatively include all newly imported modules in this query.
                for name in set(sys.modules) - imports:
                    file = getattr(sys.modules[name], "__file__", "") or ""
                    if file.startswith(prefix):
                        paths.add(file[len(prefix) :])
                tail.append(
                    dict(paths=sorted(paths), names=sorted(names), calls=sorted(calls))
                )
            if expected and row != expected[i]:
                raise ValueError("plan changed")
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
