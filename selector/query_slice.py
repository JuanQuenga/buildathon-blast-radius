"""Locate changed statements inside existing methods and intersect pin traces."""
import ast
import functools
import gzip
import json
import marshal
from pathlib import Path


def spans(before, after):
    old, new = ast.parse(before), ast.parse(after)
    regions = []

    def equal(a, b):
        return ast.dump(a) == ast.dump(b)

    def diff(a, b):
        if equal(a, b):
            return
        # Only descend through identical block headers. A new branch, moved
        # statement or insertion dirties its entire enclosing statement.
        blocks = []
        if type(a) is type(b):
            for field in a._fields:
                x, y = getattr(a, field), getattr(b, field)
                if field in {"body", "orelse", "finalbody"} and isinstance(x, list) and len(x) == len(y):
                    blocks.extend(zip(x, y))
                elif isinstance(x, ast.AST) and isinstance(y, ast.AST) and equal(x, y):
                    continue
                elif isinstance(x, list) and isinstance(y, list) and len(x) == len(y) and all(
                    equal(i, j) if isinstance(i, ast.AST) and isinstance(j, ast.AST) else i == j
                    for i, j in zip(x, y)
                ):
                    continue
                elif x != y:
                    break
            else:
                for x, y in blocks:
                    diff(x, y)
                return
        regions.append((a.lineno, a.end_lineno))

    def scope_bindings(fn):
        bindings = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bindings.add(("local", node.id))
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                bindings.update((type(node).__name__, name) for name in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                bindings.add(("definition", node.name))
            elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
                bindings.add(("binding", node.name))
            elif isinstance(node, (ast.Yield, ast.YieldFrom)):
                bindings.add(("generator", ""))
        return bindings

    # Replacing just the edited bodies must reconstruct the complete original
    # module, including all class definitions, decorators, and default values.
    pairs = list(zip(old.body, new.body))
    for fn, other in pairs:
        if isinstance(fn, ast.ClassDef) and isinstance(other, ast.ClassDef):
            pairs.extend(zip(fn.body, other.body))
            continue
        if not isinstance(fn, ast.FunctionDef) or not isinstance(other, ast.FunctionDef):
            continue
        if fn.name != other.name or fn.name.startswith("__") or equal(fn, other):
            continue
        # A store in a dormant branch can change a live global lookup into
        # an unbound local. This is a whole-function change at compile time.
        old_scope, new_scope = scope_bindings(fn), scope_bindings(other)
        if ("generator", "") in old_scope.symmetric_difference(new_scope):
            return None
        if len(fn.body) == len(other.body) and old_scope == new_scope:
            for x, y in zip(fn.body, other.body):
                diff(x, y)
        else:
            regions.append((fn.body[0].lineno, fn.end_lineno))
        other.body = fn.body
    return regions if regions and equal(old, new) else None


@functools.cache
def trace():
    with gzip.open("/app/query-lines.json.gz") as stream:
        data = json.load(stream)
    if len(data["worker"]) != 99 or len(data["all"]) != 99:
        raise ValueError("incomplete trace")
    return data


def reached(row, changed):
    return any(lo <= n <= hi for path, spans in changed.items()
               for n in row.get(path, ()) for lo, hi in spans)


@functools.cache
def _hits(name):
    return marshal.loads(Path(f"/app/pin-cache/{name}-hits.marshal").read_bytes())


def indices(name, changed):
    index = _hits(name)
    bits = 0
    for path, spans in changed.items():
        file_hits = index.get(path, {})
        for lo, hi in spans:
            for line in range(lo, hi + 1):
                bits |= file_hits.get(line, 0)
    if bits < 0:
        return None
    result = []
    while bits:
        bit = bits & -bits
        result.append(bit.bit_length() - 1)
        bits ^= bit
    return result


def query_cases(changed):
    active = indices("query", changed)
    if active is None:
        return None
    return {"active": active, "worker": sorted(set(active).intersection(indices("worker", changed)))}


def keyword_edit(before, after):
    """Only string-to-TokenType entries of Tokenizer.KEYWORDS may differ."""
    def mapping(tree):
        cls = next(c for c in tree.body if isinstance(c, ast.ClassDef) and c.name == "Tokenizer")
        return next(s for s in cls.body if isinstance(s, ast.AnnAssign)
                    and isinstance(s.target, ast.Name) and s.target.id == "KEYWORDS")
    old, new = ast.parse(before), ast.parse(after)
    a, b = mapping(old), mapping(new)
    def fixed_entries(node):
        entries = []
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                    and isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name)
                    and v.value.id == "TokenType"):
                continue
            entries.append((ast.dump(k) if k else None, ast.dump(v)))
        return entries
    if not isinstance(a.value, ast.Dict) or not isinstance(b.value, ast.Dict):
        return False
    if fixed_entries(a.value) != fixed_entries(b.value):
        return False
    b.value = a.value
    return ast.dump(old) == ast.dump(new)
