"""Compare the reference SQL and the observable result of each executed query."""
import hashlib
import pickle
import signal
import time


def table_digest(table):
    result = hashlib.sha256()
    for values in (table.columns, *table.rows):
        result.update(pickle.dumps(tuple(values), protocol=4))
    return result.hexdigest()


def run(test, tests, ex, expected, full, selection, digest, plan_digest):
    execute = ex.PythonExecutor.execute
    extra_cpu = 3.0 if full else 0.6

    def check_plan(engine, plan):
        nonlocal extra_cpu, ran, skipped
        if ran or skipped:
            raise ValueError("multiple executions")
        if not required:
            if (parsed_matches and digest(plan.expression.dump()) == reference[6]
                    and plan_digest(plan) == reference[7]):
                skipped = True
                return None
            if extra_cpu <= 0.02:
                raise TimeoutError("result verification budget exhausted")
            # A changed join can expand rapidly. A CPU timer also bounds a
            # single divergent query. The process exits on SIGPROF.
            signal.setitimer(signal.ITIMER_PROF, extra_cpu)
        start = time.process_time()
        try:
            ran = True
            return execute(engine, plan)
        finally:
            if not required:
                signal.setitimer(signal.ITIMER_PROF, 0)
                extra_cpu -= time.process_time() - start

    ex.PythonExecutor.execute = check_plan
    try:
        if len(test.tpcds_sqls) != len(expected) or len(expected) != 99:
            raise ValueError("incomplete fixtures")
        for i, (meta, sql, _) in enumerate(test.tpcds_sqls):
            if selection is not None and i not in selection["active"]:
                continue
            reference = expected[i]
            expr = pickle.loads(pickle.dumps(tests.parse_one(sql)))
            if not meta.get("execute"):
                continue
            if digest(tests.transpile(sql, write="duckdb")) != reference[4]:
                raise ValueError("reference query changed")
            required = i in full if isinstance(full, list) else bool(full)
            parsed_matches = not required and digest(expr.dump()) == reference[2]
            if selection is not None and i not in selection["worker"] and parsed_matches:
                continue
            ran = skipped = False
            table = tests.mp_execute(expr, meta)
            if skipped and table is None:
                continue
            if not ran or table is None:
                raise ValueError("missing result")
            if table_digest(pickle.loads(pickle.dumps(table))) != reference[-1]:
                raise ValueError("result changed")
    finally:
        ex.PythonExecutor.execute = execute
