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
    from phase import mark

    execute = ex.PythonExecutor.execute
    extra_cpu = 3.0 if full else 0.6

    def check_plan(engine, plan):
        nonlocal extra_cpu, ran, skipped
        mark("plan")
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
            mark("execute")
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
        inputs = []
        import sqlglot
        active = set(selection["active"]) if selection is not None else range(99)
        mark("tokens")
        for i in sorted(active):
            meta, sql, _ = test.tpcds_sqls[i]
            tokens = sqlglot.tokenize(sql)
            if digest([[t.token_type.name, t.text, t.line, t.col, t.start, t.end, t.comments]
                       for t in tokens]) != expected[i][0]:
                raise ValueError("tokens changed")
        mark("parse")
        for i in sorted(active):
            meta, sql, _ = test.tpcds_sqls[i]
            expr = pickle.loads(pickle.dumps(tests.parse_one(sql)))
            if digest(expr.dump()) != expected[i][2]:
                raise ValueError("AST changed")
            if meta.get("execute"):
                inputs.append((i, meta, sql, expr))
        mark("generate")
        for i, meta, sql, expr in inputs:
            if digest(tests.transpile(sql, write="duckdb")) != expected[i][4]:
                raise ValueError("reference query changed")
        for i, meta, sql, expr in inputs:
            reference = expected[i]
            mark("optimize")
            required = i in full if isinstance(full, list) else bool(full)
            parsed_matches = True
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
