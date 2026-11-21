"""Record the owning tokenizer class and key at pinned numeric escape lookups."""
import ast
import atexit
import builtins
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path

out = Path(os.environ["BR_TOKEN_TRACE"])
owners = {}
hits = set()
installed = set()
original = SourceFileLoader.get_code


def register(core, tokenizer):
    table = core.numeric_escapes
    base = next((c for c in type(tokenizer).__mro__ if "NUMERIC_ESCAPES" in vars(c)), None)
    if base and table is vars(base)["NUMERIC_ESCAPES"]:
        entry = owners.setdefault(id(table), (table, set()))
        entry[1].add(base.__module__ + "." + base.__qualname__)
    return core


def lookup(table, key):
    entry = owners.get(id(table))
    for owner in entry[1] if entry and entry[0] is table else ("*",):
        hits.add((owner, key))
    return table.get(key)


def instrument(loader, name):
    if name not in {"sqlglot.tokenizer_core", "sqlglot.tokens"}:
        return original(loader, name)
    tree = ast.parse(loader.get_data(loader.path), loader.path)
    count = 0
    for node in ast.walk(tree):
        if (name == "sqlglot.tokens" and isinstance(node, ast.FunctionDef)
                and node.name == "_init_core"):
            ret = node.body[-1]
            assert isinstance(ret, ast.Return)
            ret.value = ast.copy_location(ast.Call(ast.Name("_br_register", ast.Load()),
                      [ret.value, ast.Name("self", ast.Load())], []), ret.value)
            count += 1
        if (name == "sqlglot.tokenizer_core" and isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "numeric_escapes"):
            assert len(node.args) == 1 and not node.keywords
            node.args.insert(0, node.func.value)
            node.func = ast.copy_location(ast.Name("_br_lookup", ast.Load()), node.func)
            count += 1
    assert count == (1 if name == "sqlglot.tokens" else 2)
    installed.add(name)
    return compile(ast.fix_missing_locations(tree), loader.path, "exec", dont_inherit=True)


def save():
    (out / f"{os.getpid()}.json").write_text(json.dumps([sorted(installed), sorted(hits)]))


(out / f"{os.getpid()}.start").touch()
builtins._br_register = register
builtins._br_lookup = lookup
SourceFileLoader.get_code = instrument
atexit.register(save)
