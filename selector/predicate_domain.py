"""Compare SQL membership predicates over pinned equality and nullness domains."""
import ast
import functools
import json
from pathlib import Path


def expression(n, state):
    if isinstance(n, ast.Constant) and (n.value is None or type(n.value) is bool):
        return n.value
    if isinstance(n, ast.Name) and n.id == "has_null":
        return state[n.id]
    if isinstance(n, ast.IfExp):
        return expression(n.body if expression(n.test, state) else n.orelse, state)
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
        return not expression(n.operand, state)
    if isinstance(n, ast.Compare) and len(n.ops) == 1 and isinstance(n.left, ast.Name):
        name, other, op = n.left.id, n.comparators[0], n.ops[0]
        if name in ("value", "candidate"):
            if isinstance(other, ast.Constant) and other.value is None and isinstance(op, (ast.Is, ast.IsNot)):
                return (state[name] is None) == isinstance(op, ast.Is)
            if isinstance(other, ast.Name) and {name, other.id} == {"value", "candidate"} and isinstance(op, (ast.Eq, ast.NotEq)):
                return (state[name] == state[other.id]) == isinstance(op, ast.Eq)
    raise ValueError("outside the equality domain")


_END = object()


def block(stmts, state):
    for n in stmts:
        if isinstance(n, ast.Return):
            return expression(n.value, state)
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "has_null":
            state["has_null"] = expression(n.value, state)
        elif isinstance(n, ast.If):
            value = block(n.body if expression(n.test, state) else n.orelse, state)
            if value is not _END:
                return value
        elif (isinstance(n, ast.For) and isinstance(n.target, ast.Name) and n.target.id == "candidate"
              and isinstance(n.iter, ast.Name) and n.iter.id == "candidates" and not n.orelse):
            for state["candidate"] in state["candidates"]:
                value = block(n.body, state)
                if value is not _END:
                    return value
        else:
            raise ValueError("outside the predicate grammar")
    return _END


@functools.cache
def domain():
    return json.loads((Path(__file__).parent / "maps/predicate-domain.json").read_text())


def equivalent(before, after, active):
    old, new = ast.parse(before), ast.parse(after)
    fn = next(f for f in old.body if isinstance(f, ast.FunctionDef) and f.name == "sql_in")
    changed = next(f for f in new.body if isinstance(f, ast.FunctionDef) and f.name == fn.name)
    body, changed.body = changed.body, fn.body
    if ast.dump(old) != ast.dump(new):
        return False
    rows = domain()
    # Each reached worker needs a complete, pinned observation.
    if not active or any(str(i) not in rows for i in active):
        return False
    if any(isinstance(n, (ast.Yield, ast.YieldFrom, ast.Global, ast.Nonlocal,
                          ast.FunctionDef, ast.ClassDef, ast.Lambda, ast.Delete))
           for statement in body for n in ast.walk(statement)):
        return False
    if sum(isinstance(n, ast.For) for s in body for n in ast.walk(s)) != 1:
        return False
    for i in active:
        for value, candidates, expected, truth_only in rows[str(i)]:
            got = block(body, {"value": value, "candidates": candidates})
            if got is _END:
                got = None
            if got is not expected and not (truth_only and bool(got) == bool(expected)):
                return False
    return True
