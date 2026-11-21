"""Observe value reads from the pinned tokenizer's keyword dictionary."""
import ast
import atexit
import builtins
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path

out = Path(os.environ["BR_KEYWORD_TRACE"])
original = SourceFileLoader.get_code
seen = set()
installed = False


def observe(table, key):
    if key in table or (isinstance(key, str) and len(key) <= 64 and key.isascii() and key.isidentifier()):
        seen.add(key)
    return key


def instrument(loader, name):
    global installed
    if name != "sqlglot.tokenizer_core":
        return original(loader, name)
    tree = ast.parse(loader.get_data(loader.path), loader.path)
    count = 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.In)):
            obj, key = node.comparators[0], node.left
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            obj, key = node.value, node.slice
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "get" and node.args):
            obj, key = node.func.value, node.args[0]
        else:
            continue
        if not ((isinstance(obj, ast.Name) and obj.id == "keywords")
                or (isinstance(obj, ast.Attribute) and obj.attr == "keywords")):
            continue
        wrapped = ast.copy_location(ast.Call(ast.Name("_br_keyword_read", ast.Load()),
                                              [obj, key], []), key)
        if isinstance(node, ast.Compare):
            node.left = wrapped
        elif isinstance(node, ast.Subscript):
            node.slice = wrapped
        else:
            node.args[0] = wrapped
        count += 1
    assert count == 6
    installed = True
    return compile(ast.fix_missing_locations(tree), loader.path, "exec", dont_inherit=True)


(out / f"{os.getpid()}.start").touch()
builtins._br_keyword_read = observe
SourceFileLoader.get_code = instrument
atexit.register(lambda: (out / f"{os.getpid()}.json").write_text(json.dumps([installed, sorted(seen)])))
