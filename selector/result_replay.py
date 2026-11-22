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


import re as _re

_SQL_WS_TOKEN_RE = _re.compile(
    r"'(?:''|[^'\\])*'|\"(?:\"\"|[^\"\\])*\"|[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|::|!=|<>|<=|>=|\|\||->|->>|[^\s]"
)


def _duckdb_sql_ws_equivalent(old_sql: str, new_sql: str) -> bool:
    """True when ``old_sql`` and ``new_sql`` differ ONLY in insignificant
    whitespace (e.g. ``pretty=True`` formatting) without altering any literal,
    comment, identifier, number, or operator token."""
    if not old_sql or not new_sql:
        return False
    for s in (old_sql, new_sql):
        if "--" in s or "/*" in s or "\\" in s:
            return False
    old_toks = _SQL_WS_TOKEN_RE.findall(old_sql)
    new_toks = _SQL_WS_TOKEN_RE.findall(new_sql)
    if old_toks != new_toks:
        return False
    # Verify every non-whitespace character was consumed cleanly by the tokenizer.
    if _SQL_WS_TOKEN_RE.sub("", old_sql).strip() or _SQL_WS_TOKEN_RE.sub("", new_sql).strip():
        return False
    return True


def _make_lazy_duckdb_reference(test, reference_setup):
    """Create a TestExecutor-compatible live DuckDB reference whose TPC-DS views
    are registered lazily per referenced table (~0.05s vs ~0.87s for full
    TestExecutor.setUpClass() CSV parsing)."""
    if hasattr(test, "tpcds_conn"):
        return test, None
    import duckdb
    from tests.test_executor import DIR_TPCDS, TPCDS_SCHEMA, TestExecutor

    ref = TestExecutor("test_execute_tpcds")
    conn = duckdb.connect()
    ref.tpcds_conn = conn
    ref.tpcds_sqls = test.tpcds_sqls
    ref.cache = {}
    reference_setup.callback(conn.close)
    loaded = set()

    def ensure_tables_for(expr):
        import sqlglot
        names = {t.name for t in sqlglot.find_tables(expr)}
        if not names or not (names <= TPCDS_SCHEMA.keys()):
            names = set(TPCDS_SCHEMA.keys())
        for table in names - loaded:
            columns = TPCDS_SCHEMA[table]
            file_name = f"{DIR_TPCDS}{table}.csv.gz"
            conn.execute(
                f"CREATE VIEW {table} AS SELECT * FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})"
            )
            loaded.add(table)

    return ref, ensure_tables_for


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
        ensure_duckdb_tables = None
        digest_fallbacks = 0
        active = list(dict.fromkeys(selection["active"])) if selection is not None else list(range(99))
        mark("parse")
        for i in active:
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
                new_list = tests.transpile(sql, write="duckdb")
                if digest(new_list) != expected[i][4]:
                    old_list = tests.transpile(sql, write="duckdb", pretty=False)
                    if (
                        len(old_list) == 1
                        and len(new_list) == 1
                        and digest(old_list) == expected[i][4]
                        and _duckdb_sql_ws_equivalent(old_list[0], new_list[0])
                    ):
                        continue
                    changed_reference.add(i)
                    # Mass DuckDB reference SQL divergence would require full live
                    # DuckDB + Python execution across all queries -> retain.
                    if len(changed_reference) > 16:
                        raise ValueError("broad reference SQL change")
        # Run required/forced queries shortest-first so broken patches fail fast.
        if full:
            import gzip as _gzip
            import json as _json
            from pathlib import Path as _Path
            _costs_p = _Path(os.environ.get("BLAST_APP_DIR", "/app")) / "ds-costs.json.gz"
            if _costs_p.is_file():
                _crows = _json.loads(_gzip.open(_costs_p, "rt", encoding="utf-8").read())
                inputs.sort(
                    key=lambda row: (
                        _crows.get(f"ds:{row[0]}", {}).get("phases", {}).get("execute", 0.0),
                        row[0],
                    )
                )
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
            if i in changed_reference or table_digest(table) != reference[-1]:
                if i not in changed_reference:
                    digest_fallbacks += 1
                    if digest_fallbacks > 16:
                        raise ValueError("broad result change")
                if live_reference is None:
                    live_reference, ensure_duckdb_tables = _make_lazy_duckdb_reference(test, reference_setup)
                if ensure_duckdb_tables is not None:
                    ensure_duckdb_tables(expr)
                live_reference.subtestHelper(i, table, tpch=False)
    finally:
        ex.PythonExecutor.execute = execute
        if orig_nlj is not None:
            ex.PythonExecutor.nested_loop_join = orig_nlj
        reference_setup.close()
