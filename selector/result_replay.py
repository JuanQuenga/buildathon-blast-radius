"""Compare the reference SQL and the observable result of each executed query."""
import contextlib
import hashlib
import functools
import json
from pathlib import Path
import pickle


def table_digest(table):
    result = hashlib.sha256()
    for values in (table.columns, *table.rows):
        result.update(pickle.dumps(tuple(values), protocol=4))
    return result.hexdigest()


@functools.cache
def result_sizes():
    return json.loads((Path(__file__).parent / "maps/result-sizes.json").read_text())


def run(test, tests, ex, expected, full, selection, digest, plan_digest):
    from phase import mark

    execute = ex.PythonExecutor.execute

    def check_plan(engine, plan):
        nonlocal ran, skipped
        mark("plan")
        if ran or skipped:
            raise ValueError("multiple executions")
        if not required:
            if (parsed_matches and digest(plan.expression.dump()) == reference[6]
                    and plan_digest(plan) == reference[7]):
                skipped = True
                return None
        mark("execute")
        ran = True
        return execute(engine, plan)

    reference_setup = contextlib.ExitStack()
    changed_reference = set()
    ex.PythonExecutor.execute = check_plan
    try:
        if len(test.tpcds_sqls) != len(expected) or len(expected) != 99:
            raise ValueError("incomplete fixtures")
        inputs = []
        live_reference = None
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
            required |= i in changed_reference
            if not required and selection is not None and i not in selection["worker"] and parsed_matches:
                continue
            ran = skipped = False
            table = tests.mp_execute(expr, meta)
            if skipped and table is None:
                continue
            if not ran or table is None:
                raise ValueError("missing result")
            # A row/column count mismatch against an unchanged reference
            # cannot pass DataFrame equality. Exit before serializing the rows.
            if i not in changed_reference and [len(table.rows), len(table.columns)] != result_sizes()[i]:
                raise ValueError("result dimensions changed")
            table = pickle.loads(pickle.dumps(table))
            if i in changed_reference:
                if live_reference is None:
                    # Run the original assertion when its DuckDB SQL changes.
                    from tests.test_executor import TestExecutor
                    if hasattr(test, "tpcds_conn"):
                        live_reference = test
                    else:
                        live_reference = TestExecutor("test_execute_tpcds")
                        live_reference.setUpClass()
                        reference_setup.callback(live_reference.tearDownClass)
                live_reference.subtestHelper(i, table, tpch=False)
            elif table_digest(table) != reference[-1]:
                raise ValueError("result changed")
    finally:
        ex.PythonExecutor.execute = execute
        reference_setup.close()
