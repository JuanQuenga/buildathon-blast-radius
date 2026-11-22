"""Follow literal tokenizer options through Tokenizer._init_core into TokenizerCore."""
import ast
import functools
import copy
import gzip
import json
import os
from pathlib import Path


def option(before, after):
    if len(before) != 1 or len(after) != 1:
        return None
    def binding(stmt):
        if isinstance(stmt, ast.AnnAssign):
            return stmt.target, stmt.value
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            return stmt.targets[0], stmt.value
        return None, None
    a, x = binding(before[0])
    b, y = binding(after[0])
    if not isinstance(a, ast.Name) or not isinstance(b, ast.Name) or a.id != b.id:
        return None
    restored = copy.copy(after[0])
    restored.value = x
    if ast.dump(restored) != ast.dump(before[0]):
        return None
    if isinstance(x, ast.Constant) and isinstance(y, ast.Constant) and type(x.value) is type(y.value) is bool:
        return a.id
    if a.id not in {"NUMERIC_ESCAPES", "NUMERIC_LITERALS"} or not isinstance(x, ast.Dict) or not isinstance(y, ast.Dict):
        return None
    try:
        keys = lambda n: [ast.literal_eval(k) for k in n.keys]
        if keys(x) != keys(y):
            return None
        ast.literal_eval(x)
        ast.literal_eval(y)
    except (ValueError, TypeError):
        return None
    return a.id


@functools.cache
def discover_sites(name):
    from .impact import _sources
    src = _sources()["src"]
    tokenizer = "sqlglot/tokens.py"
    core = "sqlglot/tokenizer_core.py"
    tree = ast.parse(src[tokenizer])
    init = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_init_core")
    transfers = [kw for kw in ast.walk(init) if isinstance(kw, ast.keyword)
                 and isinstance(kw.value, ast.Attribute) and kw.value.attr == name
                 and isinstance(kw.value.value, ast.Name) and kw.value.value.id == "self"]
    if len(transfers) != 1:
        return None
    transfer = transfers[0]
    field = transfer.arg
    if not field:
        return None
    # All non-forwarding reads retain their pin line coverage. For the numeric
    # maps, audited key membership and truth checks survive a value-only edit.
    reads = set()
    for path, source in src.items():
        if name not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and node.value == name:
                return None  # reflective access
            if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
                reads.add((path, node.lineno))
            if isinstance(node, ast.Attribute) and node.attr == name and isinstance(node.ctx, ast.Load):
                if path == tokenizer and node.lineno == transfer.value.lineno:
                    continue
                if name == "NUMERIC_ESCAPES" and path in {
                        "sqlglot/generator.py", "sqlglot/dialects/dialect.py"}:
                    continue  # pin uses only truth, "x"/"0"/"u" membership here
                reads.add((path, node.lineno))
    tree = ast.parse(src[core])
    stores = [n for n in ast.walk(tree) if isinstance(n, ast.Assign) and
              any(isinstance(t, ast.Attribute) and t.attr == field for t in n.targets)]
    if (len(stores) != 1 or not isinstance(stores[0].value, ast.Name)
            or stores[0].value.id != field):
        return None
    for node in ast.walk(tree):
        if name in {"NUMERIC_ESCAPES", "NUMERIC_LITERALS"}:
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                obj = node.func.value
                if (isinstance(obj, ast.Name) and obj.id == field
                        or isinstance(obj, ast.Attribute) and obj.attr == field):
                    reads.add((core, node.lineno))
        elif isinstance(node, ast.Attribute) and node.attr == field and isinstance(node.ctx, ast.Load):
            reads.add((core, node.lineno))
    return tuple(reads) if any(p == core for p, _ in reads) else None


def read_bits(an, name):
    locations = sites(name)
    if locations is None:
        return None
    bits = 0
    for path, line in locations:
        bits |= an.lines_bits(path, line, line)
    # Worker execution may be outside the pytest process's line coverage.
    path = "tests/test_executor.py"
    from .impact import _sources
    for n, line in enumerate(_sources()["src"][path].splitlines(), 1):
        if "with ProcessPoolExecutor(" in line:
            bits |= an.lines_bits(path, n, n)
    return bits


@functools.cache
def site_map():
    return json.loads((Path(os.environ.get("BLAST_APP_DIR", "/app")) / "pin-cache/token-options.json").read_text())


def sites(name):
    try:
        return site_map().get(name)
    except FileNotFoundError:
        return discover_sites(name)


def corpus_edit(path, original, patched):
    if not (path == "sqlglot/tokens.py" or path.startswith("sqlglot/dialects/")):
        return False
    old, new = ast.parse(original), ast.parse(patched)
    for a, b in zip(ast.walk(old), ast.walk(new)):
        if not isinstance(a, ast.ClassDef) or a.name != "Tokenizer" or not isinstance(b, ast.ClassDef):
            continue
        for x, y in zip(a.body, b.body):
            if ast.dump(x) == ast.dump(y):
                continue
            field = option([x], [y])
            reads = sites(field) if field else None
            if not reads or any(p != "sqlglot/tokenizer_core.py" for p, _ in reads):
                continue
            saved = y.value
            y.value = x.value
            equal = ast.dump(old) == ast.dump(new)
            y.value = saved
            if equal:
                return True
    return False


