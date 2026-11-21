"""Instrument a clean SQLGlot pytest run. BR_IDENTITY_OUT must be an empty directory."""
import ast
import builtins
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
    if right is None and (left is None or (type(left).__eq__ is object.__eq__ and type(left).__ne__ is object.__ne__)):
        safe = True
    elif isinstance(left, Enum) and type(left) is type(right) and type(left).__eq__ is str.__eq__ and type(left).__ne__ is str.__ne__:
        safe = (left == right) == identity and (left != right) != identity
    key = (site, safe)
    if key not in seen:
        seen.add(key)
        (Path(out) / (site + (".agree" if safe else ".unknown"))).touch()
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
            if not isinstance(left, ast.Attribute) or not isinstance(left.value, ast.Name) or left.value.id != "self":
                return node
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
    builtins._br_observe_identity = observe
    SourceFileLoader.get_code = get_code
