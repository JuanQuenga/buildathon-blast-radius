"""Record the largest nested-loop input product while checking every pinned TPC-DS result."""
import json
from pathlib import Path
import pickle
import sys

sys.path[:0] = ["/app/sqlglot", "/app/selector"]
from result_replay import table_digest
from sqlglot.executor import PythonExecutor
from tests import test_executor as tests

fixtures, tables = pickle.loads(Path("/app/staged-tables.pkl").read_bytes())
reference = json.loads(Path("/app/staged.json").read_bytes())["ds"]
assert len(fixtures) == len(reference) == 99
tests.initializer(tests.TPCDS_SCHEMA, tables)
nested_loop = PythonExecutor.nested_loop_join
largest = 0


def record(engine, join, left, right, condition, context):
    global largest
    largest = max(largest, len(left.table.rows) * len(right.table.rows))
    return nested_loop(engine, join, left, right, condition, context)


PythonExecutor.nested_loop_join = record
checked = 0
try:
    for i, (meta, sql, _) in enumerate(fixtures):
        expression = pickle.loads(pickle.dumps(tests.parse_one(sql)))
        result = tests.mp_execute(expression, meta)
        if result is None:
            assert not meta.get("execute")
        else:
            assert table_digest(result) == reference[i][-1]
            checked += 1
finally:
    PythonExecutor.nested_loop_join = nested_loop
assert checked == 57 and largest > 0
Path(sys.argv[1]).write_text(json.dumps({"maximum_nested_loop_pairs": largest}) + "\n")