@functools.cache
def key_reads():
    path = Path(__file__).parent / "maps" / "token-reads.json"
    return json.loads(path.read_text())


def keyed_bits(an, bits, owner, old, new):
    before, after = ast.literal_eval(old.value), ast.literal_eval(new.value)
    changed = {k for k in before if repr(before[k]) != repr(after[k])}
    profiles = key_reads()
    for i, test in enumerate(an.idx["tests"]):
        if bits & (1 << i) and test in profiles and not test.startswith("tests/test_executor.py::"):
            observations = profiles[test]
            if not any(cls in (owner, "*") and key in changed for cls, key in observations):
                bits &= ~(1 << i)
    return bits


@functools.cache
def keyword_reads():
    maps = Path(__file__).parent / "maps"
    gz = maps / "keyword-reads.json.gz"
    if gz.is_file():
        with gzip.open(gz, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads((maps / "keyword-reads.json").read_text())


@functools.cache
def token_kinds():
    from .impact import _sources
    module = ast.parse(_sources()["src"]["sqlglot/tokenizer_core.py"])
    cls = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "TokenType")
    return {target.id for stmt in cls.body if isinstance(stmt, ast.Assign)
            for target in stmt.targets if isinstance(target, ast.Name)}


def changed_keywords(old, new, *, extra_kinds=()):
    if len(old) != 1 or len(new) != 1:
        return None
    a, b = old[0], new[0]
    if type(a) != type(b) or not isinstance(a, (ast.Assign, ast.AnnAssign)):
        return None
    targets = a.targets if isinstance(a, ast.Assign) else [a.target]
    if len(targets) != 1 or not isinstance(targets[0], ast.Name) or targets[0].id != "KEYWORDS":
        return None
    x, y = a.value, b.value
    if not isinstance(x, ast.Dict) or not isinstance(y, ast.Dict):
        return None
    restored = copy.copy(b)
    restored.value = x
    if ast.dump(a) != ast.dump(restored):
        return None
    allowed_kinds = token_kinds() | set(extra_kinds) if extra_kinds else token_kinds()

    def entries(node):
        order, values = [], {}
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and type(key.value) is str
                    and isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                    and value.value.id == "TokenType" and value.attr in allowed_kinds):
                if key.value in values:
                    return None, None
                values[key.value] = value.attr
                order.append(key.value)
            else:
                order.append((ast.dump(key) if key is not None else None, ast.dump(value)))
        return order, values

    left, old_values = entries(x)
    right, new_values = entries(y)
    if left is None or right is None:
        return None
    moved = old_values.keys() ^ new_values.keys()
    if moved:
        meta = keyword_reads()
        for k in moved:
            if (not k.isascii() or not k.isidentifier() or len(k) > 64 or k in meta["excluded"]
                    or any(char in k for char in meta["single"])):
                return None
    if [k for k in left if k not in moved] != [k for k in right if k not in moved]:
        return None
    return {k for k in old_values.keys() | new_values.keys() if old_values.get(k) != new_values.get(k)}



def keyword_bits(an, bits, keys):
    profiles = keyword_reads()["profiles"]
    for i, test in enumerate(an.idx["tests"]):
        if bits & (1 << i) and test in profiles and keys.isdisjoint(profiles[test]):
            bits &= ~(1 << i)
    return bits


@functools.cache
def date_reads():
    maps = Path(__file__).parent / "maps"
    gz = maps / "date-reads.json.gz"
    if gz.is_file():
        with gzip.open(gz, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads((maps / "date-reads.json").read_text())


def date_unit_bits(diff, bits, old, new):
    """Account for both the direct mapping and the derived interval-unit sets."""
    if len(old) != 1 or len(new) != 1:
        return bits
    before, after = old[0], new[0]
    if not (type(before) is type(after) and isinstance(before, (ast.Assign, ast.AnnAssign))
            and isinstance(before.value, ast.Dict) and isinstance(after.value, ast.Dict)):
        return bits
    restored = copy.copy(after)
    restored.value = before.value
    if ast.dump(restored) != ast.dump(before):
        return bits
    maps = []
    for node in (before.value, after.value):
        if not all(isinstance(x, ast.Constant) and type(x.value) is str
                   for x in node.keys + node.values):
            return bits
        mapping = ast.literal_eval(node)
        if len(mapping) != len(node.keys):
            return bits
        maps.append(mapping)
    a, b = maps
    # DuckDB removes this inherited key during its module import.
    if "WEEKDAY" in a and "WEEKDAY" not in b:
        return bits
    keys = {k for k in a.keys() | b.keys() if a.get(k) != b.get(k)}
    # A subclass may override another alias to the same unit. Retain both
    # values even when another base-class key still maps to that unit.
    affected = keys | {m[k] for k in keys for m in maps if k in m}
    profiles = date_reads()
    for i, test in enumerate(diff.an.idx["tests"]):
        if test in profiles and affected.isdisjoint(profiles[test]):
            bits &= ~(1 << i)
    return bits
