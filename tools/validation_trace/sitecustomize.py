"""Record required-argument validation and SHOW dispatch on the clean pin."""
import ast
import atexit
import builtins
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path

out = os.environ.get("BR_VALIDATION")
original = SourceFileLoader.get_code
required = {}
show = set()
installed = set()


def inspect_args(expr):
    args = expr.args
    missing = {k for k in expr.arg_types if args.get(k) is None
               or isinstance(args[k], list) and not args[k]}
    # Removing or adding a key can also change iteration order of existing errors.
    if missing & expr.required_args:
        missing.update(expr.arg_types)
    for cls in type(expr).__mro__:
        if "arg_types" in vars(cls):
            required.setdefault(f"{cls.__module__}.{cls.__qualname__}", set()).update(missing)
            break
    return expr.required_args


def dispatch(mapping, key):
    show.add(key)
    return mapping[key]


def instrument(loader, module):
    if module not in {"sqlglot.expressions.core", "sqlglot.parser"}:
        return original(loader, module)
    tree = ast.parse(loader.get_data(loader.path), loader.path)
    name, before, after = (
        ("error_messages", "self.required_args", "_br_required(self)") if module.endswith(".core")
        else ("_find_parser", 'parsers[" ".join(this)]', '_br_dispatch(parsers, " ".join(this))'))
    old = ast.dump(ast.parse(before, mode="eval").body)
    class Replace(ast.NodeTransformer):
        def visit(self, node):
            if isinstance(node, (ast.Attribute, ast.Subscript)) and ast.dump(node) == old:
                installed.add(name)
                return ast.copy_location(ast.parse(after, mode="eval").body, node)
            return super().visit(node)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            Replace().visit(node)
    return compile(ast.fix_missing_locations(tree), loader.path, "exec", dont_inherit=True)


if out:
    dest = Path(out) / str(os.getpid())
    dest.with_suffix(".start").touch()
    atexit.register(lambda: dest.with_suffix(".json").write_text(json.dumps(
        [sorted(installed), sorted(show), {k: sorted(v) for k, v in required.items()}])))
    builtins._br_required = inspect_args
    builtins._br_dispatch = dispatch
    SourceFileLoader.get_code = instrument
