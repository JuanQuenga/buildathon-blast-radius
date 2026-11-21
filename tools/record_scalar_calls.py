"""Record the SQL executor's scalar calls on the complete pinned TPC-DS data."""
from collections import Counter
import dis
import functools
import gzip
import pickle
import sys
from pathlib import Path

sys.path[:0] = ["/app/sqlglot", "/app/selector"]
from sqlglot.executor.env import ENV
from tests import test_executor as tests
from result_replay import table_digest
import json


@functools.cache
def successors(code):
    instructions = list(dis.get_instructions(code, show_caches=True))
    next_op = ""
    out = {}
    for op in reversed(instructions):
        out[op.offset] = next_op
        if op.opname != "CACHE":
            next_op = op.opname
    return out


def truth_only(frame):
    # Direct return from the compiled SQL expression through Context.eval
    # into a conditional jump in the unchanged executor.
    if successors(frame.f_code).get(frame.f_lasti) != "RETURN_VALUE":
        return False
    caller = frame.f_back
    if caller.f_code.co_filename != "/app/sqlglot/sqlglot/executor/context.py" or caller.f_code.co_name != "eval":
        return False
    owner = caller.f_back
    return (owner.f_code.co_filename == "/app/sqlglot/sqlglot/executor/python.py"
            and successors(owner.f_code).get(owner.f_lasti) in {"TO_BOOL", "POP_JUMP_IF_TRUE", "POP_JUMP_IF_FALSE"})


def wrap(name, original):
    @functools.wraps(original)
    def call(*args):
        wrapped[index, name] += 1
        observed = []
        def thunk(i, fn):
            def get():
                value = fn()
                observed.append((i, value))
                return value
            return get
        lazy = name in ("sql_and", "sql_or")
        result = original(*(thunk(i, fn) for i, fn in enumerate(args))) if lazy else original(*args)
        values = [v for i, v in observed] if lazy else args
        if any(type(v) not in (str, int, float, bool, type(None)) for v in values):
            unsupported.add((index, name))
        else:
            records.setdefault((index, name), set()).add(pickle.dumps(
                (tuple(observed) if lazy else args, result, truth_only(sys._getframe(1))), protocol=4))
        return result
    return call


queries, tables = pickle.loads(Path("/app/staged-tables.pkl").read_bytes())
expected = json.loads(Path("/app/staged.json").read_text())["ds"]
records, unsupported = {}, set()
actual, wrapped = Counter(), Counter()
codes = {}
def entered(code, offset):
    name = codes.get(code)
    if name:
        actual[index, name] += 1
    else:
        return sys.monitoring.DISABLE

for key in ("AND", "OR", "IN", "NOT"):
    if key in ENV and getattr(ENV[key], "__name__", "").startswith("sql_"):
        codes[ENV[key].__code__] = ENV[key].__name__
        ENV[key] = wrap(ENV[key].__name__, ENV[key])
tests.initializer(tests.TPCDS_SCHEMA, tables)
sys.monitoring.use_tool_id(4, "scalar calls")
sys.monitoring.register_callback(4, sys.monitoring.events.PY_START, entered)
sys.monitoring.set_events(4, sys.monitoring.events.PY_START)
for index, (meta, sql, _) in enumerate(queries):
    expr = pickle.loads(pickle.dumps(tests.parse_one(sql)))
    table = tests.mp_execute(expr, meta)
    if table is not None:
        assert table_digest(table) == expected[index][-1]
sys.monitoring.set_events(4, 0)
unsupported.update(k for k in actual if actual[k] != wrapped[k])
print(len(records), len(unsupported), sum(map(len, records.values())))
Path(sys.argv[1]).write_bytes(gzip.compress(pickle.dumps(
    {key: sorted(values) for key, values in sorted(records.items()) if key not in unsupported}), mtime=0))
