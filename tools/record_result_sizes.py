"""Record row and column counts after checking each pinned TPC-DS result."""
import json
from pathlib import Path
import pickle
import sys

sys.path[:0] = ["/app/sqlglot", "/app/selector"]
from result_replay import table_digest
from tests import test_executor as tests

queries, tables = pickle.loads(Path("/app/staged-tables.pkl").read_bytes())
expected = json.loads(Path("/app/staged.json").read_text())["ds"]
assert len(queries) == len(expected) == 99
tests.initializer(tests.TPCDS_SCHEMA, tables)
sizes = []
for i, (meta, sql, _) in enumerate(queries):
    expr = pickle.loads(pickle.dumps(tests.parse_one(sql)))
    result = tests.mp_execute(expr, meta)
    if result is None:
        assert not meta.get("execute")
        sizes.append(None)
    else:
        assert table_digest(result) == expected[i][-1]
        sizes.append([len(result.rows), len(result.columns)])
assert sum(row is not None for row in sizes) == 57
Path(sys.argv[1]).write_text(json.dumps(sizes, separators=(",", ":")))
