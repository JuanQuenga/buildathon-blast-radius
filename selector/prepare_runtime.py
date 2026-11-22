"""Precompute repeated operations on pinned sources inside the image's Python."""
import gzip
import marshal
import py_compile
import sys
from pathlib import Path
import re
import sqlite3
import tarfile

from selector.impact import (
    _branch_map,
    _class_attr_reads,
    _default_args_map,
    _index,
    _operand_map,
    _sources,
)
from selector import dict_keys as _dict_keys, token_options as _token_options

archive = Path("/app/selector/maps/pin-runtime.tar.xz")
with tarfile.open(archive) as recorded:
    recorded.extractall("/app", filter="data")
archive.unlink()

# Store raw members together so xz can compress shared rule inputs across files.
for raw in Path("/app").rglob("*.gz.raw"):
    raw.with_suffix("").write_bytes(gzip.compress(raw.read_bytes(), compresslevel=1, mtime=0))
    raw.unlink()

out = Path("/app/pin-cache")
out.mkdir()
for name, data in (
    ("index", _index()),
    ("src", _sources()),
    ("branches", _branch_map()),
    ("operands", _operand_map()),
    ("dict-keys", _dict_keys._load()),
    ("keyword-reads", _token_options.keyword_reads()),
    ("class-attr-reads", ({a: sorted(cs) for a, cs in _class_attr_reads()[0].items()}, _class_attr_reads()[1])),
    ("default-args", _default_args_map()),
):
    if data is not None:
        (out / (name + ".marshal")).write_bytes(marshal.dumps(data))

db = sqlite3.connect(out / "names.db")
db.execute("CREATE TABLE sites (name TEXT, path TEXT, rows BLOB, PRIMARY KEY(name,path)) WITHOUT ROWID")
for path, text in _sources()["src"].items():
    dest = out / (path + ".marshal")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(marshal.dumps(compile(text.encode(), path, "exec", dont_inherit=True)))
    words = {}
    for line, source in enumerate(text.split("\n"), 1):
        for name in set(re.findall(r"\w+", source)):
            words.setdefault(name, []).append(line)
    db.executemany("INSERT INTO sites VALUES(?,?,?)",
                   ((name, path, marshal.dumps(lines)) for name, lines in words.items()))
db.commit()
db.close()

for module in tuple(sys.modules.values()):
    source = getattr(module, "__file__", None)
    if source and source.endswith(".py"):
        py_compile.compile(source, doraise=True)

