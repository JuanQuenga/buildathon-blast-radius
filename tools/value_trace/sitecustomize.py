"""Observe the arguments of nested SQLGlot value builders on the pristine pin."""
import ast
import atexit
import builtins
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import sys
import tracemalloc

out = os.environ.get("BR_VALUES")
original = SourceFileLoader.get_code
observed = {}


def table_value(fn, name):
    return fn(name), fn, type(name) is str


def value_call(site, alias_fn, constructed, alias, copy):
    table, table_fn, string_name = constructed
    from sqlglot import exp
    from sqlglot.expressions import builders, core
    # A new Table and its Identifier have no external owners. The original
    # inputs are immutable and none of these constructors are wrapped.
    functions = (alias_fn, table_fn, exp.Table.copy, exp.Identifier.copy,
                 exp.Table.__deepcopy__, exp.Identifier.__deepcopy__)
    expected = ((core.__file__, "alias_"), (builders.__file__, "table_"),
                (core.__file__, "copy"), (core.__file__, "copy"),
                (core.__file__, "__deepcopy__"), (core.__file__, "__deepcopy__"))
    safe = (string_name and type(alias) is str and type(table) is exp.Table and not tracemalloc.is_tracing()
            and sys.getprofile() is None and sys.gettrace() is None and all(
                hasattr(fn, "__code__") and (fn.__code__.co_filename, fn.__code__.co_name) == signature
                for fn, signature in zip(functions, expected)))
    observed[site] = observed.get(site, True) and safe
    return alias_fn(table, alias=alias, copy=copy)


def site_parts(node):
    if not isinstance(node, ast.Call) or ast.unparse(node.func) != "exp.alias_" or len(node.args) != 1:
        return
    inner = node.args[0]
    if (not isinstance(inner, ast.Call) or ast.unparse(inner.func) != "exp.table_"
            or len(inner.args) != 1 or inner.keywords or not isinstance(inner.args[0], ast.Name)):
        return
    keys = {kw.arg: kw.value for kw in node.keywords}
    if list(keys) != ["alias", "copy"] or not isinstance(keys["copy"], ast.Constant):
        return
    alias = keys["alias"]
    if not (isinstance(alias, ast.Attribute) and isinstance(alias.value, ast.Name)
            and alias.attr == "alias_or_name"):
        return
    constructed = ast.Call(ast.Name("_br_table_value", ast.Load()), [inner.func, inner.args[0]], [])
    return [node.func, constructed, alias, keys["copy"]]


def get_code(loader, module):
    if not module.startswith("sqlglot."):
        return original(loader, module)
    source = loader.get_data(loader.path)
    if b"exp.alias_(" not in source or b"exp.table_(" not in source:
        return original(loader, module)

    class Observe(ast.NodeTransformer):
        def visit_Call(self, node):
            parts = site_parts(node)
            if parts:
                site = f"{module}:{node.lineno}:{node.col_offset}"
                return ast.copy_location(ast.Call(ast.Name("_br_value_call", ast.Load()),
                                                  [ast.Constant(site), *parts], []), node)
            return self.generic_visit(node)

    tree = Observe().visit(ast.parse(source, loader.path))
    return compile(ast.fix_missing_locations(tree), loader.path, "exec", dont_inherit=True)


if out:
    (Path(out) / f"{os.getpid()}.start").touch()
    atexit.register(lambda: (Path(out) / f"{os.getpid()}.json").write_text(json.dumps(observed)))
    builtins._br_table_value = table_value
    builtins._br_value_call = value_call
    SourceFileLoader.get_code = get_code
