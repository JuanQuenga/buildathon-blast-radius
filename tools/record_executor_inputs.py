"""Run offline on the clean pin. Record full- and reduced-fixture replay controls."""
import gzip
import json
import pickle
import sys
from pathlib import Path

sys.path[:0] = ["/app/sqlglot", "/app/selector"]
from tests import test_executor as tests
from sqlglot.executor import PythonExecutor
from sqlglot.executor.table import Table
from result_replay import table_digest

out = Path(sys.argv[1])
queries, tables = pickle.loads(Path("/app/staged-tables.pkl").read_bytes())
expected = json.loads(Path("/app/staged.json").read_text())["ds"]
prefix = "/app/sqlglot/"
hits = set()


def line(code, number):
    if code.co_filename.startswith(prefix + "sqlglot/executor/"):
        hits.add((code.co_filename[len(prefix):], number))
    return sys.monitoring.DISABLE


def record(engine, plan, *args):
    sys.monitoring.set_events(4, 0)
    plans[index] = pickle.dumps((plan, tuple(engine.tables.mapping)))
    front[index] = sorted(hits)
    return execute(engine, plan, *args)


def sample(table):
    rows = table.rows
    indices = {0, len(rows) - 1}
    for col in range(len(table.columns)):
        nonnull = [j for j, row in enumerate(rows) if row[col] is not None]
        if nonnull:
            indices.update(f(nonnull, key=lambda j: rows[j][col]) for f in (min, max))
        seen = set()
        for j, row in enumerate(rows):
            if row[col] is None:
                indices.add(j)
                break
        for j in nonnull:
            if rows[j][col] in seen:
                indices.add(j)
                break
            seen.add(rows[j][col])
    indices.update(range(0, len(rows), max(1, len(rows) // 12)))
    return Table(columns=table.columns, rows=[rows[j] for j in sorted(indices) if rows])


execute = PythonExecutor.execute
plans, front, reduced = {}, {}, {}
sys.monitoring.use_tool_id(4, "prefix")
sys.monitoring.register_callback(4, sys.monitoring.events.LINE, line)
tests.initializer(tests.TPCDS_SCHEMA, tables)
for index, (meta, sql, _) in enumerate(queries):
    if meta.get("execute"):
        hits.clear()
        sys.monitoring.restart_events()
        sys.monitoring.set_events(4, sys.monitoring.events.LINE)
        tests.transpile(sql, write="duckdb")
        # Restore the original method before descending into correlated subqueries.
        def outer(engine, plan):
            PythonExecutor.execute = execute
            return record(engine, plan)
        PythonExecutor.execute = outer
        result = tests.mp_execute(pickle.loads(pickle.dumps(tests.parse_one(sql))), meta)
        assert table_digest(result) == expected[index][-1], index

from sqlglot.executor.table import ensure_tables
small = {name: sample(table) for name, table in tables.items()}
for i, raw in plans.items():
    plan, names = pickle.loads(raw)
    result = PythonExecutor(tables=ensure_tables({n: tables[n] for n in names})).execute(plan)
    assert table_digest(result) == expected[i][-1], i
    plan, names = pickle.loads(raw)
    result = PythonExecutor(tables=ensure_tables({n: small[n] for n in names})).execute(plan)
    reduced[i] = table_digest(result)
out.write_bytes(gzip.compress(pickle.dumps((plans, front), protocol=4), mtime=0))
if len(sys.argv) > 2:
    Path(sys.argv[2]).write_bytes(gzip.compress(pickle.dumps((small, reduced)), mtime=0))
