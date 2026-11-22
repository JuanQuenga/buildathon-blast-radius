"""Compare the reference SQL and the observable result of each executed query."""
import contextlib
import hashlib
import os
import pickle
import time


# Modules provably disjoint from default-dialect parse_one(sql) + pickle round-trip.
_PARSE_DISJOINT_EXACT = frozenset({
    "sqlglot.planner",
    "sqlglot.generator",
    "sqlglot.schema",
    "sqlglot.lineage",
    "sqlglot.diff",
})

# Exact modules executed by transpile(sql, write="duckdb") on the 57 executed TPC-DS queries.
_TRANSPILE_MODULES = frozenset({
    "sqlglot",
    "sqlglot.dialects",
    "sqlglot.dialects.dialect",
    "sqlglot.dialects.duckdb",
    "sqlglot.errors",
    "sqlglot.expressions.builders",
    "sqlglot.expressions.core",
    "sqlglot.expressions.datatypes",
    "sqlglot.expressions.query",
    "sqlglot.generator",
    "sqlglot.generators",
    "sqlglot.generators.duckdb",
    "sqlglot.helper",
    "sqlglot.jsonpath",
    "sqlglot.optimizer.scope",
    "sqlglot.parser",
    "sqlglot.parsers.duckdb",
    "sqlglot.tokenizer_core",
    "sqlglot.tokens",
    "sqlglot.transforms",
    "sqlglot.trie",
    "sqlglot.typing.duckdb",
})

# Pinned max nested-loop join pair count across all 57 TPC-DS queries is 10,000.
# 80x that limit prevents quadratic join blowups from burning 10-35s CPU.
_NLJ_PAIR_LIMIT = 800_000
# Slowest pinned TPC-DS query takes ~0.62s Mac / ~1.0s Linux CPU; 3.5s catches blowups without false aborts.
_PER_QUERY_CPU_LIMIT = 3.5


class _JoinBudgetExceeded(BaseException):
    """BaseException so patched ``except Exception`` cannot swallow an overrun."""


def table_digest(table):
    result = hashlib.sha256()
    for values in (table.columns, *table.rows):
        result.update(pickle.dumps(tuple(values), protocol=4))
    return result.hexdigest()


def _needs_parse_check(changed_modules):
    if not changed_modules:
        return True
    for mod in changed_modules:
        if mod in _PARSE_DISJOINT_EXACT:
            continue
        if (
            (mod.startswith("sqlglot.optimizer.") and mod != "sqlglot.optimizer.scope")
            or mod.startswith("sqlglot.executor.")
            or mod.startswith("sqlglot.generators.")
            or mod.startswith("sqlglot.typing.")
        ):
            continue
        return True
    return False


def _needs_transpile_check(changed_modules):
    if not changed_modules:
        return True
    for mod in changed_modules:
        if mod in _TRANSPILE_MODULES:
            return True
        if not (
            (mod.startswith("sqlglot.optimizer.") and mod != "sqlglot.optimizer.scope")
            or mod.startswith("sqlglot.executor.")
            or mod == "sqlglot.planner"
        ):
            return True
    return False


def run(test, tests, ex, expected, full, selection, digest, plan_digest):
    from phase import mark

    execute = ex.PythonExecutor.execute
    orig_nlj = getattr(ex.PythonExecutor, "nested_loop_join", None)
    join_overrun = False

    def guarded_nlj(engine, join, source_context, join_context, condition, condition_context):
        nonlocal join_overrun
        if len(source_context.table.rows) * len(join_context.table.rows) > _NLJ_PAIR_LIMIT:
            join_overrun = True
            raise _JoinBudgetExceeded("nested loop join budget exceeded")
        return orig_nlj(engine, join, source_context, join_context, condition, condition_context)

    def check_plan(engine, plan):
        nonlocal ran, skipped
        mark("plan")
        if ran or skipped:
            raise ValueError("multiple executions")
        if not required:
            if (
                parsed_matches
                and digest(plan.expression.dump()) == reference[6]
                and plan_digest(plan) == reference[7]
            ):
                skipped = True
                return None
        mark("execute")
        ran = True
        return execute(engine, plan)

    reference_setup = contextlib.ExitStack()
    changed_reference = set()
    changed_modules = [m for m in os.environ.get("BLAST_CHANGED_MODULE", "").split(",") if m]
    check_parse = _needs_parse_check(changed_modules)
    check_transpile = _needs_transpile_check(changed_modules)

    ex.PythonExecutor.execute = check_plan
    if orig_nlj is not None:
        ex.PythonExecutor.nested_loop_join = guarded_nlj
    try:
        if len(test.tpcds_sqls) != len(expected) or len(expected) != 99:
            raise ValueError("incomplete fixtures")
        inputs = []
        live_reference = None
        active = set(selection["active"]) if selection is not None else range(99)
        mark("parse")
        for i in sorted(active):
            meta, sql, _ = test.tpcds_sqls[i]
            is_exec = bool(meta.get("execute"))
            if not check_parse and not is_exec:
                continue
            if not check_parse and not check_transpile and selection is not None and i not in selection["worker"]:
                continue
            expr = pickle.loads(pickle.dumps(tests.parse_one(sql)))
            matches = (digest(expr.dump()) == expected[i][2]) if check_parse else True
            if is_exec:
                inputs.append((i, meta, sql, expr, matches))
        if check_transpile:
            mark("generate")
            for i, meta, sql, expr, parsed_matches in inputs:
                if digest(tests.transpile(sql, write="duckdb")) != expected[i][4]:
                    changed_reference.add(i)
                    # Mass DuckDB reference SQL divergence (e.g. pretty=True) would
                    # require full live DuckDB execution across all queries -> retain.
                    if len(changed_reference) > 16:
                        raise ValueError("broad reference SQL change")
        for i, meta, sql, expr, parsed_matches in inputs:
            reference = expected[i]
            mark("optimize")
            required = i in full if isinstance(full, list) else bool(full)
            required |= i in changed_reference
            if not required and selection is not None and i not in selection["worker"] and parsed_matches:
                continue
            q_start = time.process_time()
            ran = skipped = False
            table = tests.mp_execute(expr, meta)
            if join_overrun:
                raise _JoinBudgetExceeded("nested loop join budget exceeded")
            if time.process_time() - q_start > _PER_QUERY_CPU_LIMIT:
                raise _JoinBudgetExceeded("per-query CPU budget exceeded")
            if skipped and table is None:
                continue
            if not ran or table is None:
                raise ValueError("missing result")
            table = pickle.loads(pickle.dumps(table))
            if i in changed_reference:
                if live_reference is None:
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
        if orig_nlj is not None:
            ex.PythonExecutor.nested_loop_join = orig_nlj
        reference_setup.close()
