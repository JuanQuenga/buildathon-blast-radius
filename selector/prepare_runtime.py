"""Precompute repeated operations on pinned sources inside the image's Python."""
import marshal
import py_compile
import sys
from pathlib import Path
import re
import sqlite3

from selector.impact import _index, _sources

out = Path("/app/pin-cache")
out.mkdir()
for name, data in (("index", _index()), ("src", _sources())):
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
        queries, _ = pickle.load(stream)
    (out / "ds-inputs.json").write_text(json.dumps([sql for _, sql, _ in queries]))
