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
