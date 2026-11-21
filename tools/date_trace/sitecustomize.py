"""Profile the SQLGlot pin's runtime date-part lookup and derived set reads."""
import ast
import atexit
import builtins
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path

out = Path(os.environ["BR_DATE_TRACE"])
original = SourceFileLoader.get_code
seen = set()
installed = set()


def observe(key):
    if not isinstance(key, str):
        raise TypeError("unexpected interval lookup")
    seen.add(key)
    return key


def instrument(loader, name):
    if name not in {"sqlglot.parser", "sqlglot.dialects.dialect"}:
        return original(loader, name)
    tree = ast.parse(loader.get_data(loader.path), loader.path)
    count = 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], (ast.In, ast.NotIn))
                and isinstance(node.comparators[0], ast.Attribute)
                and node.comparators[0].attr == "VALID_INTERVAL_UNITS"):
            key = node.left
            node.left = ast.copy_location(ast.Call(ast.Name("_br_date_read", ast.Load()), [key], []), key)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "get" and isinstance(node.func.value, ast.Attribute)
              and node.func.value.attr == "DATE_PART_MAPPING"):
            key = node.args[0]
            node.args[0] = ast.copy_location(ast.Call(ast.Name("_br_date_read", ast.Load()), [key], []), key)
        else:
            continue
        count += 1
    assert count == (4 if name == "sqlglot.parser" else 1)
    installed.add(name)
    return compile(ast.fix_missing_locations(tree), loader.path, "exec", dont_inherit=True)


(out / f"{os.getpid()}.start").touch()
builtins._br_date_read = observe
SourceFileLoader.get_code = instrument
atexit.register(lambda: (out / f"{os.getpid()}.json").write_text(json.dumps([sorted(installed), sorted(seen)])))
