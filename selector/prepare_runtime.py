"""Precompute repeated operations on pinned sources inside the image's Python."""
import gzip
import marshal
import py_compile
import sys
from pathlib import Path
import re
import sqlite3
import tarfile

from selector.impact import _branch_map, _index, _operand_map, _sources
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

    _pygen_map = {}
    for _i, (_meta, _sql, _) in enumerate(queries):
        if not _meta.get("execute"):
            continue
        _opt = _ex.optimize(sqlglot.parse_one(_sql), schema=_TPCDS_SCHEMA, leave_tables_isolated=True)
        _plan = _ex.Plan(_opt)
        _eng = _ex.PythonExecutor(tables={})
        _pygen_map[str(_i)] = _pygen_digest(_eng, _plan, _planner, _Gen, _staged_digest)
    (out / "pygen-digests.json").write_text(json.dumps(_pygen_map, separators=(",", ":")))

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
