"""Observe truth decisions in complete pinned heavy-test runs, including spawn."""
import ast
import builtins
import atexit
import json
from importlib.machinery import SourceFileLoader
import os
from pathlib import Path

out = os.environ.get("BR_BRANCH_OUT")
seen = set()
original = SourceFileLoader.get_code


def observe(site, value):
    truth = bool(value)
    marker = (site, truth)
    if marker not in seen:
        (Path(out) / f"{site}.{int(truth)}").touch()
        seen.add(marker)
    return truth


values = {}


def operand(site, value):
    observed = values.setdefault(site, set())
    if observed is not None:
        if type(value) not in (str, int, bool, type(None)) or (type(value) is str and len(value) > 80):
            values[site] = None
        else:
            observed.add((type(value).__name__, value))
            if len(observed) > 64:
                values[site] = None
    return value


def save_operands():
    result = {site: None if items is None else sorted(items) for site, items in values.items()}
    (Path(out) / f"operands-{os.getpid()}.json").write_text(json.dumps(result))


class Conditions(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module

    def wrap(self, value, key):
        return ast.copy_location(ast.Call(ast.Name("_br_branch_truth", ast.Load()),
                                          [ast.Constant(key), value], []), value)

    def visit_Compare(self, node):
        self.generic_visit(node)
        if (len(node.ops) == 1 and isinstance(node.comparators[0], ast.Constant)
                and isinstance(node.ops[0], (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE))):
            site = f"{self.module}:{node.lineno}:{node.col_offset}"
            left = node.left
            node.left = ast.copy_location(ast.Call(ast.Name("_br_operand", ast.Load()),
                                                   [ast.Constant(site), left], []), left)
        return node

    def visit_If(self, node):
        self.generic_visit(node)
        test = node.test
        site = f"{self.module}:{test.lineno}:{test.col_offset}"
        if isinstance(test, ast.BoolOp):
            # Only reached when the prefix is true for AND, false for OR.
            test.values = [self.wrap(v, site + (":tail" if i == len(test.values) - 1 else f":{i}"))
                           for i, v in enumerate(test.values)]
        node.test = self.wrap(test, site)
        return node

    visit_While = visit_If


def instrument(loader, fullname):
    if not fullname.startswith("sqlglot."):
        return original(loader, fullname)
    tree = ast.parse(loader.get_data(loader.path), loader.path)
    Conditions(fullname).visit(tree)
    return compile(ast.fix_missing_locations(tree), loader.path, "exec", dont_inherit=True)


if out:
    (Path(out) / f"started-{os.getpid()}").touch()
    atexit.register(save_operands)
    builtins._br_operand = operand
    builtins._br_branch_truth = observe
    SourceFileLoader.get_code = instrument
