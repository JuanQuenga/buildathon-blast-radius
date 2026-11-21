"""Compare complete Python functions on the control paths observed at the pin."""
import ast
import collections
import copy


def residual(fn, choices):
    """Keep the evaluated guard in the residual so motion across it is visible."""
    class Fold(ast.NodeTransformer):
        def visit_If(self, node):
            key = ast.dump(node.test)
            choice = choices.get(key)
            if choice is None:
                return self.generic_visit(node)
            # This is an internal comparison node, never compiled or executed.
            # A tuple object cannot occur in the patched tree as an AST node.
            marker = ast.Expr(value=ast.Constant(value=(key, choice)))
            statements = node.body if choice else node.orelse
            return [marker, *self.visit(ast.Module(body=statements, type_ignores=[])).body]

    return ast.dump(Fold().visit(copy.deepcopy(fn)))


def equivalent(path, old, new, sites):
    if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                          ast.Global, ast.Nonlocal, ast.Yield, ast.YieldFrom))
           for statement in old.body + new.body for n in ast.walk(statement)):
        return False
    def bindings(fn):
        return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)
                and isinstance(n.ctx, (ast.Store, ast.Del))}
    if bindings(old) != bindings(new):
        return False
    occurrences = collections.Counter(ast.dump(n.test) for n in ast.walk(old) if isinstance(n, ast.If))
    choices = {}
    for node in ast.walk(old):
        if isinstance(node, ast.If):
            test = node.test
            key = ast.dump(test)
            site = f"{path}:{test.lineno}:{test.col_offset}"
            if occurrences[key] == 1 and site in sites:
                choices[key] = sites[site]
    return bool(choices) and residual(old, choices) == residual(new, choices)


def module_equivalent(path, before, after, sites):
    old, new = ast.parse(before), ast.parse(after)
    a = [n for n in ast.walk(old) if isinstance(n, ast.FunctionDef)]
    b = [n for n in ast.walk(new) if isinstance(n, ast.FunctionDef)]
    if len(a) != len(b):
        return False
    changed = False
    for left, right in zip(a, b):
        if ast.dump(left) == ast.dump(right):
            continue
        if left.name != right.name or not equivalent(path, left, right, sites):
            return False
        right.body = left.body
        changed = True
    return changed and ast.dump(old) == ast.dump(new)
