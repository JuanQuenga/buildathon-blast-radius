"""Precompute repeated operations on pinned sources inside the image's Python."""
import marshal
from pathlib import Path
import re
import sqlite3

from selector.impact import _sources

out = Path("/app/pin-cache")
out.mkdir()
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
