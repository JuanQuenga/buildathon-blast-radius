"""Compare the reference SQL and the observable result of each executed query."""
import contextlib
import gzip
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys
import time


# Modules provably disjoint from default-dialect parse_one(sql) + pickle round-trip.
_PARSE_DISJOINT_EXACT = frozenset({
    "sqlglot.planner",
    "sqlglot.generator",
    "sqlglot.schema",
    "sqlglot.lineage",
    "sqlglot.diff",
    "sqlglot.trie",
    "sqlglot.anonymize",
    "sqlglot.jsonpath",
    "sqlglot.transforms",
})

# Modules whose ONLY execution-tail effect in PythonExecutor.execute() is
# covered by pygen_plan_digest (plan topology, PythonGenerator compilation,
# TIME_PART_SINGULARS, DType/DataType sets, and engine.tables.find()).
_PYGEN_MODULES = frozenset({
    "sqlglot.generator",
    "sqlglot.generators.python",
    "sqlglot.dialects.dialect",
    "sqlglot.expressions.core",
    "sqlglot.expressions.query",
    "sqlglot.expressions.datatypes",
    "sqlglot.planner",
    "sqlglot.schema",
    "sqlglot.trie",
    "sqlglot.serde",
    "sqlglot.helper",
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


_TPCDS_DB = Path(os.environ.get("BLAST_APP_DIR", "/app")) / "pin-cache" / "tpcds.duckdb"
_TPCDS_DUCKDB_DIGESTS = _TPCDS_DB.with_name("tpcds-duckdb-digests.json")


def frame_digest(frame):
    """Canonical digest of a DataFrame: columns, dtypes, NA mask and exact cell reprs."""
    rows = frame.astype(object).itertuples(index=False, name=None)
    payload = (list(map(str, frame.columns)), [str(t) for t in frame.dtypes], frame.isna().to_numpy().tolist(),
               [[repr(v) for v in row] for row in rows], len(frame.index))
    return hashlib.sha256(pickle.dumps(payload, protocol=4)).hexdigest()


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
    if _TPCDS_DB.is_file():
        # The same READ_CSV relations, materialized once at image build time.
        conn = duckdb.connect(str(_TPCDS_DB), read_only=True)
        ref.tpcds_conn = conn
        ref.tpcds_sqls = test.tpcds_sqls
        ref.cache = {}
        reference_setup.callback(conn.close)
        return ref, None
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
            or mod.startswith("sqlglot.parsers.")
            or mod.startswith("sqlglot.typing.")
            or (mod.startswith("sqlglot.dialects.") and mod != "sqlglot.dialects.dialect")
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
        if mod.startswith("sqlglot.expressions."):
            return True
        if (
            (mod.startswith("sqlglot.optimizer.") and mod != "sqlglot.optimizer.scope")
            or mod.startswith("sqlglot.executor.")
            or mod.startswith("sqlglot.dialects.")
            or mod.startswith("sqlglot.parsers.")
            or mod.startswith("sqlglot.generators.")
            or mod.startswith("sqlglot.typing.")
            or mod in {
                "sqlglot.planner",
                "sqlglot.schema",
                "sqlglot.lineage",
                "sqlglot.diff",
                "sqlglot.serde",
                "sqlglot.anonymize",
            }
        ):
            continue
        return True
    return False


def _needs_pygen_check(changed_modules):
    if not changed_modules:
        return True
    return any(mod in _PYGEN_MODULES for mod in changed_modules)


def pygen_plan_digest(engine, plan, planner_mod, generator_cls, digest_fn):
    """Complete, deterministic, side-effect-free execution-equivalence digest of
    ``plan``: hashes EVERY step field read by ``PythonExecutor.execute()``
    (topology, step names, limits, offsets, column names, join order/sides/kinds,
    set ops) plus EVERY Python code string compiled by ``PythonGenerator``
    (including nested subqueries) and ``Generator.TIME_PART_SINGULARS``.

    Insensitive to unexecuted ``exp.Expr.meta`` / ``comments`` while faithfully
    capturing any ``.meta`` read by ``PythonGenerator.generate()``."""
    import math as _math
    from sqlglot import helper as _helper_mod

    probe_eng = type(engine)(tables={})
    live_tables = getattr(engine, "tables", None)
    check_live_tables = (
        live_tables is not None
        and hasattr(live_tables, "find")
        and isinstance(getattr(live_tables, "mapping", None), dict)
        and bool(live_tables.mapping)
    )

    def det_register(query):
        if probe_eng._ctes is not None and not query.args.get("with_"):
            query.set("with_", probe_eng._ctes)
        sql = query.sql()
        name = probe_eng._plan_names_by_sql.get(sql)
        if name is None:
            name = probe_eng._plan_names_by_sql[sql] = "_sq_" + digest_fn(sql)[:12]
            probe_eng._subquery_plans[name] = (planner_mod.Plan(query), {})
        return planner_mod.exp.Literal.string(name)

    probe_eng._register_subquery = det_register
    memo_plans = {}

    def _lim(v):
        return "inf" if isinstance(v, float) and _math.isinf(v) else v

    def _col_names(exprs):
        return [
            e.alias_or_name if isinstance(e, planner_mod.exp.Expr) else e
            for e in (exprs or ())
        ]

    def _source_key(src):
        if src is None:
            return None
        if isinstance(src, planner_mod.exp.Expr):
            parts = [p.name for p in getattr(src, "parts", ())] if hasattr(src, "parts") else []
            return ("E", type(src).__qualname__, src.name, src.alias, src.alias_or_name, parts)
        return ("S", src)

    def walk_plan(p):
        pid = id(p)
        if pid in memo_plans:
            return memo_plans[pid]
        ctes = probe_eng._ctes
        probe_eng._ctes = p.ctes
        try:
            step_memo = {}

            def visit(step):
                sid = id(step)
                if sid in step_memo:
                    return step_memo[sid]
                dep_digests = sorted(visit(dep) for dep in step.dependencies)
                for dep in step.dependencies:
                    if step not in dep.dependents:
                        raise ValueError("broken plan dependents link")

                def gen_one(e):
                    if not e:
                        return ""
                    rep = probe_eng._replace_subqueries(e)
                    code = probe_eng.generator.generate(rep)
                    if "JSONEXTRACT" in code:
                        raise ValueError("JSONEXTRACT requires live execution")
                    return code

                lim = _lim(step.limit)
                off = step.offset
                if isinstance(step, planner_mod.Scan):
                    if check_live_tables and isinstance(step.source, planner_mod.exp.Expr):
                        sname = step.source.name or step.source.alias
                        dep_names = {d.name for d in step.dependencies}
                        if sname and sname not in dep_names:
                            found = live_tables.find(step.source)
                            expected_tbl = live_tables.mapping.get(step.source.name)
                            if found is None or expected_tbl is None or found is not expected_tbl:
                                raise ValueError(f"table lookup diverged for {sname}")
                    payload = (
                        "S", step.name, _source_key(step.source), lim, off,
                        _col_names(step.projections),
                        gen_one(step.condition), [gen_one(x) for x in step.projections], dep_digests,
                    )
                elif isinstance(step, planner_mod.Aggregate):
                    payload = (
                        "A", step.name, _source_key(getattr(step, "source", None)), lim, off,
                        list(step.group.keys()), [gen_one(x) for x in step.group.values()],
                        _col_names(step.aggregations), [gen_one(x) for x in step.aggregations],
                        _col_names(step.operands), [gen_one(x) for x in step.operands],
                        _col_names(step.projections), gen_one(step.condition),
                        [gen_one(x) for x in step.projections], dep_digests,
                    )
                elif isinstance(step, planner_mod.Join):
                    # Preserve dict order: PythonExecutor assigns column_ranges in step.joins order.
                    jitems = [
                        (
                            nm,
                            j.get("side"),
                            j.get("kind"),
                            bool(j.get("source_key")),
                            [gen_one(x) for x in j["source_key"]],
                            [gen_one(x) for x in j["join_key"]],
                            gen_one(j["condition"]),
                        )
                        for nm, j in step.joins.items()
                    ]
                    payload = (
                        "J", step.name, step.source_name, lim, off, jitems,
                        _col_names(step.projections), gen_one(step.condition),
                        [gen_one(x) for x in step.projections], dep_digests,
                    )
                elif isinstance(step, planner_mod.Sort):
                    payload = (
                        "O", step.name, lim, off,
                        _col_names(step.projections), [gen_one(x) for x in step.key],
                        gen_one(step.condition), [gen_one(x) for x in step.projections], dep_digests,
                    )
                elif isinstance(step, planner_mod.SetOperation):
                    op_cls = step.op
                    op_flags = (
                        op_cls.__qualname__ if isinstance(op_cls, type) else str(op_cls),
                        isinstance(op_cls, type) and issubclass(op_cls, planner_mod.exp.Intersect),
                        isinstance(op_cls, type) and issubclass(op_cls, planner_mod.exp.Except),
                        isinstance(op_cls, type) and issubclass(op_cls, planner_mod.exp.Union),
                    )
                    payload = (
                        "U", step.name, op_flags, bool(step.distinct), step.left, step.right,
                        lim, off, dep_digests,
                    )
                else:
                    raise ValueError(f"unknown plan step {type(step).__name__}")
                h = digest_fn(payload)
                step_memo[sid] = h
                return h

            root_h = visit(p.root)
            leaves_h = sorted(s.name for s in p.leaves)
            sq_items = []
            for sq_sql, sq_name in sorted(probe_eng._plan_names_by_sql.items()):
                sq_plan, _ = probe_eng._subquery_plans[sq_name]
                if id(sq_plan) != pid:
                    sq_items.append((sq_sql, walk_plan(sq_plan)))
            res = digest_fn((root_h, leaves_h, sq_items))
            memo_plans[pid] = res
            return res
        finally:
            probe_eng._ctes = ctes

    exp_mod = planner_mod.exp
    env_invariants = (
        sorted(generator_cls.TIME_PART_SINGULARS.items()),
        sorted(str(x) for x in exp_mod.DataType.TEXT_TYPES),
        sorted(str(x) for x in exp_mod.DataType.NUMERIC_TYPES),
        [
            str(getattr(exp_mod.DType, k))
            for k in ("DATE", "TIME", "DATETIME", "TIMESTAMP", "BOOLEAN", "FLOAT", "DOUBLE", "DECIMAL", "INT")
        ],
        bool(_helper_mod.PYTHON_VERSION >= (3, 8)),
    )
    return digest_fn((env_invariants, walk_plan(plan)))


def _load_pygen_digests():
    p = Path(os.environ.get("BLAST_APP_DIR", "/app")) / "pin-cache" / "pygen-digests.json"
    if p.is_file():
        return json.loads(p.read_text())
    return None


def _load_query_exec_costs():
    p = Path(os.environ.get("BLAST_APP_DIR", "/app")) / "ds-costs.json.gz"
    if not p.is_file():
        return {}
    with gzip.open(p) as stream:
        rows = json.load(stream)
    return {
        int(k[3:]): float(v.get("phases", {}).get("execute", 0.0))
        for k, v in rows.items()
        if k.startswith("ds:") and k[3:].isdigit()
    }


def run(test, tests, ex, expected, full, selection, digest, plan_digest, *, check_pygen=None, plan_budget_s=None):
    from phase import mark
    from sqlglot import planner as planner_mod
    from sqlglot.generator import Generator as generator_cls

    execute = ex.PythonExecutor.execute
    orig_nlj = getattr(ex.PythonExecutor, "nested_loop_join", None)
    join_overrun = False

    def guarded_nlj(engine, join, source_context, join_context, condition, condition_context):
        nonlocal join_overrun
        if len(source_context.table.rows) * len(join_context.table.rows) > _NLJ_PAIR_LIMIT:
            join_overrun = True
            raise _JoinBudgetExceeded("nested loop join budget exceeded")
        return orig_nlj(engine, join, source_context, join_context, condition, condition_context)

    reference_setup = contextlib.ExitStack()
    changed_reference = set()
    changed_modules = [m for m in os.environ.get("BLAST_CHANGED_MODULE", "").split(",") if m]
    check_parse = _needs_parse_check(changed_modules)
    check_transpile = _needs_transpile_check(changed_modules)
    touches_executor = any(m.startswith("sqlglot.executor") for m in changed_modules)
    if check_pygen is None:
        check_pygen = _needs_pygen_check(changed_modules)
    pygen_digests = _load_pygen_digests() if check_pygen else None
    pygen_loaded = check_pygen
    exec_costs = _load_query_exec_costs() if plan_budget_s is not None else None
    exec_needed = 0.0
    plan_only_mode = False
    plan_changed_flag = False

    def _pygen_matches(engine, plan):
        nonlocal pygen_digests, pygen_loaded
        if not pygen_loaded:
            pygen_digests = _load_pygen_digests()
            pygen_loaded = True
        if pygen_digests is not None and str(cur_i) in pygen_digests:
            return pygen_plan_digest(engine, plan, planner_mod, generator_cls, digest) == pygen_digests[str(cur_i)]
        return False

    def check_plan(engine, plan):
        nonlocal ran, skipped, plan_changed_flag
        mark("plan")
        if ran or skipped:
            raise ValueError("multiple executions")
        if not required:
            if check_pygen and not touches_executor:
                # pygen_plan_digest subsumes plan_digest and plan.expression.dump();
                # skip computing the two redundant AST/JSON digests first.
                try:
                    if _pygen_matches(engine, plan):
                        skipped = True
                        return None
                except ValueError:
                    pass
            else:
                exact_match = (
                    parsed_matches
                    and digest(plan.expression.dump()) == reference[6]
                    and plan_digest(plan) == reference[7]
                )
                if exact_match:
                    if not check_pygen:
                        skipped = True
                        return None
                    try:
                        if _pygen_matches(engine, plan):
                            skipped = True
                            return None
                    except ValueError:
                        pass
                elif not touches_executor:
                    try:
                        if _pygen_matches(engine, plan):
                            skipped = True
                            return None
                    except ValueError:
                        pass
        if plan_only_mode:
            plan_changed_flag = True
            skipped = True
            return None
        mark("execute")
        ran = True
        return execute(engine, plan)

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
        active = sorted(set(selection["active"])) if selection is not None else list(range(99))
        mark("parse")
        for i in active:
            meta, sql, _ = test.tpcds_sqls[i]
            is_exec = bool(meta.get("execute"))
            if not check_parse and not is_exec:
                continue
            if not check_parse and not check_transpile and selection is not None and i not in selection["worker"]:
                continue
            if check_parse:
                expr = pickle.loads(pickle.dumps(tests.parse_one(sql)))
                matches = (
                    digest(expr.dump()) == expected[i][2]
                    if is_exec and not (check_pygen and not touches_executor)
                    else True
                )
            else:
                expr = None
                matches = True
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
                    if len(changed_reference) > 57:
                        raise ValueError("broad reference SQL change")
        # Streaming budget-guarded plan check: before executing any query when
        # called from rule_corpus Pass 2 overflow, probe the cheapest 2 queries
        # first (~0.02-0.04s). Only if BOTH produce identical plan/pygen digests
        # (proving downstream normalization or meta-only diff) do we scan all
        # remaining queries in fixture order, aborting the instant changed-plan
        # execution exceeds plan_budget_s.
        if plan_budget_s is not None and inputs:
            plan_only_mode = True
            t_plan_start = time.process_time()
            # Fast 2-query canary in strict fixture order (0..98) to avoid wasting
            # 1-2s on commits whose plans genuinely diverge across the suite.
            canary_checked = set()
            for row in inputs[:2]:
                i, meta, sql, expr, parsed_matches = row
                cur_i = i
                reference = expected[i]
                mark("optimize")
                required = i in full if isinstance(full, list) else bool(full)
                required |= i in changed_reference
                if expr is None:
                    expr = tests.parse_one(sql)
                ran = skipped = False
                plan_changed_flag = False
                tests.mp_execute(expr, meta)
                if not skipped:
                    raise ValueError("missing result")
                if plan_changed_flag:
                    raise ValueError("canary plan diverged under budget guard")
                canary_checked.add(i)
            need_exec_set = set()
            for i, meta, sql, expr, parsed_matches in inputs:
                if i in canary_checked:
                    continue
                cur_i = i
                reference = expected[i]
                mark("optimize")
                required = i in full if isinstance(full, list) else bool(full)
                required |= i in changed_reference
                if not required and selection is not None and i not in selection["worker"] and parsed_matches:
                    continue
                if expr is None:
                    expr = tests.parse_one(sql)
                ran = skipped = False
                plan_changed_flag = False
                tests.mp_execute(expr, meta)
                if not skipped:
                    raise ValueError("missing result")
                if plan_changed_flag:
                    need_exec_set.add(i)
                    exec_needed += exec_costs.get(i, 0.25)
                    elapsed_plan = time.process_time() - t_plan_start
                    budget_left = plan_budget_s - elapsed_plan
                    if (
                        len(need_exec_set) > 2
                        or exec_needed > 1.5
                        or exec_needed > 0.85 * max(0.0, budget_left)
                    ):
                        raise ValueError("changed plan execution exceeds probe budget")
                elif time.process_time() - t_plan_start > 0.60 * plan_budget_s:
                    raise ValueError("plan scan exceeds probe budget")
            plan_only_mode = False
            if not need_exec_set:
                return
            inputs = [row for row in inputs if row[0] in need_exec_set]
        # A changed DuckDB reference with an untouched executor: when the Python
        # plan (and generated code) match the pinned baseline, Python's result is
        # the pinned result, which passed against the pinned DuckDB reference. The
        # query then passes iff DuckDB returns the identical frame for the new and
        # pinned reference SQL; anything else is a failure (the test is kept).
        pinned_frames = json.loads(_TPCDS_DUCKDB_DIGESTS.read_text()) if _TPCDS_DUCKDB_DIGESTS.is_file() else {}
        reference_only = changed_reference if not touches_executor and pinned_frames else set()

        def verify_reference_only(i, sql, expr):
            nonlocal live_reference, ensure_duckdb_tables
            mark("reference")
            new_list = tests.transpile(sql, write="duckdb")
            if str(i) not in pinned_frames or len(new_list) != 1:
                raise ValueError("reference SQL not comparable")
            if live_reference is None:
                live_reference, ensure_duckdb_tables = _make_lazy_duckdb_reference(test, reference_setup)
            if ensure_duckdb_tables is not None:
                ensure_duckdb_tables(expr)
            if frame_digest(live_reference.tpcds_conn.execute(new_list[0]).fetchdf()) != pinned_frames[str(i)]:
                raise ValueError("reference result changed")

        # Always execute in original fixture order (0..98) to preserve cross-query state invariants.
        for i, meta, sql, expr, parsed_matches in inputs:
            cur_i = i
            reference = expected[i]
            mark("optimize")
            required = i in full if isinstance(full, list) else bool(full)
            required |= i in changed_reference and i not in reference_only
            if (not required and selection is not None and i not in selection["worker"] and parsed_matches
                    and i not in changed_reference):
                continue
            if expr is None:
                expr = tests.parse_one(sql)
            q_start = time.process_time()
            ran = skipped = False
            table = tests.mp_execute(expr, meta)
            if join_overrun:
                raise _JoinBudgetExceeded("nested loop join budget exceeded")
            if time.process_time() - q_start > _PER_QUERY_CPU_LIMIT:
                raise _JoinBudgetExceeded("per-query CPU budget exceeded")
            if skipped and table is None:
                if i in changed_reference:
                    verify_reference_only(i, sql, expr)
                continue
            if not ran or table is None:
                raise ValueError("missing result")
            table = pickle.loads(pickle.dumps(table))
            if i in changed_reference or table_digest(table) != reference[-1]:
                if i not in changed_reference:
                    digest_fallbacks += 1
                    if digest_fallbacks > 57:
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