if Path("/app/ds.json.gz").exists():
    import ast
    import gzip
    import json
    from selector.heavy import local_methods

    methods = {}
    for path, source in _sources()["src"].items():
        if not re.fullmatch(r"sqlglot/(parser|generator|(?:parsers|generators)/\w+|executor/(?:context|python))\.py", path):
            continue
        ranges = []
        for fn in local_methods(ast.parse(source), path):
            if all(isinstance(d, ast.Name) and d.id in {"property", "staticmethod"} for d in fn.decorator_list):
                ranges.append([fn.lineno, fn.end_lineno, fn.body[0].lineno])
        methods[path] = ranges
    (out / "heavy-methods.json").write_text(json.dumps(methods))
    for group in ("ds", "parser"):
        with gzip.open(f"/app/{group}.json.gz") as stream:
            rows = json.load(stream)
        if group == "ds":
            (out / "ds-setup.json").write_text(json.dumps(sorted(rows["ds:setup"]["lines"])))
            with gzip.open("/app/ds-costs.json.gz") as stream:
                costs = json.load(stream)
            for key, row in rows.items():
                row["cpu"] = costs[key]["cpu"]
                row["phases"] = costs[key]["phases"]
        for row in rows.values():
            row.pop("arcs", None)
            row["lines"] = {p: v for p, v in row["lines"].items() if group == "parser" or p in methods}
        packed = json.dumps(rows, separators=(",", ":")).encode()
        (out / f"{group}-lines.json.gz").write_bytes(gzip.compress(packed, mtime=0))

    from selector.token_options import discover_sites
    source = ast.parse(_sources()["src"]["sqlglot/tokens.py"])
    init = next(n for n in ast.walk(source) if isinstance(n, ast.FunctionDef) and n.name == "_init_core")
    names = {n.value.attr for n in ast.walk(init) if isinstance(n, ast.keyword)
             and isinstance(n.value, ast.Attribute) and n.value.attr.isupper()}
    (out / "token-options.json").write_text(json.dumps({n: discover_sites(n) for n in sorted(names)}))

    # A parser mismatch can reject a replay without loading executor tables.
    import pickle
    sys.path.insert(0, "/app/sqlglot")
    with open("/app/staged-tables.pkl", "rb") as stream:
        queries, tables = pickle.load(stream)
    (out / "query-fixtures.pkl").write_bytes(pickle.dumps(queries))
    table_dir = out / "tables"
    table_dir.mkdir()
    for name, table in tables.items():
        assert re.fullmatch(r"[A-Za-z0-9_]+", name)
        (table_dir / name).write_bytes(pickle.dumps(table))
    (out / "ds-inputs.json").write_text(json.dumps([sql for _, sql, _ in queries]))

    # Precompute pinned PythonGenerator codegen digests for all 57 executed TPC-DS queries.
    import sqlglot
    import sqlglot.executor as _ex
    from sqlglot import planner as _planner
    from sqlglot.generator import Generator as _Gen
    from tests.helpers import TPCDS_SCHEMA as _TPCDS_SCHEMA
    from selector.result_replay import pygen_plan_digest as _pygen_digest
    from selector.staged import digest as _staged_digest

    from sqlglot.parser import Parser as _Parser

    _pygen_map = {}
    _ds_plans = {}
    _ds_gen_in_opt = []
    _ds_parse_seen = {}
    _orig_p_parse = _Parser.parse
    _orig_p_parse_into = _Parser.parse_into
    _orig_g_gen = _Gen.generate
    _ds_in_opt = [False]
    _ds_hit_gen = [False]

    def _rec_parse(self, raw_tokens, sql=None):
        res = _orig_p_parse(self, raw_tokens, sql=sql)
        if _ds_in_opt[0] and sql is not None and type(self).__name__ in {"Parser", "BaseParser"}:
            key = ("parse", "", sql)
            if key not in _ds_parse_seen:
                _ds_parse_seen[key] = _staged_digest([e.dump() if e is not None else None for e in res])
        return res

    def _rec_parse_into(self, expression_types, raw_tokens, sql=None):
        res = _orig_p_parse_into(self, expression_types, raw_tokens, sql=sql)
        if _ds_in_opt[0] and sql is not None and type(self).__name__ in {"Parser", "BaseParser"}:
            et = expression_types if isinstance(expression_types, tuple) else (expression_types,)
            if len(et) == 1 and isinstance(et[0], type):
                key = ("parse_into", et[0].__name__, sql)
                if key not in _ds_parse_seen:
                    _ds_parse_seen[key] = _staged_digest([e.dump() if e is not None else None for e in res])
        return res

    def _rec_gen(self, expr, *a, **kw):
        if _ds_in_opt[0]:
            _ds_hit_gen[0] = True
        return _orig_g_gen(self, expr, *a, **kw)

    _Parser.parse = _rec_parse
    _Parser.parse_into = _rec_parse_into
    _Gen.generate = _rec_gen
    try:
        for _i, (_meta, _sql, _) in enumerate(queries):
            if not _meta.get("execute"):
                continue
            _parsed_ds = pickle.loads(pickle.dumps(sqlglot.parse_one(_sql)))
            _ds_hit_gen[0] = False
            _ds_in_opt[0] = True
            try:
                _opt = _ex.optimize(_parsed_ds, schema=_TPCDS_SCHEMA, leave_tables_isolated=True)
                _plan = _ex.Plan(_opt)
            finally:
                _ds_in_opt[0] = False
            _eng = _ex.PythonExecutor(tables={})
            _pygen_map[str(_i)] = _pygen_digest(_eng, _plan, _planner, _Gen, _staged_digest)
            if _ds_hit_gen[0]:
                _ds_gen_in_opt.append(_i)
            else:
                _ds_plans[_i] = _plan
    finally:
        _Parser.parse = _orig_p_parse
        _Parser.parse_into = _orig_p_parse_into
        _Gen.generate = _orig_g_gen
    (out / "pygen-digests.json").write_text(json.dumps(_pygen_map, separators=(",", ":")))
    _ds_plans_dir = out / "ds-plans"
    _ds_plans_dir.mkdir(exist_ok=True)
    for _qi, _pl in _ds_plans.items():
        (_ds_plans_dir / f"{_qi}.pkl").write_bytes(pickle.dumps(_pl, protocol=pickle.HIGHEST_PROTOCOL))
    from selector.heavy import _leaf_dialect_targets as _leaf_dt, worker_path_queries as _wpq
    _wfiles = set(_wpq())
    _def_disjoint = sorted(
        p[:-3].replace("/", ".")
        for p in _sources()["src"]
        if p.startswith(("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/"))
        and p.endswith(".py")
        and p not in _wfiles
        and (_ls := _leaf_dt(p)) is not None
        and "" not in _ls
    )
    (out / "ds-fast-meta.json").write_text(
        json.dumps(
            {
                "gen_in_opt": _ds_gen_in_opt,
                "plan_ids": sorted(_ds_plans),
                "opt_parse_checks": [
                    [k[0], k[1], k[2], v] for k, v in sorted(_ds_parse_seen.items())
                ],
                "default_disjoint_mods": _def_disjoint,
            },
            separators=(",", ":"),
        )
    )

    # Precompute pinned TPC-H transpile + PythonGenerator codegen digests for
    # test_optimized_tpch (3.537s) and test_execute_tpch (3.863s).
    from tests.helpers import TPCH_SCHEMA as _TPCH_SCHEMA, load_sql_fixture_pairs as _load_pairs

    _tpch_rows = []
    for _meta, _sql, _opt_sql in _load_pairs("optimizer/tpc-h/tpc-h.sql"):
        _parsed = pickle.loads(pickle.dumps(sqlglot.parse_one(_sql)))
        _is_exec = bool(_meta.get("execute"))
        _pyg = None
        if _is_exec:
            _opt = _ex.optimize(_parsed, schema=_TPCH_SCHEMA, leave_tables_isolated=True)
            _plan = _ex.Plan(_opt)
            _eng = _ex.PythonExecutor(tables={})
            _pyg = _pygen_digest(_eng, _plan, _planner, _Gen, _staged_digest)
        _tpch_rows.append(
            {
                "sql": _sql,
                "opt": _opt_sql,
                "execute": _is_exec,
                "tsql": _staged_digest(sqlglot.transpile(_sql, write="duckdb")),
                "topt": _staged_digest(sqlglot.transpile(_opt_sql, write="duckdb")),
                "parse": _staged_digest(_parsed.dump()),
                "pygen": _pyg,
            }
        )
    (out / "tpch-digests.json").write_text(json.dumps(_tpch_rows, separators=(",", ":")))

    # Precompute pinned per-query line coverage & digests for TestOptimizer.test_tpcds (2.232s),
    # stress-loop line coverage for TestOptimizer.test_scope (2.533s), and per-dialect
    # line coverage for TestOptimizer.test_annotate_funcs (1.963s).
    from sqlglot import optimizer as _opt_mod
    from sqlglot.optimizer.annotate_types import annotate_types as _annotate_types
    from sqlglot.optimizer.scope import build_scope as _build_scope, traverse_scope as _traverse_scope
    from tests.helpers import string_to_bool as _str_to_bool

    # Pre-warm all dialects and lazy caches BEFORE tracing so one-time imports
    # never pollute query 0 / group 0 line coverage.
    for _dname in sqlglot.dialects.Dialects:
        sqlglot.Dialect.get_or_raise(_dname)
    _opt_mod.optimize(sqlglot.parse_one("SELECT 1"), schema=_TPCDS_SCHEMA).sql(pretty=True)

    _pin_prefix = str(Path("/app/sqlglot").resolve()) + "/"
    _cur_hits = {}

    def _trace_line(code, lineno):
        fn = code.co_filename
        if fn.startswith(_pin_prefix):
            rel = fn[len(_pin_prefix):]
            if rel.startswith("sqlglot/"):
                _cur_hits.setdefault(rel, set()).add(lineno)
        return sys.monitoring.DISABLE

    _tid = 5
    sys.monitoring.use_tool_id(_tid, "c34 pin profiles")
    sys.monitoring.register_callback(_tid, sys.monitoring.events.LINE, _trace_line)
    try:
        # 1. TestOptimizer.test_tpcds: all 99 queries
        _tpcds_opt_rows = []
        _tpcds_opt_hits = {}
        _tpcds_opt_files = set()
        for _qi, (_meta, _sql, _expected) in enumerate(_load_pairs("optimizer/tpc-ds/tpc-ds.sql")):
            _cur_hits = {}
            sys.monitoring.restart_events()
            sys.monitoring.set_events(_tid, sys.monitoring.events.LINE)
            try:
                _d = _meta.get("dialect")
                _kw = {"schema": _TPCDS_SCHEMA}
                if _s := _meta.get("schema"):
                    _kw["schema"] = json.loads(_s)
                if (_lti := _meta.get("leave_tables_isolated")) is not None:
                    _kw["leave_tables_isolated"] = _str_to_bool(_lti)
                if (_vqc := _meta.get("validate_qualify_columns")) is not None:
                    _kw["validate_qualify_columns"] = _str_to_bool(_vqc)
                if _d:
                    _kw["dialect"] = _d
                if (_cta := _meta.get("canonicalize_table_aliases")) is not None:
                    _kw["canonicalize_table_aliases"] = _str_to_bool(_cta)
                _res = _opt_mod.optimize(sqlglot.parse_one(_sql, read=_d), **_kw)
                _actual = _res.sql(pretty=True, dialect=_d)
                assert _actual == _expected
            finally:
                sys.monitoring.set_events(_tid, 0)
            _bit = 1 << _qi
            for _p, _lns in _cur_hits.items():
                _tpcds_opt_files.add(_p)
                _fh = _tpcds_opt_hits.setdefault(_p, {})
                for _ln in _lns:
                    _fh[_ln] = _fh.get(_ln, 0) | _bit
            _tpcds_opt_rows.append([_meta, _sql, _staged_digest(_expected)])
        (out / "tpcds-opt-digests.json").write_text(json.dumps(_tpcds_opt_rows, separators=(",", ":")))
        (out / "tpcds-opt-hits.marshal").write_bytes(
            marshal.dumps({"hits": _tpcds_opt_hits, "files": sorted(_tpcds_opt_files)})
        )

        # 2. TestOptimizer.test_scope stress loops (N=8 captures 100% of executed lines)
        _cur_hits = {}
        sys.monitoring.restart_events()
        sys.monitoring.set_events(_tid, sys.monitoring.events.LINE)
        try:
            _mu = sqlglot.parse_one(" UNION ALL ".join(["SELECT x FROM t"] * 8))
            list(_build_scope(_mu).traverse())
            _traverse_scope(_mu)
            _ch = sqlglot.parse_one("SELECT 1" + " UNION SELECT 1 INTERSECT SELECT 1" * 8, read="hive")
            list(_build_scope(_ch).traverse())
        finally:
            sys.monitoring.set_events(_tid, 0)
        (out / "scope-stress-lines.json").write_text(
            json.dumps({p: sorted(lns) for p, lns in _cur_hits.items()}, separators=(",", ":"))
        )

        # 3. TestOptimizer.test_annotate_funcs: grouped by meta.get("dialect") or ""
        _af_schema = {
            "tbl": {
                "bin_col": "BINARY", "str_col": "STRING", "bignum_col": "BIGNUMERIC",
                "date_col": "DATE", "decfloat_col": "DECFLOAT", "float_col": "FLOAT",
                "timestamp_col": "TIMESTAMP", "double_col": "DOUBLE", "bigint_col": "BIGINT",
                "smallint_col": "SMALLINT", "bit_col": "BIT", "obj_col": "OBJECT",
                "int_col": "INT", "bool_col": "BOOLEAN", "bytes_col": "BYTES",
                "interval_col": "INTERVAL", "array_col": "ARRAY<STRING>",
            }
        }
        _af_by_group = {}
        for _meta, _sql, _exp_t in _load_pairs("optimizer/annotate_functions.sql"):
            _gk = _meta.get("dialect") or ""
            _af_by_group.setdefault(_gk, []).append((_sql, _exp_t))
        _af_groups = sorted(_af_by_group)
        _af_counts = [len(_af_by_group[g]) for g in _af_groups]
        _af_hits = {}
        _af_file_groups = {}
        for _gi, _gk in enumerate(_af_groups):
            _cur_hits = {}
            sys.monitoring.restart_events()
            sys.monitoring.set_events(_tid, sys.monitoring.events.LINE)
            try:
                for _sql, _exp_t in _af_by_group[_gk]:
                    _full_sql = f"SELECT {_sql} FROM tbl"
                    for _d in _gk.split(", "):
                        _r = _annotate_types(
                            sqlglot.parse_one(_full_sql, read=_d), dialect=_d, schema=_af_schema
                        ).expressions[0]
                        _r.type.sql(_d)
                        sqlglot.exp.DataType.build(_exp_t, dialect=_d).sql(_d)
            finally:
                sys.monitoring.set_events(_tid, 0)
            _bit = 1 << _gi
            for _p, _lns in _cur_hits.items():
                _af_file_groups[_p] = _af_file_groups.get(_p, 0) | _bit
                _fh = _af_hits.setdefault(_p, {})
                for _ln in _lns:
                    _fh[_ln] = _fh.get(_ln, 0) | _bit
        (out / "annotate-funcs-hits.marshal").write_bytes(
            marshal.dumps({
                "groups": _af_groups,
                "counts": _af_counts,
                "hits": _af_hits,
                "file_groups": _af_file_groups,
            })
        )

        # 4. Stress-loop line profiles for C35 scaled targets (long_cte, serde_rec, deep_ann)
        #    and line coverage for cache_schema + 5 opt-medium unreached targets.
        from sqlglot.optimizer.qualify import qualify as _qualify
        from sqlglot.schema import MappingSchema as _MappingSchema
        from sqlglot.serde import dump as _serde_dump, load as _serde_load
        import gc as _gc, weakref as _weakref

        _c35_stress = {}
        # 4a. long_cte stress (N=8)
        _cur_hits = {}
        sys.monitoring.restart_events()
        sys.monitoring.set_events(_tid, sys.monitoring.events.LINE)
        try:
            for _join in ("", "JOIN", "SEMI JOIN", "CORRELATED"):
                _ctes = ["c0 AS (SELECT s FROM t)"]
                for _ci in range(1, 8):
                    _src = "t AS l" if _join == "SEMI JOIN" else f"c{_ci - 1} AS l"
                    if _join == "CORRELATED":
                        _proj = "(SELECT l.s) AS s"
                    else:
                        _proj = "l.s"
                        if _join:
                            _src += f" {_join} c{_ci - 1} AS r ON l.s = r.s"
                    _ctes.append(f"c{_ci} AS (SELECT {_proj} FROM {_src})")
                _q = _qualify(
                    sqlglot.parse_one(f"WITH {', '.join(_ctes)} SELECT (s).* FROM c7", dialect="risingwave"),
                    schema={"t": {"s": "STRUCT<x INT>"}},
                    dialect="risingwave",
                )
                _annotate_types(_q, schema={"t": {"s": "STRUCT<x INT>"}}, dialect="risingwave")
        finally:
            sys.monitoring.set_events(_tid, 0)
        _c35_stress["long_cte"] = {p: sorted(lns) for p, lns in _cur_hits.items()}

        # 4b. serde_rec stress (N=10)
        _cur_hits = {}
        sys.monitoring.restart_events()
        sys.monitoring.set_events(_tid, sys.monitoring.events.LINE)
        try:
            _se = sqlglot.parse_one("SELECT 1" + " UNION ALL SELECT 1" * 10)
            _se.sql()
            _serde_load(_serde_dump(_se)).sql()
            pickle.loads(pickle.dumps(_se)).sql()
        finally:
            sys.monitoring.set_events(_tid, 0)
        _c35_stress["serde_rec"] = {p: sorted(lns) for p, lns in _cur_hits.items()}

        # 4c. deep_ann stress (N=10)
        _cur_hits = {}
        sys.monitoring.restart_events()
        sys.monitoring.set_events(_tid, sys.monitoring.events.LINE)
        try:
            _us = "SELECT 1 UNION ALL " * 10 + "SELECT 1"
            _annotate_types(sqlglot.parse_one(_us)).sql()
            _bs = "SELECT " + "t.a + " * 10 + "t.a FROM t"
            _annotate_types(sqlglot.parse_one(_bs), schema={"t": {"a": "INT"}}).sql()
        finally:
            sys.monitoring.set_events(_tid, 0)
        _c35_stress["deep_ann"] = {p: sorted(lns) for p, lns in _cur_hits.items()}

        # 4d. cache_schema full trace
        _cur_hits = {}
        sys.monitoring.restart_events()
        sys.monitoring.set_events(_tid, sys.monitoring.events.LINE)
        try:
            class _Mk:
                pass
            for _norm in (False, True):
                for _mname, _margs in (
                    ("find", ()), ("column_names", ()), ("get_column_type", ("id",)),
                    ("has_column", ("id",)), ("add_table", ({"id": "INT"},)),
                ):
                    _sc = _MappingSchema({"items": {"id": "INT"}}, dialect="duckdb", normalize=_norm)
                    _qq = sqlglot.parse_one("SELECT id FROM items", dialect="duckdb")
                    _tb = _qq.find(sqlglot.exp.Table)
                    _mk = _Mk()
                    _qq.meta["marker"] = _tb.meta["marker"] = _mk
                    _wr = _weakref.ref(_mk)
                    getattr(_sc, _mname)(_tb, *_margs)
                    del _qq, _tb, _mk
                    _gc.collect()
                    assert _wr() is None
                    assert _sc.column_names("items") == ["id"]
        finally:
            sys.monitoring.set_events(_tid, 0)
        _c35_stress["cache_schema"] = {p: sorted(lns) for p, lns in _cur_hits.items()}
        (out / "c35-stress-lines.json").write_text(json.dumps(_c35_stress, separators=(",", ":")))

        # 5. Pre-warmed line profiles for 5 top excess opt-medium tests (unreached/imported proofs)
        from concurrent.futures import Future as _Future
        import tests.test_optimizer as _topt_mod

        class _InlinePool:
            def submit(self, fn, *a, **kw):
                f = _Future()
                try:
                    f.set_result(fn(*a, **kw))
                except BaseException as e:
                    f.set_exception(e)
                return f
            def shutdown(self, *a, **kw):
                pass

        _orig_ppe = _topt_mod.ProcessPoolExecutor
        _topt_mod.ProcessPoolExecutor = lambda *a, **kw: _InlinePool()
        _orig_gor = sqlglot.Dialect.get_or_raise.__func__
        _used_dialects = set()

        @classmethod
        def _track_gor(cls, dialect_type):
            res = _orig_gor(cls, dialect_type)
            _used_dialects.add(type(res).__name__.lower() if type(res) is not sqlglot.Dialect else "")
            return res

        sqlglot.Dialect.get_or_raise = _track_gor
        _opt_med = {}
        try:
            _topt_mod.TestOptimizer.setUpClass()
            for _mkey in (
                "test_qualify_columns",
                "test_optimize_in_nulls",
                "test_merge_subqueries",
                "test_optimize_correlated_all",
                "test_simplify",
            ):
                _inst = _topt_mod.TestOptimizer(_mkey)
                _inst.setUp()
                _cur_hits = {}
                _used_dialects = {""}
                sys.monitoring.restart_events()
                sys.monitoring.set_events(_tid, sys.monitoring.events.LINE)
                try:
                    getattr(_inst, _mkey)()
                finally:
                    sys.monitoring.set_events(_tid, 0)
                _opt_med[_mkey] = {
                    "files": sorted(_cur_hits),
                    "dialects": sorted(_used_dialects),
                    "hits": {p: sorted(lns) for p, lns in _cur_hits.items()},
                }
        finally:
            sqlglot.Dialect.get_or_raise = classmethod(_orig_gor)
            _topt_mod.ProcessPoolExecutor = _orig_ppe
        (out / "opt-medium-hits.marshal").write_bytes(marshal.dumps(_opt_med))
    finally:
        sys.monitoring.set_events(_tid, 0)
        sys.monitoring.register_callback(_tid, sys.monitoring.events.LINE, None)
        sys.monitoring.free_tool_id(_tid)

    if Path("/app/staged.json").is_file():
        (out / "staged.marshal").write_bytes(marshal.dumps(json.loads(Path("/app/staged.json").read_text())))

    from selector import query_slice, rule_corpus
    for name, rows, outside in (
        ("query", query_slice.trace()["all"], [query_slice.trace()["setup"], query_slice.trace()["teardown"]]),
        ("worker", query_slice.trace()["worker"], []),
        ("rules", rule_corpus.index()[0], [rule_corpus.index()[1]]),
    ):
        hits = {}
        for i, row in enumerate(rows):
            bit = 1 << i
            for path, lines in row.items():
                file_hits = hits.setdefault(path, {})
                for line in lines:
                    file_hits[line] = file_hits.get(line, 0) | bit
        for row in outside:
            for path, lines in row.items():
                file_hits = hits.setdefault(path, {})
                for line in lines:
                    file_hits[line] = -1
        (out / f"{name}-hits.marshal").write_bytes(marshal.dumps(hits))
