"""Compare scalar executor calls on the full pin's observed inputs."""
import ast
import gzip
from pathlib import Path
import pickle

PATH = "sqlglot/executor/env.py"
NAMES = {"sql_in", "sql_not", "sql_and", "sql_or"}


def edited_function(before, after):
    old, new = ast.parse(before), ast.parse(after)
    for a, b in zip(old.body, new.body):
        if not isinstance(a, ast.FunctionDef) or a.name not in NAMES or not isinstance(b, ast.FunctionDef):
            continue
        body = b.body
        if ast.dump(a) == ast.dump(b):
            continue
        b.body = a.body
        if ast.dump(old) != ast.dump(new):
            return None
        # Only local scalar operations are replayable. Placing arbitrary code
        # inside the overlay would invalidate the recorded call boundary.
        locals_ = {v.id for v in ast.walk(a) if isinstance(v, ast.Name)}
        permitted = (ast.Return, ast.If, ast.Assign, ast.For, ast.Expr, ast.Compare,
                     ast.BoolOp, ast.UnaryOp, ast.IfExp, ast.Name, ast.Constant,
                     ast.Load, ast.Store, ast.Is, ast.IsNot, ast.Eq, ast.NotEq,
                     ast.And, ast.Or, ast.Not, ast.Call, ast.Pass)
        for stmt in body:
            for node in ast.walk(stmt):
                if not isinstance(node, permitted):
                    return None
                if isinstance(node, ast.Name) and node.id not in locals_:
                    return None
                if isinstance(node, ast.Call) and not (
                    isinstance(node.func, ast.Name) and node.func.id in {"bool", "left", "right"}
                    and not node.keywords and len(node.args) == int(node.func.id == "bool")
                ):
                    return None
        return a.name
    return None


def equivalent(selection):
    from sqlglot.executor import env
    from phase import mark

    name, candidates = selection["scalar"]
    fn = getattr(env, name)
    with gzip.open(Path(__file__).parent / "maps/scalar-calls.pkl.gz", "rb") as stream:
        records = pickle.load(stream)
    known = {}
    verified = set()
    mark("scalar")
    for i in candidates:
        cases = records.get((i, name))
        if not cases:
            continue
        for raw in cases:
            if raw not in known:
                try:
                    known[raw] = matches(fn, name, raw)
                except (ValueError, TypeError, StopIteration):
                    known[raw] = False
            if not known[raw]:
                break
        else:
            verified.add(i)
    return verified


def matches(fn, name, raw):
    args, expected, truth_only = pickle.loads(raw)
    if name in ("sql_and", "sql_or"):
        calls = iter(args)
        def read(index):
            which, value = next(calls)
            if which != index:
                raise ValueError("thunk order changed")
            return value
        try:
            result = fn(lambda: read(0), lambda: read(1))
        except (ValueError, StopIteration, TypeError):
            return False
        if next(calls, None) is not None:
            return False
    else:
        result = fn(*args)
    if truth_only:
        return bool(result) == bool(expected)
    return type(result) is type(expected) and result == expected
