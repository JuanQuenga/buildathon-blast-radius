"""Compare the reference SQL and the observable result of each executed query."""
import contextlib
import functools
import gzip
import hashlib
from pathlib import Path
import pickle


@functools.cache
def reference_frames():
    with gzip.open(Path(__file__).parent / "maps/duckdb-frames.pkl.gz", "rb") as stream:
        return pickle.load(stream)


def table_digest(table):
    result = hashlib.sha256()
    for values in (table.columns, *table.rows):
        result.update(pickle.dumps(tuple(values), protocol=4))
    return result.hexdigest()


def run(test, tests, ex, expected, full, selection, digest, plan_digest):
    from phase import mark
    from query_budget import query_budget
    import json
    import os
    from pathlib import Path
    from staged import execution_key

    modules = os.environ.get("BLAST_CHANGED_MODULE", "").split(",")
    keys = {}
    if all(m.startswith("sqlglot.optimizer.") for m in modules):
        keys = json.loads((Path(__file__).parent / "maps/plan-keys.json").read_text())

    execute = ex.PythonExecutor.execute

    inside = False
    live_reference = None
    changed_reference = set()
    reference_setup = contextlib.ExitStack()

    def reference_test():
        nonlocal live_reference
        if live_reference is None:
            if hasattr(test, "tpcds_conn"):
                live_reference = test
            else:
                from tests.test_executor import TestExecutor
                live_reference = TestExecutor("test_execute_tpcds")
                live_reference.setUpClass()
                reference_setup.callback(live_reference.tearDownClass)
        return live_reference

    def reference_matches(i):
        if i not in changed_reference:
            return True
        frame = reference_test().cached_execute(test.tpcds_sqls[i][1], tpch=False)
        return pickle.dumps(frame, protocol=4) == pickle.dumps(reference_frames()[i], protocol=4)

    def check_plan(engine, plan, *args, **kwargs):
        nonlocal ran, skipped, inside
        if inside:
            return execute(engine, plan, *args, **kwargs)
        mark("plan")
        if ran or skipped:
            raise ValueError("multiple executions")
        if not required:
            same = (parsed_matches and digest(plan.expression.dump()) == reference[6]
                    and plan_digest(plan) == reference[7])
            if not same and str(i) in keys:
                same = execution_key(plan) == keys[str(i)]
            if same and reference_matches(i):
                skipped = True
                return None
        mark("execute")
        ran = True
        inside = True
        try:
            with query_budget(i):
                return execute(engine, plan, *args, **kwargs)
        finally:
            inside = False

    ex.PythonExecutor.execute = check_plan
    try:
        if len(test.tpcds_sqls) != len(expected) or len(expected) != 99:
            raise ValueError("incomplete fixtures")
        inputs = []
        active = set(selection["active"]) if selection is not None else range(99)
        mark("parse")
        for i in sorted(active):
            meta, sql, _ = test.tpcds_sqls[i]
            expr = pickle.loads(pickle.dumps(tests.parse_one(sql)))
            matches = digest(expr.dump()) == expected[i][2]
            if meta.get("execute"):
                inputs.append((i, meta, sql, expr, matches))
        mark("generate")
        for i, meta, sql, expr, parsed_matches in inputs:
            if digest(tests.transpile(sql, write="duckdb")) != expected[i][4]:
                changed_reference.add(i)
        for i, meta, sql, expr, parsed_matches in inputs:
            reference = expected[i]
            mark("optimize")
            required = i in full if isinstance(full, list) else bool(full)
            if not required and selection is not None and i not in selection["worker"] and parsed_matches and reference_matches(i):
                continue
            ran = skipped = False
            table = tests.mp_execute(expr, meta)
            if skipped and table is None:
                continue
            if not ran or table is None:
                raise ValueError("missing result")
            table = pickle.loads(pickle.dumps(table))
            if i in changed_reference:
                reference_test().subtestHelper(i, table, tpch=False)
            elif table_digest(table) != reference[-1]:
                raise ValueError("result changed")
    finally:
        ex.PythonExecutor.execute = execute
        reference_setup.close()
