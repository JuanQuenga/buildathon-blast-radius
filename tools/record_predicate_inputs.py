"""Record SQL IN operands and their consumers on the pinned TPC-DS workload.

For positive predicates, eagerly visit both sides of AND/OR. Recording every
additional operand also checks that a changed short-circuit cannot expose a
new error on a row. Only plain comparisons and scalar row reads qualify.
"""
import ast
import collections
import functools
import json
from pathlib import Path
import pickle
import sys

sys.path[:0] = ["/app/sqlglot", "/app/selector"]
from sqlglot.executor.context import Context
from sqlglot.executor.env import ENV
from tests import test_executor as tests
from result_replay import table_digest


def scalar(n):
    return isinstance(n, ast.Constant) or (
        isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
        and isinstance(n.value, ast.Subscript) and isinstance(n.value.slice, ast.Constant)
        and isinstance(n.value.value, ast.Name) and n.value.value.id == "scope")


def positive(n):
    if isinstance(n, ast.Constant):
        return n.value is True or n.value is False
    if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Name) or n.keywords:
        return False
    if n.func.id in ("AND", "OR"):
        return len(n.args) == 2 and all(isinstance(x, ast.Lambda) and not x.args.args
                                        and positive(x.body) for x in n.args)
    return n.func.id in ("IN", "LTE", "GTE", "EQ", "LT", "GT") and all(map(scalar, n.args))


@functools.cache
def supported(code):
    return "IN(" in code.co_filename and positive(ast.parse(code.co_filename, mode="eval").body)


records = {}
unknown = set()
actual = collections.Counter()
wrapped = collections.Counter()
original_in = ENV["IN"]
original_eval = Context.eval
truth = False


def entered(code, offset):
    if code is original_in.__code__:
        actual[index] += 1
    else:
        return sys.monitoring.DISABLE


def observed(*args):
    wrapped[index] += 1
    value = original_in(*args)
    if not all(type(a) in (bool, int, float, str, type(None)) for a in args):
        unknown.add(index)
    else:
        key = pickle.dumps((args, value), protocol=4)
        cases = records.setdefault(index, {})
        cases[key] = truth and cases.get(key, True)
    return value


def eager_and(a, b):
    left, right = a(), b()
    return ENV["AND"](lambda: left, lambda: right)


def eager_or(a, b):
    left, right = a(), b()
    return ENV["OR"](lambda: left, lambda: right)


def visit(context, code):
    global truth
    if not supported(code):
        return original_eval(context, code)
    caller = sys._getframe(1)
    # Verify these exact uses against the pinned source (a join compares with
    # True by identity). The checker requires a three-valued boolean result.
    path = caller.f_code.co_filename
    line = source_lines.get((path, caller.f_lineno), "")
    if line not in {"if condition and not context.eval(condition):",
                    "return condition_context.eval(condition) is True"}:
        return original_eval(context, code)
    old_and, old_or = context.env["AND"], context.env["OR"]
    if old_and is not ENV["AND"] or old_or is not ENV["OR"]:
        return original_eval(context, code)
    try:
        truth = True
        context.env.update(AND=eager_and, OR=eager_or)
        return original_eval(context, code)
    finally:
        truth = False
        context.env.update(AND=old_and, OR=old_or)


path = Path("/app/sqlglot/sqlglot/executor/python.py")
source_lines = {(str(path), i): s.strip() for i, s in enumerate(path.read_text().splitlines(), 1)}
ENV["IN"] = observed
Context.eval = visit
queries, tables = pickle.loads(Path("/app/staged-tables.pkl").read_bytes())
expected = json.loads(Path("/app/staged.json").read_text())["ds"]
tests.initializer(tests.TPCDS_SCHEMA, tables)
sys.monitoring.use_tool_id(4, "predicate operands")
sys.monitoring.register_callback(4, sys.monitoring.events.PY_START, entered)
sys.monitoring.set_events(4, sys.monitoring.events.PY_START)
for index, (meta, sql, _) in enumerate(queries):
    expr = pickle.loads(pickle.dumps(tests.parse_one(sql)))
    table = tests.mp_execute(expr, meta)
    if table is not None and table_digest(table) != expected[index][-1]:
        raise RuntimeError("pinned output changed during recording")
sys.monitoring.set_events(4, 0)
assert len(queries) == 99 and actual and actual == wrapped
result = {}
for i, rows in sorted(records.items()):
    if i in unknown:
        continue
    abstract = {}
    for raw, truth_only in rows.items():
        (value, *args), output = pickle.loads(raw)
        pattern = tuple(None if a is None else int(a == value) for a in args)
        key = (None if value is None else 1, pattern, output)
        abstract[key] = truth_only and abstract.get(key, True)
    result[i] = sorted(([v, args, out, mode] for (v, args, out), mode in abstract.items()), key=repr)
Path(sys.argv[1]).write_text(json.dumps(result, separators=(",", ":"), sort_keys=True))
print(len(result), sum(map(len, result.values())), sorted(unknown))
