"""Instrument a clean SQLGlot pytest run. BR_IDENTITY_OUT must be an empty directory."""
import ast
import builtins
import atexit
import json
from enum import Enum
import os
from pathlib import Path
from importlib.machinery import SourceFileLoader

out = os.environ.get("BR_IDENTITY_OUT")
seen = set()


def observe(site, left, right, negate):
    identity = left is right
    # Only evaluate an alternate comparison when both implementations are pure.
    safe = False
    scalars = (type(None), bool, int, float, str, bytes)
    if type(left) in scalars and type(right) in scalars:
        safe = (left == right) == identity and (left != right) != identity
    elif right is None or left is None:
        other = type(left if right is None else right)
        safe = (other.__eq__ is object.__eq__ and other.__ne__ is object.__ne__)
    elif isinstance(left, Enum) and type(left) is type(right):
        cls = type(left)
        if cls.__eq__ is str.__eq__ and cls.__ne__ is str.__ne__:
            safe = (left == right) == identity and (left != right) != identity
        elif cls.__eq__ is object.__eq__ and cls.__ne__ is object.__ne__:
            safe = True
    seen.add((site, safe))
    return not identity if negate else identity


original = SourceFileLoader.get_code


def get_code(loader, fullname):
    if not fullname.startswith("sqlglot."):
        return original(loader, fullname)
    source = loader.get_data(loader.path)
    tree = ast.parse(source, loader.path)
    class Replace(ast.NodeTransformer):
        changed = False

        def visit_Compare(self, node):
            self.generic_visit(node)
            if len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Is, ast.IsNot)):
                return node
            left = node.left
            self.changed = True
            site = f"{fullname}-{node.lineno}-{node.col_offset}"
            args = [ast.Constant(site), left, node.comparators[0], ast.Constant(isinstance(node.ops[0], ast.IsNot))]
            return ast.copy_location(ast.Call(ast.Name("_br_observe_identity", ast.Load()), args, []), node)

    transform = Replace()
    transform.visit(tree)
    if not transform.changed:
        return original(loader, fullname)
    return compile(ast.fix_missing_locations(tree), loader.path, "exec", dont_inherit=True)


if out:
    pid = os.getpid()
    (Path(out) / f"{pid}.start").touch()
    def save():
        (Path(out) / f"{os.getpid()}.json").write_text(json.dumps(sorted(seen)))
    atexit.register(save)
    builtins._br_observe_identity = observe
    SourceFileLoader.get_code = get_code
