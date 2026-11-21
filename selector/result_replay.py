"""Compare the reference SQL and the observable result of each executed query."""
import contextlib
import hashlib
import os
import pickle


def table_digest(table):
    result = hashlib.sha256()
    for values in (table.columns, *table.rows):
        result.update(pickle.dumps(tuple(values), protocol=4))
    return result.hexdigest()


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
            table = pickle.loads(pickle.dumps(table))
            diagnostic = os.environ.get("BLAST_PRACTICE_DIAGNOSTIC", "") in {"D1", "D2", "D3"}
            mismatch = table_digest(table) != reference[-1]
            if i in changed_reference or (diagnostic and mismatch):
                if live_reference is None:
                    # Run the original assertion when its DuckDB SQL changes.
                    from tests.test_executor import TestExecutor
                    if hasattr(test, "tpcds_conn"):
                        live_reference = test
                    else:
                        live_reference = TestExecutor("test_execute_tpcds")
                        live_reference.setUpClass()
                        reference_setup.callback(live_reference.tearDownClass)
                if diagnostic:
                    # Only an error raised by the final DataFrame comparison is
                    # positive evidence. Setup, SQL execution and conversion can fail.
                    from tests import test_executor as actual
                    frame = live_reference.cached_execute(test.tpcds_sqls[i][1], tpch=False)
                    got = actual.pd.DataFrame(
                        ((actual.np.nan if c is None else c for c in r) for r in table.rows),
                        columns=table.columns)
                    try:
                        actual.assert_frame_equal(frame, got, check_dtype=False, check_index_type=False)
                    except AssertionError:
                        mark("asserted")
                        raise
                    if mismatch and i not in changed_reference:
                        raise ValueError("non-identical digest")
                else:
                    live_reference.subtestHelper(i, table, tpch=False)
            elif mismatch:
                raise ValueError("result changed")
    finally:
        ex.PythonExecutor.execute = execute
        reference_setup.close()
