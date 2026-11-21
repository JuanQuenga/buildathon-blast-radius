"""Observe truth decisions in complete pinned heavy-test runs, including spawn."""
import ast
import builtins
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


class Conditions(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module

    def wrap(self, value, key):
        return ast.copy_location(ast.Call(ast.Name("_br_branch_truth", ast.Load()),
                                          [ast.Constant(key), value], []), value)

    def visit_If(self, node):
        self.generic_visit(node)
        test = node.test
        site = f"{self.module}:{test.lineno}:{test.col_offset}"
        if isinstance(test, ast.BoolOp):
            # Only reached when the prefix is true for AND, false for OR.
            test.values[-1] = self.wrap(test.values[-1], site + ":tail")
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
    builtins._br_branch_truth = observe
    SourceFileLoader.get_code = instrument
