"""Identify deleted expression-type entries in a pinned literal return-type map."""
import ast
import functools
import json
from pathlib import Path

from . import impact


def expression_set(node):
    if isinstance(node, ast.Dict) and not node.keys:
        return set()
    if not isinstance(node, ast.Set):
        return None
    if not all(isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name)
               and x.value.id == "exp" for x in node.elts):
        return None
    return {x.attr for x in node.elts}


def removed_keys(path, after):
    if not path.startswith("sqlglot/typing/") or not path.endswith(".py"):
        return None
    before = impact._sources()["src"][path]
    old, new = ast.parse(before), ast.parse(after)
    removed = set()
    for a, b in zip(old.body, new.body):
        if not (isinstance(a, ast.Assign) and isinstance(b, ast.Assign)
                and len(a.targets) == 1 and isinstance(a.targets[0], ast.Name)
                and a.targets[0].id == "EXPRESSION_METADATA"
                and isinstance(a.value, ast.Dict) and isinstance(b.value, ast.Dict)):
            continue
        for x, y in zip(a.value.values, b.value.values):
            if not (isinstance(x, ast.DictComp) and isinstance(y, ast.DictComp)
                    and len(x.generators) == len(y.generators) == 1):
                continue
            comp = x.generators[0]
            if not (isinstance(comp.target, ast.Name) and isinstance(x.key, ast.Name)
                    and x.key.id == comp.target.id and not comp.ifs and not comp.is_async
                    and isinstance(x.value, ast.Dict) and len(x.value.keys) == 1
                    and isinstance(x.value.keys[0], ast.Constant) and x.value.keys[0].value == "returns"):
                continue
            value = x.value.values[0]
            if not (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Attribute)
                    and value.value.attr == "DType" and isinstance(value.value.value, ast.Name)
                    and value.value.value.id == "exp"):
                continue
            lhs = expression_set(comp.iter)
            rhs = expression_set(y.generators[0].iter)
            if lhs is not None and rhs is not None and rhs < lhs:
                removed.update(lhs - rhs)
                y.generators[0].iter = comp.iter
    if removed and ast.dump(old) == ast.dump(new):
        return removed
    return None


@functools.cache
def observations():
    return json.loads((Path(__file__).parent / "maps/metadata-keys.json").read_text())
