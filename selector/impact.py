"""Patch impact analysis for the pinned sqlglot checkout (stdlib only).

``select(patch_text, catalog) -> {"required", "plausible", "full", "reasons"}``

Evidence comes from maps baked offline from the clean pin (tools/): per-test
line coverage (collection-time execution expanded to every test of the
collected file), files opened by each test, sqlglot classes each test
instantiated or touched (with their MRO), and the pinned sources themselves.

Decision policy (sound by construction where possible, widen when not):

* The patch is applied to the pinned source and both versions are parsed.
  Statements are aligned with an AST tree diff, so comment/format-only edits
  produce no semantic change at all.
* Changed statement inside a function body -> tests that executed that
  statement on the pin (a test that never reaches a statement cannot observe
  it). Inserted statements -> tests that reached the insertion point
  (previous sibling, else first statement of the block, else the owning
  compound header).
* Class-body change (attribute, dispatch table, new/removed/renamed method,
  bases) -> every test that instantiated or touched that class or any
  subclass, plus tests that ran code of that class.
* Module-level binding change -> tests that reach references of the bound
  names (runtime references by coverage, class-level references by class
  family, module-level references recursively); anything that may fail at
  import (unresolved names, unknown imports, syntax errors) -> every test that
  imported the module.
* Fixtures / data files -> tests that opened them (and listed their dir).
* Source files read at runtime (e.g. typing tests reading sqlglot/typing/*.py)
  -> their readers even for comment-only edits.
* Config files, binary diffs, unapplicable hunks, unknown paths -> full.
* Catalog tests absent from the maps are always required.
"""

from __future__ import annotations

import ast
import bisect
import builtins
import difflib
import dis
import functools
import inspect
import json
import marshal
import operator
import os
import pickle
import re
import zlib
from pathlib import Path
from types import CodeType

MAPS_DIR = Path(os.environ.get("BLAST_MAPS", str(Path(__file__).resolve().parent / "maps")))
_PIN_CACHE = Path(os.environ.get("BLAST_APP_DIR", str(MAPS_DIR.parents[1]))) / "pin-cache"

# How a change that breaks a module imported while collecting tests is handled:
# "full" (default): plain pytest aborts the whole session on a collection
#   error and the official labelling of that case is unpublished -> full suite.
# "file": the labeler continues past collection errors -> only tests of the
#   files that import the module (plus runtime importers) can fail.
COLLECTION_ERRORS = os.environ.get("BLAST_COLLECTION_ERRORS", "full")

_IDX: dict | None = None
_SRC: dict | None = None
_BUILTINS = frozenset(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__",
                                        "__path__", "__package__", "__loader__",
                                        "__builtins__", "__annotations__", "__dict__",
                                        "__qualname__", "__module__", "__class__"}

# files that configure pytest / collection itself
FULL_PATHS = {"pytest.ini", "tox.ini", "conftest.py", "tests/conftest.py", ".pytest.ini",
              "sitecustomize.py", "usercustomize.py"}
CONFIG_PATHS = {"pyproject.toml", "setup.cfg"}


@functools.lru_cache(maxsize=64)
def _parse_pinned(path: str):
    """Parse a pinned source file once per process (read-only AST)."""
    src = _sources()["src"].get(path)
    if src is None:
        return None
    return ast.parse(src)


def _load_map(name):
    cache = _PIN_CACHE / (name + ".marshal")
    if cache.exists():
        return marshal.loads(cache.read_bytes())
    return pickle.loads(zlib.decompress((MAPS_DIR / (name + ".pkl.z")).read_bytes()))


def _cached_marshal(name):
    cache = _PIN_CACHE / (name + ".marshal")
    if cache.exists():
        return marshal.loads(cache.read_bytes())
    return None


def _index() -> dict:
    global _IDX
    if _IDX is None:
        _IDX = _load_map("index")
    return _IDX


def _sources() -> dict:
    global _SRC
    if _SRC is None:
        _SRC = _load_map("src")
    return _SRC


class Unsupported(Exception):
    """Something we cannot analyse soundly -> full suite."""


# --------------------------------------------------------------------------- diff


class Hunk:
    __slots__ = ("old_start", "old_len", "new_start", "new_len", "lines")

    def __init__(self, os_, ol, ns, nl):
        self.old_start, self.old_len, self.new_start, self.new_len = os_, ol, ns, nl
        self.lines: list[tuple[str, str]] = []


class FilePatch:
    __slots__ = ("old_path", "new_path", "status", "hunks", "binary", "payload")

    def __init__(self):
        self.old_path = self.new_path = None
        self.status = "M"
        self.hunks: list[Hunk] = []
        self.binary = False
        self.payload = None

    @property
    def path(self):
        return self.new_path or self.old_path


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_path(p: str) -> str | None:
    """Raw path token from a diff header (prefix kept; see _norm_path)."""
    p = p.rstrip("\n").split("\t")[0].strip()
    if p.startswith('"') and p.endswith('"'):
        p = bytes(p[1:-1], "utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")
    if p == "/dev/null":
        return None
    return p


def _norm_path(p: str | None, known: set) -> str | None:
    """Drop git's a/ b/ (or c/ i/ w/ o/ mnemonic, or --src-prefix) component.
    Prefer the interpretation that names a file of the pinned checkout."""
    if p is None:
        return None
    if p.startswith("./"):
        p = p[2:]
    if p in known:
        return p
    if "/" in p:
        head, rest = p.split("/", 1)
        if rest in known or len(head) == 1:
            return rest
        # a prefix that is not a top-level dir of the checkout
        if not any(k.startswith(head + "/") for k in known) and rest:
            return rest
    return p


def parse_patch(text: str) -> list[FilePatch]:
    lines = text.splitlines()
    if len(text) > 256 * 1024 and any(len(line) > 256 * 1024 for line in lines):
        raise Unsupported("unusually long diff line")
    hunk_count = 0
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            rest = line[len("diff --git "):]
            if rest.startswith('"'):
                m = re.match(r'^("(?:[^"\\]|\\.)*") ("(?:[^"\\]|\\.)*"|\S+)$', rest)
                parts = [m.group(1), m.group(2)] if m else []
            else:
                parts = rest.split(" ")
                if len(parts) != 2:
                    half = len(rest) // 2
                    parts = [rest[:half], rest[half + 1:]] if rest[half:half + 1] == " " else []
            if len(parts) == 2:
                cur.old_path, cur.new_path = _strip_path(parts[0]), _strip_path(parts[1])
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = FilePatch()
                files.append(cur)
            op, np_ = _strip_path(line[4:]), _strip_path(lines[i + 1][4:])
            cur.old_path, cur.new_path = op, np_
            if op is None:
                cur.status = "A"
            elif np_ is None:
                cur.status = "D"
            i += 2
            continue
        if cur is not None:
            if line.startswith(("old mode ", "new mode ")):
                raise Unsupported("file mode change")
            if line.startswith("new file mode"):
                cur.status = "A"
            elif line.startswith("deleted file mode"):
                cur.status = "D"
            elif line.startswith("rename from "):
                cur.status = "R"
                cur.old_path = "./" + line[len("rename from "):].strip()
            elif line.startswith("rename to "):
                cur.status = "R"
                cur.new_path = "./" + line[len("rename to "):].strip()
            elif line.startswith("copy from ") or line.startswith("copy to "):
                cur.status = "C"
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur.binary = True
                if line == "GIT binary patch":
                    end = i + 1
                    while end < n and lines[end] and not lines[end].startswith("diff --git "):
                        end += 1
                    cur.payload = lines[i+1:end]
            elif line.startswith("@@"):
                hunk_count += 1
                if hunk_count > 2048:
                    raise Unsupported("too many diff hunks")
                m = _HUNK_RE.match(line)
                if not m:
                    raise Unsupported(f"bad hunk header {line[:60]!r}")
                h = Hunk(int(m.group(1)), int(m.group(2) or 1), int(m.group(3)),
                         int(m.group(4) or 1))
                cur.hunks.append(h)
                o_left, n_left = h.old_len, h.new_len
                i += 1
                while i < n and (o_left > 0 or n_left > 0):
                    l = lines[i]
                    tag = l[:1]
                    if tag == "\\":
                        i += 1
                        continue
                    if tag == " " or l == "":
                        h.lines.append((" ", l[1:]))
                        o_left -= 1
                        n_left -= 1
                    elif tag == "-":
                        h.lines.append(("-", l[1:]))
                        o_left -= 1
                    elif tag == "+":
                        h.lines.append(("+", l[1:]))
                        n_left -= 1
                    else:
                        raise Unsupported("truncated hunk")
                    i += 1
                if o_left != 0 or n_left != 0:
                    raise Unsupported("hunk length mismatch")
                if i < n and lines[i].startswith(("+", "-")) and not lines[i].startswith("--- "):
                    raise Unsupported("extra hunk data")
                continue
        i += 1
    known = set(_sources()["files"])
    for f in files:
        if f.status == "M" and not f.hunks and not f.binary:
            raise Unsupported("missing diff content")
        f.old_path = _norm_path(f.old_path, known)
        f.new_path = _norm_path(f.new_path, known)
        if f.status == "A":
            f.old_path = None
        elif f.status == "D":
            f.new_path = None
        if f.status == "M" and f.old_path and f.new_path and f.old_path != f.new_path:
            f.status = "R"
    return files


def apply_hunks(old: str, hunks: list[Hunk]) -> tuple[str, list[tuple[Hunk, int]]]:
    """Apply hunks (exact context, small offset search). Returns new text and
    the actual 1-based old start line of each hunk."""
    old_lines = old.split("\n")
    trailing = old.endswith("\n")
    if trailing:
        old_lines = old_lines[:-1]
    out: list[str] = []
    pos = 0  # index into old_lines (0-based) copied so far
    placed = []
    for h in hunks:
        want = [t for tag, t in h.lines if tag in (" ", "-")]
        start = h.old_start - 1 if h.old_len > 0 else h.old_start
        found = None
        for delta in sorted(range(-60, 61), key=abs):
            s = start + delta
            if s < pos or s + len(want) > len(old_lines):
                continue
            if all(old_lines[s + k].rstrip("\r") == want[k].rstrip("\r")
                   for k in range(len(want))):
                found = s
                break
        if found is None:
            raise Unsupported("hunk does not apply to the pinned source")
        out.extend(old_lines[pos:found])
        for tag, t in h.lines:
            if tag in (" ", "+"):
                out.append(t)
        pos = found + len(want)
        placed.append((h, found + 1))
    out.extend(old_lines[pos:])
    return "\n".join(out) + ("\n" if trailing else ""), placed


# --------------------------------------------------------------------------- helpers


def _module_name(path: str) -> str:
    p = path[:-3] if path.endswith(".py") else path
    p = p.replace("/", ".")
    if p.endswith(".__init__"):
        p = p[: -len(".__init__")]
    return p


def _dump(node) -> str:
    return ast.dump(node, include_attributes=False)


def _is_docstring(s) -> bool:
    return (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str))


_COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
             ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Match)
if hasattr(ast, "TryStar"):
    _COMPOUND = _COMPOUND + (ast.TryStar,)
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _start(s) -> int:
    decos = getattr(s, "decorator_list", None)
    if decos:
        return min([d.lineno for d in decos] + [s.lineno])
    return s.lineno


def _header_end(s) -> int:
    body = getattr(s, "body", None)
    if isinstance(s, _COMPOUND) and body and not isinstance(s, ast.Match):
        return max(s.lineno, body[0].lineno - 1) if body[0].lineno > s.lineno else s.lineno
    if isinstance(s, ast.Match):
        return s.subject.end_lineno or s.lineno
    return s.end_lineno or s.lineno


def _header_dump(s) -> str:
    """Dump of a compound statement without its nested blocks."""
    if isinstance(s, (ast.If, ast.While)):
        return _dump(s.test)
    if isinstance(s, (ast.For, ast.AsyncFor)):
        return _dump(s.target) + _dump(s.iter)
    if isinstance(s, (ast.With, ast.AsyncWith)):
        return repr([_dump(x) for x in s.items])
    if isinstance(s, ast.Match):
        return _dump(s.subject)
    return ""


def _def_header(s) -> str:
    parts = [s.name, _dump(s.args), [_dump(d) for d in s.decorator_list],
             _dump(s.returns) if s.returns is not None else None,
             [_dump(t) for t in getattr(s, "type_params", [])]]
    return repr(parts)


def _class_header(s) -> str:
    return repr([s.name, [_dump(b) for b in s.bases], [_dump(k) for k in s.keywords],
                 [_dump(d) for d in s.decorator_list],
                 [_dump(t) for t in getattr(s, "type_params", [])]])


def _bound_names(s) -> set[str]:
    out: set[str] = set()
    if isinstance(s, (ast.Import, ast.ImportFrom)):
        for a in s.names:
            out.add((a.asname or a.name).split(".")[0])
        return out
    if isinstance(s, _DEFS + (ast.ClassDef,)):
        return {s.name}
    targets = []
    if isinstance(s, ast.Assign):
        targets = s.targets
    elif isinstance(s, (ast.AnnAssign, ast.AugAssign)):
        targets = [s.target]
    elif hasattr(ast, "TypeAlias") and isinstance(s, ast.TypeAlias):
        targets = [s.name]
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                out.add(n.id)
    return out


_IDENT_CACHE: dict = {}


# --------------------------------------------------------------------------- analysis


def _code_scopes(code):
    """Index nested code by lexical name, preserving repeated definitions."""
    scopes = {}
    pending = [code]
    while pending:
        item = pending.pop()
        scopes.setdefault(item.co_qualname, []).append(item)
        pending.extend(c for c in item.co_consts if isinstance(c, CodeType))
    return scopes


@functools.lru_cache(maxsize=12)
def _pinned_scopes(path):
    cache = _PIN_CACHE / (path + ".marshal")
    if cache.exists():
        return _code_scopes(marshal.loads(cache.read_bytes()))
    source = _sources()["src"][path]
    return _code_scopes(compile(source.encode(), path, "exec", dont_inherit=True))


def _changed_call_semantics(path, patched_code):
    # An unexecuted yield changes what a call returns. A new assignment can
    # turn an earlier global read into an uninitialized local. Old coverage
    # at the edited line says nothing about either change.
    protocols = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR
    pinned = _pinned_scopes(path)
    closure_roots = set()
    for name, new_group in _code_scopes(patched_code).items():
        old_group = pinned.get(name, ())
        for old, new in zip(old_group, new_group):
            if (old.co_flags ^ new.co_flags) & protocols:
                return True
            if old.co_freevars != new.co_freevars or old.co_cellvars != new.co_cellvars:
                root = name.split(".<locals>.", 1)[0]
                outer = pinned.get(root, ())
                if (len(outer) != 1 or not outer[0].co_flags & inspect.CO_OPTIMIZED
                        or outer[0].co_flags & protocols):
                    return True
                closure_roots.add(root)
            candidates = (set(old.co_varnames) ^ set(new.co_varnames)) & (set(old.co_names) | set(new.co_names))
            if candidates:
                # LOAD_ATTR and LOAD_METHOD use co_names too. Neither reads a
                # variable binding, even when its spelling matches a new local.
                for code in (old, new):
                    if any(i.opname.endswith(("_GLOBAL", "_NAME", "_GLOBALS"))
                           and i.argval in candidates for i in dis.get_instructions(code)):
                        return True
    return closure_roots


@functools.lru_cache(maxsize=1)
def _identity_sites():
    return frozenset(json.loads((MAPS_DIR / "identity.json").read_text()))


def _same_observed_comparison(path, old, new):
    """Allow exactly one operator substitution, with all other AST fields equal."""
    module = _module_name(path)
    old_nodes = list(ast.walk(old))
    new_nodes = list(ast.walk(new))
    if len(old_nodes) != len(new_nodes):
        return False
    for a, b in zip(old_nodes, new_nodes):
        if not isinstance(a, ast.Compare) or not isinstance(b, ast.Compare):
            continue
        if len(a.ops) != 1 or len(b.ops) != 1:
            continue
        if (type(a.ops[0]), type(b.ops[0])) not in ((ast.Is, ast.Eq), (ast.IsNot, ast.NotEq)):
            continue
        if f"{module}-{a.lineno}-{a.col_offset}" not in _identity_sites():
            continue
        saved = b.ops
        try:
            b.ops = a.ops
            return _dump(old) == _dump(new)
        finally:
            b.ops = saved
    return False


@functools.lru_cache(maxsize=1)
def _order_sites():
    path = MAPS_DIR / "order.json"
    return frozenset(json.loads(path.read_text())) if path.is_file() else frozenset()


_ORDER_TWINS = frozenset({(ast.Lt, ast.LtE), (ast.LtE, ast.Lt), (ast.Gt, ast.GtE), (ast.GtE, ast.Gt)})


def _same_observed_order(path, old, new):
    """Allow exactly one strict/non-strict ordering substitution, with all other AST
    fields equal, at a site whose pinned operands were never equal (both forms agreed)."""
    module = _module_name(path)
    old_nodes = list(ast.walk(old))
    new_nodes = list(ast.walk(new))
    if len(old_nodes) != len(new_nodes):
        return False
    for a, b in zip(old_nodes, new_nodes):
        if not isinstance(a, ast.Compare) or not isinstance(b, ast.Compare):
            continue
        if len(a.ops) != 1 or len(b.ops) != 1:
            continue
        if (type(a.ops[0]), type(b.ops[0])) not in _ORDER_TWINS:
            continue
        if f"{module}-{a.lineno}-{a.col_offset}" not in _order_sites():
            continue
        saved = b.ops
        try:
            b.ops = a.ops
            return _dump(old) == _dump(new)
        finally:
            b.ops = saved
    return False


def _load_equiv(name, kinds):
    import gzip

    path = MAPS_DIR / name
    data = json.loads(gzip.decompress(path.read_bytes())) if path.is_file() else {}
    return {kind: data.get(kind, {}) for kind in kinds}


@functools.lru_cache(maxsize=1)
def _equiv_tests():
    return _load_equiv("equiv-tests.json.gz", ("identity", "order"))


@functools.lru_cache(maxsize=1)
def _equiv_bool_tests():
    """Kept apart so identity/order edits never decode the much larger bool/ret lists."""
    return _load_equiv("equiv-bool.json.gz", ("bool", "ret"))


@functools.lru_cache(maxsize=None)
def _equiv_sites(kind):
    path = MAPS_DIR / f"{kind}.json"
    return frozenset(json.loads(path.read_text())) if path.is_file() else frozenset()


_BOOL_TWINS = frozenset({(ast.And, ast.Or), (ast.Or, ast.And)})


def _bool_swap_site(path, old, new):
    """Pinned site of the only and/or swap of a two-operand BoolOp when the trees are otherwise identical."""
    old_nodes = list(ast.walk(old))
    new_nodes = list(ast.walk(new))
    if len(old_nodes) != len(new_nodes):
        return None
    for a, b in zip(old_nodes, new_nodes):
        if not isinstance(a, ast.BoolOp) or not isinstance(b, ast.BoolOp):
            continue
        if len(a.values) != 2 or len(b.values) != 2 or (type(a.op), type(b.op)) not in _BOOL_TWINS:
            continue
        saved = b.op
        try:
            b.op = a.op
            same = _dump(old) == _dump(new)
        finally:
            b.op = saved
        return f"{_module_name(path)}-{a.lineno}-{a.col_offset}" if same else None
    return None


def _return_none_site(path, old, new):
    """Pinned site of the only ``return X`` -> ``return None and X`` edit when the trees are otherwise identical."""
    olds = [n for n in ast.walk(old) if isinstance(n, ast.Return)]
    news = [n for n in ast.walk(new) if isinstance(n, ast.Return)]
    if len(olds) != len(news):
        return None
    for a, b in zip(olds, news):
        if a.value is None or b.value is None or _dump(a.value) == _dump(b.value):
            continue
        v = b.value
        if not (isinstance(v, ast.BoolOp) and isinstance(v.op, ast.And) and len(v.values) == 2
                and isinstance(v.values[0], ast.Constant) and v.values[0].value is None
                and _dump(v.values[1]) == _dump(a.value)):
            return None
        try:
            b.value = a.value
            same = _dump(old) == _dump(new)
        finally:
            b.value = v
        return f"{_module_name(path)}-{a.lineno}-{a.col_offset}" if same else None
    return None


_IDENTITY_TWINS = frozenset({(ast.Is, ast.Eq), (ast.IsNot, ast.NotEq)})


def _substituted_site(path, old, new, twins):
    """Pinned site of the only operator substitution in ``twins`` when the trees are otherwise identical."""
    old_nodes = list(ast.walk(old))
    new_nodes = list(ast.walk(new))
    if len(old_nodes) != len(new_nodes):
        return None
    for a, b in zip(old_nodes, new_nodes):
        if not isinstance(a, ast.Compare) or not isinstance(b, ast.Compare):
            continue
        if len(a.ops) != 1 or len(b.ops) != 1 or (type(a.ops[0]), type(b.ops[0])) not in twins:
            continue
        saved = b.ops
        try:
            b.ops = a.ops
            same = _dump(old) == _dump(new)
        finally:
            b.ops = saved
        return f"{_module_name(path)}-{a.lineno}-{a.col_offset}" if same else None
    return None


def _observed_substitution_tests(path, old, new, identity_edit, order_edit, bool_edit=False, ret_edit=False):
    """Pinned tests whose call phase saw the substitution change a result (or could not tell).

    Every other test either never evaluated the site or always got the same result from
    both forms, so it executes identically. Sites with any such observation outside a
    test's call phase or inside a module body are absent from the maps. ``bool`` and
    ``ret`` sites certified for the whole suite select nothing."""
    finders = ((identity_edit, "identity", lambda: _substituted_site(path, old, new, _IDENTITY_TWINS)),
               (order_edit, "order", lambda: _substituted_site(path, old, new, _ORDER_TWINS)),
               (bool_edit, "bool", lambda: _bool_swap_site(path, old, new)),
               (ret_edit, "ret", lambda: _return_none_site(path, old, new)))
    for enabled, kind, find in finders:
        if not enabled:
            continue
        site = find()
        if site is None:
            continue
        if kind in ("bool", "ret") and site in _equiv_sites(kind):
            return kind, site, []
        tests = (_equiv_bool_tests() if kind in ("bool", "ret") else _equiv_tests())[kind].get(site)
        if tests is not None:
            return kind, site, tests
    return None


@functools.lru_cache(maxsize=1)
def _branch_map():
    """Return ``(sites_dict, tail_both_dict)`` where ``sites_dict[site] = (b0, b1)``
    bitmasks of tests where ``site`` was always False / always True."""
    cached = _cached_marshal("branches")
    if cached is not None:
        return cached
    import gzip

    gz = MAPS_DIR / "branches.json.gz"
    if gz.is_file():
        data = json.loads(gzip.decompress(gz.read_bytes()))
        sites = {}
        for s, (i0, i1) in data["sites"].items():
            b0 = 0
            for i in i0:
                b0 |= 1 << i
            b1 = 0
            for i in i1:
                b1 |= 1 << i
            sites[s] = (b0, b1)
        tail_both = {}
        for s, idxs in data.get("tail_both", {}).items():
            bm = 0
            for i in idxs:
                bm |= 1 << i
            tail_both[s] = bm
        return sites, tail_both
    profiles = json.loads((MAPS_DIR / "branches.json").read_text())
    tests = _index()["tests"]
    sites = {}
    for test, smap in profiles.items():
        bit = 1 << tests.index(test)
        for s, val in smap.items():
            b0, b1 = sites.get(s, (0, 0))
            if val == 0:
                b0 |= bit
            else:
                b1 |= bit
            sites[s] = (b0, b1)
    return sites, {}


@functools.lru_cache(maxsize=1)
def _operand_map():
    """Return ``{site: [(bitmask, concrete_values), ...]}``."""
    cached = _cached_marshal("operands")
    if cached is not None:
        return cached
    import gzip

    gz = MAPS_DIR / "operands.json.gz"
    if gz.is_file():
        data = json.loads(gzip.decompress(gz.read_bytes()))
        out = {}
        for s, groups in data["sites"].items():
            parsed = []
            for tidxs, vals in groups:
                bm = 0
                for i in tidxs:
                    bm |= 1 << i
                parsed.append((bm, vals))
            out[s] = parsed
        return out
    profiles = json.loads((MAPS_DIR / "operands.json").read_text())
    tests = _index()["tests"]
    out = {}
    for test, smap in profiles.items():
        bit = 1 << tests.index(test)
        for s, vals in smap.items():
            out.setdefault(s, []).append((bit, vals))
    return out


_COMPARISONS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
                ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def _equal_comparison_bits(module, old, new):
    for node in (old, new):
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1
                and type(node.ops[0]) in _COMPARISONS and isinstance(node.comparators[0], ast.Constant)
                and type(node.comparators[0].value) in (str, int, bool, type(None))):
            return 0
    if _dump(old.left) != _dump(new.left):
        return 0
    site = f"{module}:{old.lineno}:{old.col_offset}"
    before, after = _COMPARISONS[type(old.ops[0])], _COMPARISONS[type(new.ops[0])]
    result = 0
    for bits, concrete in _operand_map().get(site, ()):
        try:
            if concrete and all(before(x, old.comparators[0].value) == after(x, new.comparators[0].value)
                                for x in concrete):
                result |= bits
        except TypeError:
            continue
    return result


@functools.cache
def _fresh_tables():
    return frozenset(json.loads((MAPS_DIR / "fresh-tables.json").read_text()))


def _same_fresh_table(path, old, new):
    for a, b in zip(ast.walk(old), ast.walk(new)):
        if not isinstance(a, ast.Call) or not isinstance(b, ast.Call):
            continue
        if f"{_module_name(path)}:{a.lineno}:{a.col_offset}" not in _fresh_tables():
            continue
        if len(a.keywords) != len(b.keywords):
            continue
        for x, y in zip(a.keywords, b.keywords):
            if (x.arg != "copy" or y.arg != "copy" or not isinstance(x.value, ast.Constant)
                    or not isinstance(y.value, ast.Constant) or type(x.value.value) is not bool
                    or type(y.value.value) is not bool):
                continue
            saved = y.value
            try:
                y.value = x.value
                return _dump(old) == _dump(new)
            finally:
                y.value = saved
    return False


def _same_version_boundary(path, old, new):
    """Dialect.__init__ normalizes every version to exactly three integers."""
    if not path.startswith(("sqlglot/parsers/", "sqlglot/generators/")):
        return False
    for a, b in zip(ast.walk(old), ast.walk(new)):
        if not isinstance(a, ast.Compare) or not isinstance(b, ast.Compare):
            continue
        if len(a.ops) != 1 or len(b.ops) != 1:
            continue
        if {type(a.ops[0]), type(b.ops[0])} not in ({ast.Lt, ast.LtE}, {ast.Gt, ast.GtE}):
            continue
        left, right = a.left, a.comparators[0]
        if not (isinstance(left, ast.Attribute) and left.attr == "version"
                and isinstance(left.value, ast.Attribute) and left.value.attr == "dialect"
                and isinstance(left.value.value, ast.Name) and left.value.value.id == "self"
                and isinstance(right, ast.Tuple) and len(right.elts) != 3
                and all(isinstance(e, ast.Constant) and type(e.value) is int for e in right.elts)):
            continue
        saved = b.ops
        try:
            b.ops = a.ops
            return _dump(old) == _dump(new)
        finally:
            b.ops = saved
    return False


def _pure_type_guard(node):
    """A pinned SQLGlot enum type check on an expression's stored payload."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        node = node.operand
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance" and len(node.args) == 2 and not node.keywords):
        return False
    value, typ = node.args
    return (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
            and value.attr == "this" and isinstance(typ, ast.Attribute)
            and isinstance(typ.value, ast.Name) and typ.value.id == "exp" and typ.attr == "DType")


def _unchanged_branch_bits(module, old, new):
    if isinstance(old, ast.Compare) and isinstance(new, ast.Compare):
        return _equal_comparison_bits(module, old, new)
    sites, tail_both = _branch_map()
    site = f"{module}:{old.lineno}:{old.col_offset}"
    if isinstance(new, ast.BoolOp) and len(new.values) == 2:
        # If the unchanged prefix always short-circuits, the added operand
        # cannot run. This holds regardless of its calls or side effects.
        if _dump(new.values[0]) == _dump(old):
            stop = int(isinstance(new.op, ast.Or))
            b0, b1 = sites.get(site, (0, 0))
            return b1 if stop else b0
        if (isinstance(old, ast.BoolOp) and type(old.op) is type(new.op)
                and len(old.values) == 2 and _dump(old.values[0]) == _dump(new.values[0])):
            stop = int(isinstance(new.op, ast.Or))
            b0, b1 = sites.get(site, (0, 0))
            tkey = site + ":tail"
            t0, t1 = sites.get(tkey, (0, 0))
            tail_seen = t0 | t1 | tail_both.get(tkey, 0)
            return (b1 if stop else b0) & ~tail_seen
    if not isinstance(old, ast.BoolOp) or len(old.values) != 2:
        return 0
    if _dump(old.values[0]) != _dump(new) or not _pure_type_guard(old.values[1]):
        return 0
    tkey = f"{module}:{old.lineno}:{old.col_offset}:tail"
    neutral = int(isinstance(old.op, ast.And))
    t0, t1 = sites.get(tkey, (0, 0))
    return t1 if neutral else t0


def _local_raising_fixtures(body):
    if not body or not isinstance(body[0], ast.Import) or _dump(body[0]) != _dump(ast.parse("import pytest").body[0]):
        return False
    decorator = _dump(ast.parse("pytest.fixture(autouse=True)", mode="eval").body)
    for fn in body[1:]:
        if not (isinstance(fn, ast.FunctionDef) and fn.name.startswith("_") and not fn.name.startswith("__")
                and len(fn.decorator_list) == 1 and _dump(fn.decorator_list[0]) == decorator
                and not fn.args.defaults and not fn.args.kw_defaults and not fn.returns
                and not getattr(fn, "type_params", []) and not any(ast.iter_child_nodes(fn.args))
                and len(fn.body) == 1 and isinstance(fn.body[0], ast.Raise)):
            return False
        error = fn.body[0].exc
        if not (isinstance(error, ast.Call) and isinstance(error.func, ast.Name)
                and error.func.id in {"RuntimeError", "ValueError", "AssertionError"}
                and all(isinstance(arg, ast.Constant) for arg in error.args)
                and not error.keywords and fn.body[0].cause is None):
            return False
    return True


def _isolated_test_module(source):
    """Allow only unittest definitions with no import-time user expressions."""
    for stmt in ast.parse(source).body:
        if _is_docstring(stmt):
            continue
        if isinstance(stmt, ast.Import) and all(a.name == "unittest" and not a.asname for a in stmt.names):
            continue
        if not (isinstance(stmt, ast.ClassDef) and not stmt.decorator_list and not stmt.keywords
                and len(stmt.bases) == 1 and ast.unparse(stmt.bases[0]) == "unittest.TestCase"):
            return False
        for fn in stmt.body:
            if _is_docstring(fn):
                continue
            if not (isinstance(fn, ast.FunctionDef) and fn.name.startswith("test")
                    and not fn.decorator_list and not fn.returns and not fn.type_params
                    and ast.unparse(fn.args) == "self"):
                return False
            for statement in fn.body:
                if _is_docstring(statement) or isinstance(statement, ast.Pass):
                    continue
                call = statement.value if isinstance(statement, ast.Expr) else None
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name) and call.func.value.id == "self"
                        and call.func.attr in {"assertEqual", "assertTrue", "assertFalse", "assertIsNone"}
                        and not call.keywords and all(isinstance(arg, ast.Constant) for arg in call.args)):
                    return False
    return True


@functools.cache
def _absent_plugin(name):
    import importlib.metadata
    import importlib.util
    from _pytest.config import builtin_plugins
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        return False
    aliases = {name, "pytest_" + name}
    installed = {p.name for p in importlib.metadata.entry_points(group="pytest11")}
    if aliases & (installed | set(builtin_plugins)):
        return False
    if any(importlib.util.find_spec(alias) for alias in aliases):
        return False
    return not any(any(alias in source for alias in aliases) for source in _sources()["src"].values())


def _without_pytest_discovery(doc):
    import copy
    config = copy.deepcopy(doc.get("tool", {}).get("pytest", {}))
    options = config.get("ini_options", {})
    for key in ("python_files", "python_functions", "python_classes"):
        options.pop(key, None)
    addopts = options.get("addopts")
    if isinstance(addopts, str):
        disabled = re.fullmatch(r"\s*-p\s+no:([a-z][a-z0-9_]*)\s*", addopts)
        if disabled and _absent_plugin(disabled[1]):
            options.pop("addopts")
    if not options:
        config.pop("ini_options", None)
    return config, doc.get("build-system")


class Analysis:
    def __init__(self):
        self.check_imports = False
        self.single_file = False
        self.fixture_only = self.precise_fixture = self.safe_fixture_proj = False
        self.idx = _index()
        self.req = 0
        self.plaus = 0
        self.full = False
        self.reasons: list[str] = []
        self.excluded = 0  # tests a cruder sound rule would add (validation only)
        self._file_bits: dict[str, int] = {}

    # -- map access
    def sets(self, sid: int) -> int:
        return self.idx["sets"][sid]

    def lines_bits(self, path: str, lo: int, hi: int) -> int:
        cov = self.idx["cov"].get(path)
        if not cov:
            return 0
        sets = self.idx["sets"]
        b = 0
        for ln in range(lo, hi + 1):
            s = cov.get(ln)
            if s is not None:
                b |= sets[s]
        return b

    def file_bits(self, path: str) -> int:
        """Every test that executed any line of the file (= imported it)."""
        if path not in self._file_bits:
            cov = self.idx["cov"].get(path) or {}
            sets = self.idx["sets"]
            b = 0
            for s in set(cov.values()):
                b |= sets[s]
            self._file_bits[path] = b
        return self._file_bits[path]

    def collected(self) -> set:
        c = self.idx.get("_collected")
        if c is None:
            c = set()
            for mods in self.idx["collect_mods"].values():
                c.update(mods)
            self.idx["_collected"] = c
        return c

    def import_break(self, path: str, why: str) -> int:
        """``path`` may fail to import.  If any test file imports it while
        being collected, pytest reports a collection error and interrupts the
        whole session -> full suite.  Otherwise every test that imported it at
        runtime is hit."""
        if path in self.collected():
            if COLLECTION_ERRORS != "file":
                if os.environ.get("BLAST_HEAVY") != "1":
                    raise Unsupported(f"{why}: {path} is imported during collection")
                self.check_imports = True
            self.reasons.append(f"collection error expected in importers of {path} ({why})")
        return self.file_bits(path)

    def test_module_gone(self, path: str) -> int:
        """A test module disappears: its own tests vanish (cheap to keep), and
        every module importing it fails to import."""
        me = _module_name(path)
        b = self.tests_in_file(path)
        files = {_module_name(f): f for f in _sources()["src"]}
        for mod, deps in self.idx.get("imports", {}).items():
            if mod != me and me in deps and mod in files:
                b |= self.import_break(files[mod], f"imports removed test module {me}")
        return b

    def family(self, module: str, qualname: str, path: str | None = None,
               node=None) -> int:
        key = (module, qualname, path)
        cache = self.__dict__.setdefault("_fam_cache", {})
        if key in cache:
            return cache[key]
        b = 0
        sid = self.idx["families"].get(f"{module}.{qualname}")
        if sid is not None:
            b |= self.sets(sid)
        if path:
            # code of the class that ran (classmethods/staticmethods without instances)
            sc = self.idx.get("scopes", {}).get(path)
            if sc:
                pre = qualname + "."
                for it in sc[1]:
                    if it[0] == "def" and it[3].startswith(pre):
                        b |= self.lines_bits(path, it[4], it[2])
        cache[key] = b
        return b

    def tests_in_file(self, test_file: str) -> int:
        tests = self.idx["tests"]
        b = 0
        for i, t in enumerate(tests):
            if t.split("::")[0] == test_file:
                b |= 1 << i
        return b

    def add(self, bits: int, why: str):
        self.req |= bits
        if why:
            self.reasons.append(f"{why} -> {bin(bits).count('1')} tests")

    # -- per file
    def analyze(self, fp: FilePatch):
        path = fp.path
        oldp, newp = fp.old_path, fp.new_path
        if fp.status == "A" and not fp.binary and newp and newp.endswith(".py"):
            self.data_file(fp)  # existing tests may enumerate the directory
            source, _ = apply_hunks("", fp.hunks)
            filename = newp.rsplit("/", 1)[-1]
            if filename == "conftest.py":
                # Even comments can specify a broken Python source encoding.
                compile(source.encode(), newp, "exec", dont_inherit=True)
                body = ast.parse(source).body
                if (self.single_file and newp.startswith("tests/") and _local_raising_fixtures(body)
                        and newp not in _sources()["files"]):
                    directory = newp.rsplit("/", 1)[0] + "/"
                    bits = sum(1 << i for i, test in enumerate(self.idx["tests"])
                               if test.startswith(directory))
                    self.add(bits, f"autouse fixture applies below {directory}")
                    return
                if not body:
                    return
            if filename == "__init__.py" or "/" not in newp:
                raise Unsupported(f"new package or top-level module {newp} may change imports")
            if (newp.startswith("tests/") and
                    (filename.startswith("test_") or filename.endswith("_test.py"))):
                # Pytest imports new test modules even without catalog entries.
                # Top-level code and decorators can affect the collection session.
                if os.environ.get("BLAST_HEAVY") == "1" and _isolated_test_module(source):
                    self.check_imports = True
                    return
                raise Unsupported(f"new pytest module {newp}")
        if fp.binary:
            self.data_file(fp)
            if any(p and (p.endswith(".py") or p.startswith("sqlglot/")) for p in (oldp, newp)):
                raise Unsupported(f"binary change to {path}")
            return
        for p in (oldp, newp):
            if p and (p in FULL_PATHS or p.rsplit("/", 1)[-1] == "conftest.py"):
                raise Unsupported(f"config/collection file {p}")
            if p in CONFIG_PATHS:
                self.config_file(fp)
                return
        is_py = lambda p: p is not None and p.endswith(".py") and (
            p.startswith("sqlglot/") or p.startswith("tests/")) and not p.startswith(
            "tests/fixtures/")
        if is_py(oldp) or is_py(newp):
            self.python_file(fp)
        else:
            self.data_file(fp)

    def config_file(self, fp: FilePatch):
        """pyproject.toml / setup.cfg: only pytest's own sections can change
        how the suite runs; anything else is packaging/lint metadata."""
        if fp.status != "M" or fp.old_path != fp.new_path:
            raise Unsupported(f"{fp.path} added/removed/renamed")
        old = _sources().get("config", {}).get(fp.old_path)
        if old is None:
            raise Unsupported(f"no pinned copy of {fp.old_path}")
        new, _ = apply_hunks(old, fp.hunks)
        if fp.old_path == "pyproject.toml":
            import tomllib

            try:
                o, n = tomllib.loads(old), tomllib.loads(new)
            except Exception:
                raise Unsupported("pyproject.toml no longer parses")
            keep = lambda d: {"pytest": d.get("tool", {}).get("pytest"),
                              "build": d.get("build-system")}
            same = keep(o) == keep(n)
            if not same and os.environ.get("BLAST_HEAVY") == "1":
                if self.single_file and _without_pytest_discovery(o) == _without_pytest_discovery(n):
                    self.check_imports = True
                    same = True
        else:
            import configparser

            def sect(text):
                cp = configparser.RawConfigParser()
                cp.read_string(text)
                return {k: dict(cp.items(k)) for k in cp.sections()
                        if k in ("tool:pytest", "pytest")}

            try:
                same = sect(old) == sect(new)
            except Exception:
                raise Unsupported("setup.cfg no longer parses")
        if not same:
            raise Unsupported(f"pytest configuration in {fp.old_path} changed")
        self.reasons.append(f"{fp.old_path}: no pytest-relevant change")
        self.data_file(fp)

    def data_file(self, fp: FilePatch):
        opens, dirs = self.idx["opens"], self.idx["listdirs"]
        b = 0
        for p in {fp.old_path, fp.new_path} - {None}:
            sid = opens.get(p)
            if sid is not None:
                b |= self.sets(sid)
            if fp.status in ("A", "D", "R", "C"):
                d = p
                while "/" in d:
                    d = d.rsplit("/", 1)[0]
                    sid = dirs.get(d)
                    if sid is not None:
                        b |= self.sets(sid)
        if b and (self.fixture_only or self.safe_fixture_proj):
            from .fixture_fields import project
            observed = project(fp, b, self.idx["tests"], apply_hunks)
            if self.fixture_only:
                self.precise_fixture |= observed != b
            b = observed
        self.add(b, f"data {fp.path} ({fp.status}) readers")
        p = fp.path or ""
        if p.startswith("sqlglot/") and not b and fp.status != "M":
            # a non-python file appearing/disappearing inside the package
            self.reasons.append(f"note: package data {p} not read by any test")

    def python_file(self, fp: FilePatch):
        srcs = _sources()["src"]
        oldp, newp = fp.old_path, fp.new_path
        # runtime source readers (inspect / open of the .py file)
        for p in {oldp, newp} - {None}:
            sid = self.idx["opens"].get(p)
            if sid is not None:
                self.add(self.sets(sid), f"source of {p} is read at runtime")
        if fp.status == "A" or oldp is None:
            if newp and newp.startswith("sqlglot/"):
                # a brand-new module: nothing at the pin imports it
                self.reasons.append(f"new module {newp}: no pinned importer")
            else:
                self.reasons.append(f"new file {newp}: its tests are not in the catalog")
            if fp.status == "A" and newp in srcs:
                raise Unsupported(f"'new' file {newp} already exists at the pin")
            return
        if oldp not in srcs:
            raise Unsupported(f"unknown python path {oldp}")
        old_src = srcs[oldp]
        if fp.status in ("D", "R") or (newp is None):
            # old module vanishes: everything that imported/executed it
            if oldp.startswith("tests/"):
                self.add(self.test_module_gone(oldp), f"test module {oldp} removed/renamed")
            else:
                self.add(self.import_break(oldp, "module removed/renamed"),
                         f"{oldp} removed/renamed: all its importers")
            self.add(self._name_users_of_module(oldp), "")
            return
        if not fp.hunks:
            return
        pre = self.__dict__.get("_preparsed", {}).get(oldp)
        if pre is not None:
            new_src, placed, patched_code, _ = pre
        else:
            new_src, placed = apply_hunks(old_src, fp.hunks)
            # ast.parse accepts errors such as a break outside a loop. Compile the
            # complete module first so the fast path cannot hide collection errors.
            try:
                patched_code = compile(new_src.encode(), oldp, "exec", dont_inherit=True)
            except (SyntaxError, ValueError) as e:
                self.add(self.import_break(oldp, f"compile error: {type(e).__name__}"),
                         f"{oldp} no longer compiles")
                return
        self.__dict__.setdefault("_new_srcs", {})[oldp] = new_src
        semantics = _changed_call_semantics(oldp, patched_code)
        if isinstance(semantics, set) and semantics:
            scopes = self.idx["scopes"].get(oldp, [None, []])[1]
            functions = {name: (body, end) for kind, _, end, name, body in scopes if kind == "def"}
            if semantics <= functions.keys():
                for name in semantics:
                    self.add(self.lines_bits(oldp, *functions[name]), f"closure bindings in {name}")
            else:
                semantics = True
        if semantics is True:
            self.add(self.import_break(oldp, "function protocol or variable binding changed"),
                     f"{oldp} changed function call semantics")
            return
        identity_edit = False
        if self.single_file and len(placed) == 1:
            changed = [(tag, text) for tag, text in placed[0][0].lines if tag in ("+", "-")]
            identity_edit = (len(changed) == 2 and " is " in changed[0][1]
                             and ("==" in changed[1][1] or "!=" in changed[1][1]))
            self.__dict__["_order_edit"] = (
                len(changed) == 2 and changed[0][0] == "-" and changed[1][0] == "+"
                and any(c in changed[0][1] for c in "<>") and any(c in changed[1][1] for c in "<>"))
            one_line = len(changed) == 2 and changed[0][0] == "-" and changed[1][0] == "+"
            self.__dict__["_bool_edit"] = one_line and bool(
                re.search(r"\b(and|or)\b", changed[0][1]) and re.search(r"\b(and|or)\b", changed[1][1]))
            self.__dict__["_ret_edit"] = one_line and "return" in changed[0][1] and "None and" in changed[1][1]
        if self.single_file and oldp.startswith("sqlglot/typing/"):
            from .metadata_keys import removed_keys, observations
            keys = removed_keys(oldp, new_src)
            if keys:
                # Always retain independent readers (including source inspection).
                prior = self.req
                self.python_body(fp, oldp, old_src, new_src, placed, identity_edit)
                for test, seen in observations().items():
                    if keys.isdisjoint(seen) and test in self.idx["tests"]:
                        self.req &= ~(1 << self.idx["tests"].index(test))
                self.req |= prior
                return
        self.python_body(fp, oldp, old_src, new_src, placed, identity_edit)

    def python_body(self, fp, oldp, old_src, new_src, placed, identity_edit):
        if self.fast_function_path(oldp, old_src, new_src, placed, identity_edit):
            return
        try:
            old_tree = _parse_pinned(oldp)
            if old_tree is None:
                old_tree = ast.parse(old_src)
        except SyntaxError:
            raise Unsupported(f"pinned {oldp} does not parse")
        pre = self.__dict__.get("_preparsed", {}).get(oldp)
        if pre is not None and pre[3] is not None:
            new_tree = pre[3]
        else:
            try:
                new_tree = ast.parse(new_src)
            except (SyntaxError, ValueError) as e:
                self.add(self.import_break(oldp, f"no longer compiles ({type(e).__name__})"),
                         f"{oldp} no longer compiles")
                return
        if identity_edit and _same_observed_comparison(oldp, old_tree, new_tree):
            self.reasons.append(f"{oldp}: identity and equality agreed on pinned operands")
            return
        if self.__dict__.get("_order_edit") and _same_observed_order(oldp, old_tree, new_tree):
            self.reasons.append(f"{oldp}: strict and non-strict ordering agreed on pinned operands")
            return
        if self.observed_substitution(oldp, old_tree, new_tree, identity_edit):
            return
        if self.single_file and len(placed) == 1:
            from .value_reads import inert
            if inert(oldp, old_tree, new_tree, self.idx["mro"]):
                self.reasons.append(f"{oldp}: unchanged validation and dispatch on pinned inputs")
                return
        PyDiff(self, oldp, old_tree, new_tree, old_src, new_src).run()

    def observed_substitution(self, path, old, new, identity_edit) -> bool:
        hit = _observed_substitution_tests(path, old, new, identity_edit, self.__dict__.get("_order_edit"),
                                           self.__dict__.get("_bool_edit"), self.__dict__.get("_ret_edit"))
        if hit is None:
            return False
        kind, site, tests = hit
        positions = {t: i for i, t in enumerate(self.idx["tests"])}
        if any(t not in positions for t in tests):
            return False
        bits = 0
        for t in tests:
            bits |= 1 << positions[t]
        self.add(bits, f"{path}: {kind} substitution at {site} diverged in {len(tests)} pinned tests")
        return True

    def fast_function_path(self, path, old_src, new_src, placed, identity_edit=False) -> bool:
        """All hunks strictly inside one function body: parse just that
        function (old and new) instead of the whole module."""
        sc = self.idx.get("scopes", {}).get(path)
        if not sc:
            return False
        lo, hi = None, None
        delta = 0
        for h, start in placed:
            o = start
            first = last = None
            prev_ctx = None
            for tag, _t in h.lines:
                if tag == " ":
                    prev_ctx = o
                    o += 1
                    continue
                if tag == "-":
                    first = o if first is None else first
                    last = o
                    o += 1
                else:  # '+': between prev old line and the next one
                    a = prev_ctx if prev_ctx is not None else o - 1
                    first = a if first is None else min(first, a)
                    last = max(last or 0, o)
            delta += h.new_len - h.old_len
            if first is None:
                continue
            lo = first if lo is None else min(lo, first)
            hi = last if hi is None else max(hi, last)
        if lo is None:
            return False
        best = None
        for it in sc[1]:
            if it[0] == "def" and it[4] <= lo and hi <= it[2]:
                if best is None or it[1] >= best[1]:
                    best = it
        if best is None:
            return False
        kind, d_start, d_end, qual, body_start = best
        old_lines = old_src.split("\n")
        new_lines = new_src.split("\n")
        old_seg = old_lines[d_start - 1: d_end]
        new_seg = new_lines[d_start - 1: d_end + delta]

        def parse(seg):
            text = "\n".join(seg)
            indent = len(seg[0]) - len(seg[0].lstrip())
            if indent:
                tree = ast.parse("if 1:\n" + text)
                node = tree.body[0].body[0]
                ast.increment_lineno(tree, d_start - 2)
            else:
                tree = ast.parse(text)
                node = tree.body[0]
                ast.increment_lineno(tree, d_start - 1)
            return node

        try:
            o = parse(old_seg)
            n = parse(new_seg)
        except SyntaxError:
            return False
        if (not isinstance(o, _DEFS) or type(o) is not type(n) or _def_header(o) != _def_header(n)
                or o.lineno != _start(o) and False):
            return False
        if o.end_lineno != d_end or (n.end_lineno or 0) != d_end + delta:
            return False  # the segment is not exactly the function
        if (self.single_file and len(placed) == 1 and any("copy=" in line for line in old_seg)
                and _same_fresh_table(path, o, n)):
            self.reasons.append(f"{path}: copying a fresh table with pinned scalar inputs")
            return True
        if (self.single_file and len(placed) == 1 and ".version" in "\n".join(old_seg)
                and _same_version_boundary(path, o, n)):
            self.reasons.append(f"{path}: strict boundary excludes equality for the pinned version tuple")
            return True
        if identity_edit and _same_observed_comparison(path, o, n):
            self.reasons.append(f"{path}: identity and equality agreed on pinned operands")
            return True
        if self.__dict__.get("_order_edit") and _same_observed_order(path, o, n):
            self.reasons.append(f"{path}: strict and non-strict ordering agreed on pinned operands")
            return True
        if self.observed_substitution(path, o, n, identity_edit):
            return True
        excluded = ()
        if (self.single_file and path == "sqlglot/generator.py" and qual.startswith("Generator.")
                and "_replace_line_breaks(" in "\n".join(old_seg)):
            from .string_calls import transparent_for
            excluded = transparent_for(path, o, n)
        prior = self.req
        pd = PyDiff(self, path, None, None, old_src, new_src)
        first = next((x for x in o.body if not _is_docstring(x)), None)
        entry = pd.stmt_bits(first) if first is not None else pd.lb(body_start, d_end)
        pd.block(o.body, n.body, entry, "func", None)
        for node in excluded:
            if node in self.idx["tests"]:
                self.req &= ~(1 << self.idx["tests"].index(node))
        self.req |= prior
        return True

    def _name_users_of_module(self, path: str) -> int:
        return 0


class PyDiff:
    def __init__(self, an: Analysis, path: str, old_tree, new_tree, old_src="", new_src=""):
        self.an = an
        self.old_lines = old_src.split("\n")
        self.new_lines = new_src.split("\n")
        self.path = path
        self.module = _module_name(path)
        self.old_tree = old_tree
        self.new_tree = new_tree
        self.is_test = path.startswith("tests/")
        self._old_mod_names = None
        self._new_mod_names = None

    # -- coverage helpers
    def lb(self, lo, hi):
        return self.an.lines_bits(self.path, lo, hi)

    def stmt_bits(self, s) -> int:
        """Tests that executed statement s (for compound: its header)."""
        if _is_docstring(s):
            return 0
        if isinstance(s, (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())):
            return self.lb(s.lineno, s.lineno) | self.stmt_bits(s.body[0])
        return self.lb(_start(s), _header_end(s))

    def all_bits(self, s) -> int:
        return self.lb(_start(s), s.end_lineno or s.lineno)

    def add(self, bits, why):
        self.an.add(bits, f"{self.path}: {why}")

    # -- driver
    def run(self):
        self.block(self.old_tree.body, self.new_tree.body, 0, "module", None)

    def otext(self, s) -> str:
        return "\n".join(self.old_lines[_start(s) - 1: s.end_lineno or s.lineno])

    def ntext(self, s) -> str:
        return "\n".join(self.new_lines[_start(s) - 1: s.end_lineno or s.lineno])

    def same(self, o, n) -> bool:
        """Semantically equal statements (identical text is a fast path)."""
        return self.otext(o) == self.ntext(n) or _dump(o) == _dump(n)

    def block(self, olds, news, entry, scope, owner):
        ot = [self.otext(s) for s in olds]
        nt = [self.ntext(s) for s in news]
        if ot == nt:
            return
        oset, nset = set(ot), set(nt)

        def key(st, txt, other):
            if txt in other:
                return ("t", txt)
            if isinstance(st, _DEFS + (ast.ClassDef,)):
                return ("d", type(st).__name__, st.name)
            if isinstance(st, _COMPOUND):
                return ("c", type(st).__name__, _header_dump(st))
            return ("a", _dump(st))

        okeys = [key(st, t, nset) for st, t in zip(olds, ot)]
        nkeys = [key(st, t, oset) for st, t in zip(news, nt)]
        if scope == "func":
            self.an.plaus |= entry  # cheap insurance: everyone entering the block
        sm = difflib.SequenceMatcher(None, okeys, nkeys, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                # weak keys (same def name / compound header): look inside
                for k in range(i2 - i1):
                    o, n = olds[i1 + k], news[j1 + k]
                    if okeys[i1 + k][0] in "dc" and ot[i1 + k] != nt[j1 + k]:
                        if scope == "func":
                            self.func_stmt(o, n, entry)
                        elif isinstance(o, _DEFS + (ast.ClassDef,)):
                            self.named_pair(o, n, scope, owner)
                        else:
                            self.named_range(olds, news, i1+k, i1+k+1, j1+k, j1+k+1, entry, scope, owner)
                continue
            if scope == "func":
                self.func_range(olds, news, i1, i2, j1, j2, entry)
            else:
                self.named_range(olds, news, i1, i2, j1, j2, entry, scope, owner)

    # ---------------------------------------------------------------- functions
    def insertion_bits(self, olds, i, entry):
        if i > 0 and not _is_docstring(olds[i - 1]):
            return self.stmt_bits(olds[i - 1])
        if i < len(olds):
            k = i
            while k < len(olds) and _is_docstring(olds[k]):
                k += 1
            if k < len(olds):
                return self.stmt_bits(olds[k])
        return entry

    def func_range(self, olds, news, i1, i2, j1, j2, entry):
        if i2 - i1 == j2 - j1:
            for o, n in zip(olds[i1:i2], news[j1:j2]):
                if not self.same(o, n):
                    self.func_stmt(o, n, entry)
            return
        if i2 > i1:
            b = 0
            for o in olds[i1:i2]:
                b |= self.stmt_bits(o)
            if j2 > j1:
                b |= self.insertion_bits(olds, i1, entry)
            self.add(b, f"replaced statements L{_start(olds[i1])}-{olds[i2 - 1].end_lineno}")
        else:
            b = self.insertion_bits(olds, i1, entry)
            where = olds[i1 - 1].end_lineno if i1 > 0 else (olds[0].lineno if olds else "?")
            self.add(b, f"inserted statements near L{where}")

    def func_stmt(self, o, n, entry):
        if type(o) is not type(n):
            self.add(self.stmt_bits(o) | self.insertion_bits([o], 0, entry),
                     f"statement L{o.lineno} changed kind")
            return
        hb = self.stmt_bits(o)
        if isinstance(o, _DEFS):
            if _def_header(o) != _def_header(n):
                self.add(hb, f"nested def {o.name} header L{o.lineno}")
            self.block(o.body, n.body, hb, "func", None)
            return
        if isinstance(o, ast.ClassDef):
            self.add(hb | self.all_bits(o), f"local class {o.name} L{o.lineno}")
            return
        if isinstance(o, (ast.If, ast.While)):
            if _dump(o.test) != _dump(n.test):
                condition_bits = hb
                if self.an.one_code_edit:
                    condition_bits &= ~_unchanged_branch_bits(self.module, o.test, n.test)
                self.add(condition_bits, f"condition L{o.lineno}")
            self.block(o.body, n.body, hb, "func", None)
            self.block(o.orelse, n.orelse, hb, "func", None)
            return
        if isinstance(o, (ast.For, ast.AsyncFor)):
            if _dump(o.target) != _dump(n.target) or _dump(o.iter) != _dump(n.iter):
                self.add(hb, f"loop header L{o.lineno}")
            self.block(o.body, n.body, hb, "func", None)
            self.block(o.orelse, n.orelse, hb, "func", None)
            return
        if isinstance(o, (ast.With, ast.AsyncWith)):
            if [_dump(x) for x in o.items] != [_dump(x) for x in n.items]:
                self.add(hb, f"with header L{o.lineno}")
            self.block(o.body, n.body, hb, "func", None)
            return
        if isinstance(o, (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())):
            self.block(o.body, n.body, hb, "func", None)
            self.block(o.orelse, n.orelse, hb, "func", None)
            self.block(o.finalbody, n.finalbody, hb, "func", None)
            if len(o.handlers) != len(n.handlers):
                self.add(hb, f"try handlers L{o.lineno}")
            else:
                for ho, hn in zip(o.handlers, n.handlers):
                    if _dump(ho) == _dump(hn):
                        continue
                    if (_dump(ho.type) if ho.type else None) != (
                            _dump(hn.type) if hn.type else None) or ho.name != hn.name:
                        self.add(hb, f"except clause L{ho.lineno}")
                    self.block(ho.body, hn.body, hb, "func", None)
            return
        if self.an.one_code_edit and isinstance(o, ast.Return):
            hb &= ~_equal_comparison_bits(self.module, o.value, n.value)
        self.add(hb, f"statement L{o.lineno}")

    # ------------------------------------------------------- class / module bodies
    def named_range(self, olds, news, i1, i2, j1, j2, entry, scope, owner):
        """Align defs/classes by name inside a changed range of a class or
        module body; everything else is an attribute/binding change."""
        o_named, n_named = {}, {}
        o_other, n_other = [], []
        for s in olds[i1:i2]:
            if isinstance(s, _DEFS + (ast.ClassDef,)):
                o_named.setdefault(s.name, []).append(s)
            else:
                o_other.append(s)
        for s in news[j1:j2]:
            if isinstance(s, _DEFS + (ast.ClassDef,)):
                n_named.setdefault(s.name, []).append(s)
            else:
                n_other.append(s)
        for name in list(o_named) + [k for k in n_named if k not in o_named]:
            ol, nl = o_named.get(name, []), n_named.get(name, [])
            for k in range(max(len(ol), len(nl))):
                o = ol[k] if k < len(ol) else None
                n = nl[k] if k < len(nl) else None
                if o is not None and n is not None:
                    if self.otext(o) != self.ntext(n):
                        self.named_pair(o, n, scope, owner)
                elif o is not None:
                    self.named_removed(o, scope, owner)
                else:
                    self.named_added(n, scope, owner, olds)
        if o_other or n_other:
            od = [_dump(s) for s in o_other]
            nd = [_dump(s) for s in n_other]
            if od != nd:
                if scope == "class":
                    self.class_attr_change(o_other, n_other, owner, olds, i1)
                else:
                    self.module_stmt_change(o_other, n_other, olds, i1)

    def owner_q(self, owner):
        return owner[0] if owner else None

    def class_family(self, owner) -> int:
        q, node = owner[0], owner[1]
        return self.an.family(self.module, q, self.path, node)

    def named_pair(self, o, n, scope, owner):
        if isinstance(o, ast.ClassDef) and isinstance(n, ast.ClassDef):
            q = f"{owner[0]}.{o.name}" if owner else o.name
            own = (q, o, n)
            if _class_header(o) != _class_header(n):
                self.add(self.class_family(own) | self.risky_bits([n], o),
                         f"class {q} header (bases/decorators) L{o.lineno}")
            self.block(o.body, n.body, self.stmt_bits(o), "class", own)
            return
        if isinstance(o, _DEFS) and isinstance(n, _DEFS):
            body_bits = self.lb(o.body[0].lineno, o.end_lineno) if o.body else 0
            q = f"{owner[0]}.{o.name}" if owner else o.name
            if _def_header(o) != _def_header(n):
                b = body_bits
                if [_dump(d) for d in o.decorator_list] != [_dump(d) for d in n.decorator_list]:
                    if scope == "class":
                        b |= self.class_family(owner)
                    else:
                        b |= self.name_refs({o.name}, depth=0)
                    b |= self.risky_bits(n.decorator_list, None)
                if _dump(o.args) != _dump(n.args):
                    b |= self.risky_bits([n.args], None)
                self.add(b, f"def {q} signature/decorators L{o.lineno}")
            first = next((x for x in o.body if not _is_docstring(x)), None)
            entry = self.stmt_bits(first) if first is not None else body_bits
            self.block(o.body, n.body, entry, "func", None)
            return
        # def <-> class swap under the same name
        self.named_removed(o, scope, owner)
        self.named_added(n, scope, owner, [])

    def named_removed(self, o, scope, owner):
        if scope == "class":
            if (
                not self.is_test
                and isinstance(o, _DEFS)
                and not o.name.startswith("__")
            ):
                safe_decs = all(
                    _is_safe_sql_decorator(d)
                    or (isinstance(d, ast.Name) and d.id in {"property", "staticmethod", "classmethod"})
                    for d in o.decorator_list
                )
                header_exprs = [
                    *o.args.defaults,
                    *o.args.kw_defaults,
                    o.returns,
                    *(a.annotation for a in ast.walk(o.args) if isinstance(a, ast.arg)),
                ]
                safe_hdr = safe_decs and all(
                    e is None or isinstance(e, (ast.Constant, ast.Name)) for e in header_exprs
                )
                b = self.lb(o.body[0].lineno, o.end_lineno) if o.body else 0
                if not safe_hdr:
                    b |= self.all_bits(o)
                b |= self.name_refs({o.name}, depth=0, exclude=[o])
                b |= _import_state_observers(self.an)
                if o.name.endswith("_sql") and owner[0].split(".")[-1].endswith("Generator"):
                    from . import dict_keys
                    sql_bits = dict_keys.sql_method_bits(o.name)
                    fam = self.class_family(owner)
                    b |= (fam & sql_bits) if sql_bits is not None else fam
                self.add(b, f"{owner[0]}.{o.name} removed L{o.lineno}")
                return
            self.add(self.class_family(owner) | self.all_bits(o),
                     f"{owner[0]}.{o.name} removed L{o.lineno}")
        else:
            b = 0
            if isinstance(o, _DEFS):
                # Defining a function loads the module. Calling it reaches its
                # body. Preserve header execution when defaults, annotations
                # or decorators can perform work at import time.
                b = self.lb(o.body[0].lineno, o.end_lineno)
                header = [*o.decorator_list, *o.args.defaults, *o.args.kw_defaults,
                          o.returns, *(arg.annotation for arg in ast.walk(o.args)
                                       if isinstance(arg, ast.arg))]
                if o.decorator_list or any(expr is not None and not isinstance(expr, (ast.Constant, ast.Name))
                                          for expr in header):
                    b |= self.all_bits(o)
                b |= _import_state_observers(self.an)
            if isinstance(o, ast.ClassDef):
                b |= self.an.family(self.module, o.name, self.path, o)
            gone = {o.name} - self.module_names_new()
            b |= self.name_refs({o.name}, depth=0, exclude=[o], removed=gone)
            self.add(b, f"top-level {o.name} removed L{o.lineno}")

    def named_added(self, n, scope, owner, olds):
        if scope == "class":
            if (self.is_test and isinstance(n, _DEFS) and n.name.startswith("test")):
                self.an.reasons.append(f"{self.path}: new test {owner[0]}.{n.name} (uncatalogued)")
                return
            risky = self.risky_bits([n], None)
            if (
                isinstance(n, _DEFS)
                and not n.name.startswith(("__", "test"))
            ):
                if (
                    not n.decorator_list
                    and not n.name.endswith("_sql")
                    and not n.name.isupper()
                    and not _name_sites(self.an, n.name)
                ):
                    self.add(risky, f"{owner[0]}.{n.name} added (brand-new helper)")
                    return
                if (
                    n.name.endswith("_sql")
                    and owner[0].split(".")[-1].endswith("Generator")
                    and all(_is_safe_sql_decorator(d) for d in n.decorator_list)
                ):
                    from . import dict_keys
                    sql_bits = dict_keys.sql_method_bits(n.name)
                    if sql_bits is not None:
                        fam = self.class_family(owner)
                        narrowed = fam & (sql_bits | self.name_refs({n.name}, depth=0))
                        self.add(narrowed | risky, f"{owner[0]}.{n.name} added (sql dispatch)")
                        return
            self.add(self.class_family(owner) | risky,
                     f"{owner[0]}.{n.name} added (dispatch by name)")
            return
        # module level: a new name nobody at the pin references, unless it
        # rebinds an existing one
        names = self.module_names_old()
        b = self.risky_bits([n], None)
        if n.name in names:
            b |= self.name_refs({n.name}, depth=0)
            self.add(b, f"top-level {n.name} redefined")
        else:
            self.add(b, f"new top-level {n.name}")

    # ------------------------------------------------------------ class attrs
    def class_attr_change(self, o_other, n_other, owner, olds, i1):
        q = owner[0]
        executed = 0
        for s in o_other:
            executed |= self.stmt_bits(s)
        if not o_other:
            executed = self.insertion_bits(olds, i1, self.stmt_bits(owner[1]))
        docs_only = all(_is_docstring(s) for s in o_other + n_other)
        if docs_only:
            self.an.reasons.append(f"{self.path}: docstring of {q} only")
            return
        if not executed and o_other:
            self.an.reasons.append(f"{self.path}: class {q} body never executed")
            return
        if _copies(o_other) > _copies(n_other):
            # Pure deletion of a subclass override of a read-only dispatch dict
            # (e.g. ``FUNCTION_PARSERS = {**Parser.FUNCTION_PARSERS, ...}`` or
            # ``PROPERTIES_LOCATION = {**PostgresGenerator.PROPERTIES_LOCATION, ...}``)
            # simply inherits the base dict without introducing shared-state aliasing.
            _IMMUTABLE_DISPATCH_DICTS = frozenset({
                "FUNCTION_PARSERS", "PROPERTIES_LOCATION", "TRANSFORMS",
                "TYPE_MAPPING", "KEYWORDS", "NO_PAREN_FUNCTIONS",
                "SINGLE_TOKENS", "RANGE_PARSERS", "PROPERTY_PARSERS",
                "STATEMENT_PARSERS", "PRIMARY_PARSERS",
            })
            old_bound = set().union(*(_bound_names(s) for s in o_other)) if o_other else set()
            new_bound = set().union(*(_bound_names(s) for s in n_other)) if n_other else set()
            pure_dict_del = (
                bool(old_bound)
                and old_bound <= _IMMUTABLE_DISPATCH_DICTS
                and not (old_bound & new_bound)
                and all(
                    isinstance(s, (ast.Pass,)) or _is_docstring(s)
                    for s in n_other
                )
            )
            if not pure_dict_del:
                raise Unsupported(f"{self.path}: copy removed in class body of {q} "
                                  "(shared state aliasing across tests)")
        fam = self.class_family(owner)
        risky = self.risky_bits(n_other, None, class_scope=owner[2])
        names = set()
        simple = True
        for s in o_other + n_other:
            if _is_docstring(s):
                continue
            if not isinstance(s, (ast.Assign, ast.AnnAssign)):
                simple = False
            names |= _bound_names(s)
        # generator dispatch finds ``*_sql`` attributes through dir(cls)
        # attributes that disappear: qualified class-level reads (``Cls.NAME``
        # in other class bodies / module level) now raise at import
        gone = set()
        for st in owner[1].body:
            gone |= _bound_names(st)
        for st in owner[2].body:
            gone -= _bound_names(st)
        gone &= names
        if gone:
            risky |= self.removed_class_attrs(gone, q, fam)
        # pure addition of duplicate identical literal class constant already in class body
        if not o_other and n_other:
            old_dumps = {_dump(st) for st in owner[1].body}
            if all(
                _is_docstring(s)
                or (
                    isinstance(s, (ast.Assign, ast.AnnAssign))
                    and _dump(s) in old_dumps
                    and isinstance(s.value, ast.Constant)
                    and type(s.value.value) in (bool, int, float, str, type(None))
                )
                for s in n_other
            ):
                # Ensure no later statement in old class body binds the same name differently.
                last_vals = {}
                for st in owner[1].body:
                    if isinstance(st, (ast.Assign, ast.AnnAssign)):
                        for nm in _bound_names(st):
                            last_vals[nm] = _dump(st)
                if all(
                    _is_docstring(s)
                    or all(last_vals.get(nm) == _dump(s) for nm in _bound_names(s))
                    for s in n_other
                ):
                    self.an.reasons.append(f"{self.path}: duplicate class constant(s) {sorted(names)[:3]} of {q}")
                    return
        # pure addition of brand-new TokenType / DType enum member(s) (`X = auto()`):
        # zero files in pinned SQLGlot iterate TokenType or DType or rely on their int values.
        if (
            not o_other
            and n_other
            and (
                (self.path == "sqlglot/tokenizer_core.py" and q == "TokenType")
                or (self.path == "sqlglot/expressions/datatypes.py" and q == "DType")
            )
        ):
            old_enum_names = set()
            for st in owner[1].body:
                old_enum_names |= _bound_names(st)
            if all(
                _is_docstring(s)
                or (
                    isinstance(s, ast.Assign)
                    and len(s.targets) == 1
                    and isinstance(s.targets[0], ast.Name)
                    and s.targets[0].id not in old_enum_names
                    and not s.targets[0].id.startswith("_")
                    and isinstance(s.value, ast.Call)
                    and isinstance(s.value.func, ast.Name)
                    and s.value.func.id == "auto"
                    and not s.value.args
                    and not s.value.keywords
                    and not _name_sites(self.an, s.targets[0].id)
                )
                for s in n_other
            ):
                self.an.reasons.append(f"{self.path}: new {q} member(s) {sorted(names)[:3]} -> 0 extra tests")
                return
        # enum members are also read by iteration (``for d in Dialects``)
        if any(re.search(r"Enum|AutoName|Flag", _dump(b)) for b in owner[1].bases):
            risky |= self.name_refs({q.rsplit(".", 1)[-1]}, depth=0)
            self.an.reasons.append(f"{self.path}: {q} is an enum: all users of the class")
        if simple and names and not any(n.endswith("_sql") or n.startswith("__")
                                        for n in names):
            b = _attr_refs(self.an, names, self.path, f"{self.module}.{q}", q, fam,
                           o_other)
            if (q.split(".")[-1] == "Tokenizer" and
                    (self.path == "sqlglot/tokens.py" or self.path.startswith("sqlglot/dialects/"))):
                from . import token_options
                extra_tt = self.an.__dict__.get("patch_added_cls", {}).get("sqlglot.tokenizer_core.TokenType", ())
                keys = token_options.changed_keywords(o_other, n_other, extra_kinds=extra_tt)
                if keys:
                    b = token_options.keyword_bits(self.an, b, keys)
                if self.an.one_code_edit:
                    name = token_options.option(o_other, n_other)
                    readers = token_options.read_bits(self.an, name) if name else None
                    if readers is not None:
                        b &= readers
                        if name == "NUMERIC_ESCAPES":
                            b = token_options.keyed_bits(self.an, b, f"{self.module}.{q}",
                                                         o_other[0], n_other[0])
            if (self.path == "sqlglot/dialects/dialect.py"
                    and q == "Dialect" and names == {"DATE_PART_MAPPING"}):
                from .token_options import date_unit_bits
                b = date_unit_bits(self, b, o_other, n_other)
            if len(names) == 1:
                attr_name = next(iter(names))
                brand_new_tt = self.an.__dict__.get("patch_brand_new_tokens")
                if (
                    brand_new_tt
                    and (self.path == "sqlglot/parser.py" or self.path.startswith("sqlglot/parsers/"))
                    and _only_adds_brand_new_tokens(attr_name, o_other, n_other, owner[1], brand_new_tt)
                ):
                    b = 0
                else:
                    from . import dict_keys
                    if attr_name in dict_keys.TABLES:
                        narrowed = dict_keys.narrow_bits(attr_name, b, o_other, n_other)
                        if narrowed is not None:
                            b = narrowed
            self.an.excluded |= fam & ~b  # for offline validation of the refinement
            how = "readers"
        else:
            b = fam
            how = "family"
        self.add(b | risky, f"class attribute(s) {sorted(names)[:3]} of {q} ({how}) "
                            f"L{o_other[0].lineno if o_other else '+'}")

    def _inherited_from_unmodified_base(self, q: str, name: str) -> bool:
        cls_full = f"{self.module}.{q}"
        mro = self.an.idx.get("mro", {}).get(cls_full, ())
        ns_cls = self.an.idx.get("ns_cls", {})
        changed = self.an.__dict__.get("changed_paths", ())
        srcs = _sources()["src"]
        for base in mro:
            if base == cls_full:
                continue
            if name not in ns_cls.get(base, ()):
                continue
            # Resolve base module path; conservatively bail if base file is also edited.
            parts = base.split(".")
            base_path = None
            for i in range(len(parts) - 1, 0, -1):
                cand = "/".join(parts[:i]) + ".py"
                if cand in srcs:
                    base_path = cand
                    break
                cand_init = "/".join(parts[:i]) + "/__init__.py"
                if cand_init in srcs:
                    base_path = cand_init
                    break
            if base_path is None or base_path in changed:
                continue
            return True
        return False

    def removed_class_attrs(self, gone, q, fam: int = 0) -> int:
        simple = q.rsplit(".", 1)[-1]
        b = 0
        for name in gone:
            if self._inherited_from_unmodified_base(q, name):
                continue
            qual = re.compile(r"\b" + re.escape(simple) + r"\s*\.\s*" + re.escape(name) + r"\b")
            for path, ln, lo, hi, sc in _name_sites(self.an, name):
                if path == self.path and sc is not None and sc[3] == q:
                    continue
                if sc is not None and sc[0] == "def" and ln >= sc[4]:
                    bits = self.an.lines_bits(path, lo, hi)
                    if fam and not qual.search("\n".join(_src_lines(path)[lo - 1: hi])):
                        b |= (bits & fam) | _import_state_observers(self.an)
                    else:
                        b |= bits
                    continue
                if qual.search("\n".join(_src_lines(path)[lo - 1: hi])):
                    b |= self.an.import_break(path, f"reads removed {simple}.{name}")
        self.an.reasons.append(f"{self.path}: removed class attribute(s) {sorted(gone)[:3]} of {q}")
        return b

    # ------------------------------------------------------------ module level
    def module_names_old(self):
        if self._old_mod_names is None:
            self._old_mod_names = _toplevel_names(self.old_tree)
        return self._old_mod_names

    def module_names_new(self):
        if self._new_mod_names is None:
            self._new_mod_names = _toplevel_names(self.new_tree)
        return self._new_mod_names

    def module_stmt_change(self, o_other, n_other, olds, i1):
        executed = 0
        for s in o_other:
            executed |= self.all_bits(s)
        if not o_other:
            executed = self.insertion_bits(olds, i1, self.an.file_bits(self.path))
        if all(_is_docstring(s) for s in o_other + n_other):
            self.an.reasons.append(f"{self.path}: module docstring only")
            return
        if not executed:
            self.an.reasons.append(f"{self.path}: module statement never executed by any test")
            return
        if _copies(o_other) > _copies(n_other):
            raise Unsupported(f"{self.path}: copy removed at module level "
                              "(shared state aliasing across tests)")
        simple = all(isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Import,
                                    ast.ImportFrom)) or _is_docstring(s)
                     for s in o_other + n_other)
        names = set()
        imp_o, imp_n = {}, {}
        for s in o_other + n_other:
            if isinstance(s, (ast.Import, ast.ImportFrom)):
                tgt = imp_o if s in o_other else imp_n
                for a in s.names:
                    tgt[(a.asname or a.name).split(".")[0]] = (
                        getattr(s, "module", None), getattr(s, "level", 0), a.name)
            else:
                names |= _bound_names(s)
        # imports: only bindings whose source actually changed
        names |= {k for k in set(imp_o) | set(imp_n) if imp_o.get(k) != imp_n.get(k)}
        if not simple or (not names and not (imp_o or imp_n)):
            may_raise = any(isinstance(n, (ast.Raise, ast.Call, ast.Assert, ast.Import,
                                           ast.ImportFrom, ast.Subscript, ast.Attribute))
                            for st in n_other for n in ast.walk(st))
            if may_raise:
                executed |= self.an.import_break(self.path, "module-level statement may raise")
            self.add(executed, f"module-level statement L{o_other[0].lineno if o_other else '+'}"
                               " (import users)")
            return
        risky = self.risky_bits(n_other, None)
        # PEP 562 lazy exports / __all__: string data names the module's exports
        if self.has_dynamic_exports():
            so, sn = _str_consts(o_other), _str_consts(n_other)
            dyn_removed = {x for x in so - sn if x.isidentifier()}
            dyn_added = {x for x in sn - so if x.isidentifier()}
            if dyn_removed or dyn_added:
                self.an.reasons.append(f"{self.path}: dynamic exports -{sorted(dyn_removed)[:3]}"
                                       f" +{sorted(dyn_added)[:3]}")
                names = names | dyn_removed | dyn_added
                risky |= self.name_refs(dyn_removed, depth=0, exclude=o_other,
                                        removed=frozenset(dyn_removed))
        new_edges = self.new_import_edges(o_other, n_other)
        if new_edges:
            risky |= _import_state_observers(self.an)
            self.an.reasons.append(f"{self.path}: new import edge(s) {sorted(new_edges)[:3]}"
                                   " -> import-state observers")
        gone = names - self.module_names_new()
        refs = self.name_refs(names, depth=0, exclude=o_other, removed=gone)
        if self.path.startswith("sqlglot/typing/") and names == {"EXPRESSION_METADATA"} and not gone:
            from . import dict_keys
            narrowed = dict_keys.narrow_bits("EXPRESSION_METADATA", refs, o_other, n_other)
            if narrowed is not None:
                refs = narrowed
        elif self.path == "sqlglot/jsonpath.py" and names == {"JSON_PATH_PART_TRANSFORMS"} and not gone:
            from . import dict_keys
            narrowed = dict_keys.narrow_bits(
                "TRANSFORMS", refs, o_other, n_other, target_name="JSON_PATH_PART_TRANSFORMS"
            )
            if narrowed is not None:
                refs = narrowed
        self.an.excluded |= executed & ~refs
        self.add(risky | refs,
                 f"module-level binding(s) {sorted(names)[:4]}")

    def has_dynamic_exports(self) -> bool:
        for tree in (self.old_tree, self.new_tree):
            for st in tree.body:
                if isinstance(st, _DEFS) and st.name in ("__getattr__", "__dir__"):
                    return True
                if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and (
                        "__all__" in _bound_names(st)):
                    return True
        return False

    def new_import_edges(self, o_other, n_other) -> set:
        """Modules the new statements import that this module did not already
        import (transitively) at the pin: import side effects move."""
        graph = self.an.idx.get("imports", {})
        reach, stack = set(), [self.module]
        while stack:
            m = stack.pop()
            if m in reach:
                continue
            reach.add(m)
            stack.extend(graph.get(m, ()))
        out = set()
        for st in n_other:
            if isinstance(st, ast.Import):
                tg = [a.name for a in st.names]
            elif isinstance(st, ast.ImportFrom):
                base = st.module or ""
                if st.level:
                    base = _resolve_rel("." * st.level + base, self.path)
                tg = [base] + [f"{base}.{a.name}" for a in st.names
                               if f"{base}.{a.name}" in graph]
            else:
                continue
            for t in tg:
                if t.startswith(("sqlglot", "tests")) and t not in reach:
                    out.add(t)
        return out

    # ------------------------------------------------------------ references
    def name_refs(self, names, depth, exclude=(), removed=frozenset()) -> int:
        """Tests reaching a reference to any of ``names`` (whole-word match over
        the pinned sources, classified by the enclosing scope)."""
        return _name_refs(self.an, names, self.path, depth, exclude, removed)

    # ------------------------------------------------------------ import risk
    def risky_bits(self, stmts, old, class_scope=None) -> int:
        """If a new module/class-level statement may fail when the module is
        imported (unresolvable name, unknown import), every importer is hit."""
        why = _import_risk(self, stmts, class_scope)
        if why:
            self.an.reasons.append(f"{self.path}: import risk ({why})")
            return self.an.import_break(self.path, f"import risk ({why})")
        return 0


_OBSERVER_RE = re.compile(r"sys\.modules|\b_classes\b|subprocess|import_module|importlib"
                          r"|\.classes\b|entry_points|__import__")


def _import_state_observers(an: Analysis) -> int:
    """Tests whose own code inspects import state (sys.modules, the dialect
    registry, lazy loading in a subprocess, ...)."""
    if "_observers" in an.idx:
        return an.idx["_observers"]
    bits = 0
    for path, text in _sources()["src"].items():
        if not path.startswith("tests/"):
            continue
        for k, line in enumerate(text.split("\n"), 1):
            if _OBSERVER_RE.search(line):
                sc = _scope_at(an, path, k)
                if sc is not None and sc[0] == "def":
                    bits |= an.lines_bits(path, sc[4], sc[2])
    an.idx["_observers"] = bits
    return bits


_COPY_FUNCS = {"copy", "deepcopy", "dict", "list", "set", "tuple", "frozenset",
               "MappingProxyType", "OrderedDict", "defaultdict"}


def _copies(stmts) -> int:
    """Number of explicit copies (x.copy(), copy(x), dict(x), {**x}, [*x]) in
    statements; removing one at import time aliases shared state."""
    k = 0
    for st in stmts:
        for n in ast.walk(st):
            if isinstance(n, ast.Call):
                f = n.func
                if (isinstance(f, ast.Attribute) and f.attr in ("copy", "deepcopy")) or (
                        isinstance(f, ast.Name) and f.id in _COPY_FUNCS and n.args):
                    k += 1
            elif isinstance(n, ast.Dict) and any(key is None for key in n.keys):
                k += 1
            elif isinstance(n, (ast.List, ast.Set, ast.Tuple)) and any(
                    isinstance(e, ast.Starred) for e in n.elts):
                k += 1
    return k


def _str_consts(stmts) -> set:
    out = set()
    for st in stmts:
        for n in ast.walk(st):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                out.add(n.value)
    return out


def _toplevel_names(tree) -> set[str]:
    out: set[str] = set()
    for s in tree.body:
        out |= _bound_names(s)
        if isinstance(s, (ast.If, ast.Try)):
            for sub in ast.walk(s):
                if isinstance(sub, ast.stmt):
                    out |= _bound_names(sub)
        if isinstance(s, ast.ImportFrom) and any(a.name == "*" for a in s.names):
            out.add("*")
    return out


def _import_risk(pd: PyDiff, stmts, class_scope) -> str | None:
    idx = pd.an.idx
    ns_mod = idx.get("ns_mod", {})
    ns_cls = idx.get("ns_cls", {})
    importable = idx.get("importable", set())
    mod_names = pd.module_names_new()
    if "*" in mod_names:
        mod_names = mod_names | set(ns_mod.get(pd.module, {}))
    local = set()
    if class_scope is not None:
        for s in class_scope.body:
            local |= _bound_names(s)
    aliases = _aliases(pd)
    graph = idx.get("imports", {})

    def reach(src, dst):
        seen, stack = set(), [src]
        while stack:
            m = stack.pop()
            if m == dst:
                return True
            if m in seen:
                continue
            seen.add(m)
            # an ancestor package is already initialising when m is imported
            stack.extend(d for d in graph.get(m, ()) if not m.startswith(d + "."))
        return False

    def cycle(target):
        # a *new* cycle: target leads back here and was not already imported
        # (transitively) by this module at the pin
        return reach(target, pd.module) and not reach(pd.module, target)

    for st in stmts:
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            targets = []
            if isinstance(st, ast.Import):
                targets = [a.name for a in st.names]
            else:
                base = st.module or ""
                if st.level:
                    base = _resolve_rel("." * st.level + base, pd.path)
                targets = [base] + [f"{base}.{a.name}" for a in st.names]
            for tmod in targets:
                if tmod in graph and tmod != pd.module and cycle(tmod):
                    return f"import cycle via {tmod}"
        if isinstance(st, ast.Import):
            for a in st.names:
                top = a.name.split(".")[0]
                if a.name not in ns_mod and top not in importable:
                    return f"import {a.name}"
                if top in ("sqlglot", "tests") and a.name not in ns_mod and (
                        _static_names(a.name) is None):
                    return f"import {a.name} (no such module)"
            continue
        if isinstance(st, ast.ImportFrom):
            if st.level:
                base = pd.module.split(".")
                base = base[: len(base) - st.level + (1 if pd.path.endswith("__init__.py") else 0)]
                modname = ".".join(base + ([st.module] if st.module else []))
            else:
                modname = st.module or ""
            if modname in ns_mod:
                added_m = pd.an.__dict__.get("patch_added_mod", {}).get(modname, ())
                for a in st.names:
                    if (a.name != "*" and a.name not in ns_mod[modname]
                            and a.name not in added_m
                            and f"{modname}.{a.name}" not in ns_mod):
                        return f"from {modname} import {a.name}"
            elif modname.split(".")[0] in ("sqlglot", "tests"):
                static = _static_names(modname)
                added_m = pd.an.__dict__.get("patch_added_mod", {}).get(modname, ())
                if static is None and not added_m:
                    return f"from {modname} (no such module)"
                static_eff = static or set()
                for a in st.names:
                    if (a.name != "*" and a.name not in static_eff and a.name not in added_m
                            and "*" not in static_eff
                            and "__getattr__" not in static_eff
                            and _static_names(f"{modname}.{a.name}") is None):
                        return f"from {modname} import {a.name}"
            elif modname.split(".")[0] not in importable:
                return f"from {modname}"
            continue
        defined = set()
        for n in ast.walk(st):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                defined.add(n.id)
            elif isinstance(n, ast.arg):
                defined.add(n.arg)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(n.name)
        # names evaluated at definition time (skip function bodies / lambdas)
        for n in _eval_time_nodes(st):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                if (n.id not in mod_names and n.id not in _BUILTINS and n.id not in local
                        and n.id not in defined):
                    return f"name {n.id!r} L{n.lineno}"
            elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
                tgt = aliases.get(n.value.id)
                if tgt is None:
                    continue
                kind, full = tgt
                if kind == "mod" and full in ns_mod:
                    added_m = pd.an.__dict__.get("patch_added_mod", {}).get(full, ())
                    if (n.attr not in ns_mod[full] and n.attr not in added_m
                            and f"{full}.{n.attr}" not in ns_mod):
                        return f"{n.value.id}.{n.attr} L{n.lineno}"
                elif kind == "cls" and full in ns_cls:
                    added_c = pd.an.__dict__.get("patch_added_cls", {}).get(full, ())
                    if n.attr not in ns_cls[full] and n.attr not in added_c:
                        return f"{n.value.id}.{n.attr} L{n.lineno}"
    return None


def _static_names(modname: str):
    """Top-level names of a module of the pinned sources (None: no such
    module).  '*' / '__getattr__' in the result mean "anything may exist"."""
    cache = _sources().setdefault("_static_ns", {})
    if modname in cache:
        return cache[modname]
    srcs = _sources()["src"]
    base = modname.replace(".", "/")
    path = base + ".py" if base + ".py" in srcs else (
        base + "/__init__.py" if base + "/__init__.py" in srcs else None)
    if path is None:
        known = _sources()["files"]
        # namespace package (directory without __init__.py)
        res = set() if any(f.startswith(base + "/") for f in known) else None
    else:
        try:
            res = _toplevel_names(ast.parse(srcs[path]))
        except SyntaxError:
            res = {"*"}
    cache[modname] = res
    return res


def _eval_time_nodes(st):
    """Nodes of a statement evaluated when the enclosing module/class body
    runs: decorators, defaults, bases, annotations are lazy (``from
    __future__ import annotations`` is used throughout) -> skipped."""
    stack = [st]
    while stack:
        n = stack.pop()
        if isinstance(n, _DEFS):
            stack.extend(n.decorator_list)
            stack.extend(d for d in n.args.defaults if d is not None)
            stack.extend(d for d in n.args.kw_defaults if d is not None)
            continue
        if isinstance(n, ast.ClassDef):
            stack.extend(n.decorator_list)
            stack.extend(n.bases)
            stack.extend(k.value for k in n.keywords)
            stack.extend(n.body)
            continue
        if isinstance(n, ast.Lambda):
            stack.extend(d for d in n.args.defaults if d is not None)
            continue
        if isinstance(n, ast.AnnAssign):
            if n.value is not None:
                stack.append(n.value)
            stack.append(n.target)
            continue
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            # comprehension variables are local; still check iterables
            for g in n.generators:
                stack.append(g.iter)
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def _aliases(pd: PyDiff) -> dict:
    """name -> ("mod", module) | ("cls", class) for imports in the new module."""
    ns_mod = pd.an.idx.get("ns_mod", {})
    out = {}
    for s in pd.new_tree.body:
        if isinstance(s, ast.Import):
            for a in s.names:
                if a.asname:
                    out[a.asname] = ("mod", a.name)
        elif isinstance(s, ast.ImportFrom) and s.module and not s.level:
            for a in s.names:
                name = a.asname or a.name
                full = f"{s.module}.{a.name}"
                if full in ns_mod:
                    out[name] = ("mod", full)
                else:
                    kind = pd.an.idx.get("ns_kind", {}).get(full)
                    if kind == "cls":
                        out[name] = ("cls", full)
    return out


def _scope_at(an: Analysis, path: str, line: int):
    """Innermost (kind, start, end, qualname, body_start) scope containing line."""
    sc = an.idx.get("scopes", {}).get(path)
    if not sc:
        return None
    starts, items = sc
    k = bisect.bisect_right(starts, line) - 1
    while k >= 0:
        it = items[k]
        if it[2] >= line:
            return it
        k -= 1
    return None


def _stmt_span(an: Analysis, path: str, line: int) -> tuple[int, int]:
    spans = an.idx.get("spans", {}).get(path)
    if spans:
        starts = spans[0]
        k = bisect.bisect_right(starts, line) - 1
        while k >= 0 and starts[k] >= line - 400:
            if spans[1][k] >= line:
                return starts[k], spans[1][k]
            k -= 1
    return line, line


def _name_refs(an: Analysis, names, def_path, depth, exclude=(), removed=frozenset()) -> int:
    """Tests that can observe a change of the module/class-level bindings
    ``names`` defined in ``def_path``: whole-word references over the pinned
    sources, classified by the enclosing scope of each reference."""
    excl = set()
    for s in exclude:
        excl.update(range(_start(s), (s.end_lineno or s.lineno) + 1))
    bits = 0
    for n in sorted({n for n in names if n and n != "*"}):
        bits |= _refs_one(an, n, def_path, frozenset(excl), depth, n in removed)
    return bits


def _refs_one(an: Analysis, name: str, def_path: str, excl, depth: int, removed: bool) -> int:
    memo = an.__dict__.setdefault("_ref_memo", {})
    key = (name, def_path, excl, removed)
    if key in memo:
        return memo[key]
    memo[key] = 0  # cycle guard
    srcs = _sources()["src"]
    bits = 0
    mods = _reexporters(def_path)
    early, late = [], []
    for path, text in srcs.items():
        if name not in text:
            continue
        if path == def_path:
            pat = re.compile(r"(?<![\w.])" + re.escape(name) + r"\b")
        else:
            local, aliases = _file_bindings(path, text, mods, name)
            alts = [r"(?<![\w.])" + re.escape(x) + r"\b" for x in local]
            alts += [r"(?<![\w.])" + re.escape(a).replace(r"\.", r"\s*\.\s*") + r"\s*\.\s*"
                     + re.escape(name) + r"\b" for a in aliases]
            # the import statements that bind it are sites too (re-exports)
            alts += [r"^[ \t]*from[ \t]+[\w.]+[ \t]+import[ \t]+(?:\([^)]*|[^\n]*)\b"
                     + re.escape(name) + r"\b"] if local else []
            if not alts:
                continue
            pat = re.compile("|".join(alts), re.M)
        ln, last = 1, 0
        seen_spans = set()
        for m in pat.finditer(text):
            ln += text.count("\n", last, m.start())
            last = m.start()
            if path == def_path and ln in excl:
                continue
            lo, hi = _stmt_span(an, path, ln)
            if (lo, hi) in seen_spans:
                continue
            seen_spans.add((lo, hi))
            sc = _scope_at(an, path, ln)
            in_body = sc is not None and sc[0] == "def" and ln >= sc[4]
            (late if in_body else early).append((path, ln, lo, hi))
    if removed:
        new_self_src = an.__dict__.get("_new_srcs", {}).get(def_path)
        self_gone_clean = (
            new_self_src is not None
            and not re.search(r"\b" + re.escape(name) + r"\b", new_self_src)
        )
    else:
        self_gone_clean = False
    # import-time sites first: a removed name there may end the analysis early
    for path, ln, lo, hi in early + late:
        if path == def_path and self_gone_clean:
            continue
        bits |= _ref_site_bits(an, path, ln, lo, hi, name, depth, removed)
    memo[key] = bits
    return bits


def _src_lines(path: str) -> list:
    cache = _sources().setdefault("_lines", {})
    if path not in cache:
        cache[path] = _sources()["src"][path].split("\n")
    return cache[path]


def _matching_lines(text, pattern):
    line, previous = 1, 0
    for match in pattern.finditer(text):
        line += text.count("\n", previous, match.start())
        previous = match.start()
        yield line


@functools.lru_cache(maxsize=1)
def _name_index():
    path = _PIN_CACHE / "names.db"
    if path.exists():
        import sqlite3
        return sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    return None


def _name_sites(an: "Analysis", name: str) -> list:
    """Every whole-word occurrence of ``name`` in the pinned sources, one per
    statement span: (path, line, span_lo, span_hi, scope)."""
    cache = _sources().setdefault("_sites", {})
    if name in cache:
        return cache[name]
    db = _name_index()
    if db is None:
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        rows = ((path, _matching_lines(text, pat))
                for path, text in _sources()["src"].items() if name in text)
    else:
        rows = ((path, marshal.loads(lines)) for path, lines in db.execute(
            "SELECT path,rows FROM sites WHERE name=?", (name,)))
    out = []
    for path, lines in rows:
        seen = set()
        for ln in lines:
            lo, hi = _stmt_span(an, path, ln)
            if (lo, hi) in seen:
                continue
            seen.add((lo, hi))
            out.append((path, ln, lo, hi, _scope_at(an, path, ln)))
    cache[name] = out
    return out


class _Span:
    decorator_list = ()

    def __init__(self, lo, hi):
        self.lineno, self.end_lineno = lo, hi


def _attr_refs(an: Analysis, names, def_path, cls_full, cls_q, fam, exclude, depth=0) -> int:
    """Tests that can observe a changed class attribute ``names`` of class
    ``cls_full``: (users of the class family that execute a textual read of
    the attribute) + test code reading it + observers of attributes derived
    from it in class bodies (subclasses or explicit ``Cls.NAME``), recursively
    with the same rule."""
    memo = an.__dict__.setdefault("_attr_memo", {})
    excl = set()
    for s in exclude:
        excl.update(range(_start(s), (s.end_lineno or s.lineno) + 1))
    key = (tuple(sorted(names)), def_path, cls_full, min(excl, default=0))
    if key in memo:
        return memo[key]
    memo[key] = 0  # cycle guard
    srcs = _sources()["src"]
    mro = an.idx.get("mro", {})
    test_allowed = None
    if def_path.startswith("tests/"):
        imp_fn = _importers_of(def_path)
        sub_mods = {
            c.rsplit(".", 1)[0]
            for c, bases in mro.items()
            if cls_full in bases
        }
        test_allowed = {
            p for p, src in srcs.items()
            if p == def_path or imp_fn(p, src) or _module_name(p) in sub_mods
        }
    r1 = direct = derived = 0
    for name in names:
        qual = re.compile(r"\b" + re.escape(cls_q).replace(r"\.", r"\s*\.\s*")
                          + r"\s*\.\s*" + re.escape(name) + r"\b")
        for path, ln, lo, hi, sc in _name_sites(an, name):
            if path == def_path and ln in excl:
                continue
            if sc is not None and sc[0] == "def" and ln >= sc[4]:
                bits = an.lines_bits(path, lo, hi)
                if path.startswith("tests/"):
                    if test_allowed is None or path in test_allowed:
                        direct |= bits
                else:
                    r1 |= bits
                continue
            lines = _src_lines(path)[lo - 1: hi]
            span_text = "\n".join(lines)
            if sc is None:  # module level
                if qual.search(span_text):
                    derived |= an.file_bits(path)
                continue
            in_body = sc[0] == "class" and ln >= sc[4]
            owner = sc[3] if in_body else (sc[3].rsplit(".", 1)[0] if "." in sc[3] else None)
            if owner is None:  # header (decorator/default) of a top-level def
                if qual.search(span_text):
                    derived |= an.lines_bits(path, sc[4], sc[2])
                continue
            x_full = f"{_module_name(path)}.{owner}"
            if not (x_full == cls_full or cls_full in mro.get(x_full, ())
                    or qual.search(span_text)):
                continue  # another class's attribute of the same name
            xfam = an.family(_module_name(path), owner, path)
            mb = re.match(r"^\s*([A-Za-z_]\w*)\s*(?::[^=]*)?=(?!=)", lines[0] if lines else "")
            if in_body and mb and depth < 3:
                derived |= _attr_refs(an, {mb.group(1)}, path, x_full, owner, xfam,
                                      [_Span(lo, hi)], depth + 1)
            else:
                derived |= xfam
    res = (fam & r1) | direct | derived
    memo[key] = res
    return res


_STAR_RE = re.compile(r"^\s*from\s+(\.*[\w.]*)\s+import\s+\*", re.M)


def _resolve_rel(mod: str, path: str) -> str:
    if not mod.startswith("."):
        return mod
    level = len(mod) - len(mod.lstrip("."))
    pkg = _module_name(path).split(".")
    if not path.endswith("__init__.py"):
        pkg = pkg[:-1]
    pkg = pkg[: len(pkg) - (level - 1)] if level > 1 else pkg
    rest = mod.lstrip(".")
    return ".".join(pkg + ([rest] if rest else []))


def _importers_of(def_path: str):
    """Predicate (path, text) -> may this file bind a name of def_path's module
    (import of the module, ``from pkg import module``, ``from module import``,
    relative imports, or through star re-exports)."""
    cache = _sources().setdefault("_importers", {})
    if def_path in cache:
        return cache[def_path]
    srcs = _sources()["src"]
    target = _module_name(def_path)
    mods = {target}
    star = _sources().get("_star")
    if star is None:
        star = _sources()["_star"] = {}
        for p, t in srcs.items():
            for m in _STAR_RE.findall(t):
                star.setdefault(_resolve_rel(m, p), set()).add(_module_name(p))
    changed = True
    while changed:
        changed = False
        for m in list(mods):
            for imp in star.get(m, ()):
                if imp not in mods:
                    mods.add(imp)
                    changed = True
    pats = []
    for m in mods:
        parent, _, last = m.rpartition(".")
        pats.append(re.compile(r"^[ \t]*(?:import[ \t]+|from[ \t]+)" + re.escape(m)
                               + r"(?:[ \t]|$|[ \t]*,)", re.M))
        pats.append(re.compile(r"^[ \t]*import[ \t]+[^\n]*\b" + re.escape(m) + r"\b", re.M))
        if parent:
            pats.append(re.compile(r"^[ \t]*from[ \t]+" + re.escape(parent)
                                   + r"[ \t]+import[ \t]+(?:\([^)]*|[^\n]*)\b" + re.escape(last)
                                   + r"\b", re.M))

    def pred(path, text):
        if any(p.search(text) for p in pats):
            return True
        if "from ." in text:
            for m in re.findall(r"from\s+(\.+[\w.]*)\s+import\s+([^\n(]*|\([^)]*\))", text):
                full = _resolve_rel(m[0], path)
                if full in mods or any(f"{full}.{x.strip()}" in mods for x in
                                       re.split(r"[,\s()]+", m[1]) if x.strip()):
                    return True
        return False

    cache[def_path] = pred
    return pred


def _reexporters(def_path: str) -> set:
    """def_path's module plus modules re-exporting it with ``import *``."""
    cache = _sources().setdefault("_reexp", {})
    if def_path in cache:
        return cache[def_path]
    srcs = _sources()["src"]
    star = _sources().get("_star")
    if star is None:
        star = _sources()["_star"] = {}
        for p, t in srcs.items():
            for m in _STAR_RE.findall(t):
                star.setdefault(_resolve_rel(m, p), set()).add(_module_name(p))
    mods = {_module_name(def_path)}
    todo = list(mods)
    while todo:
        for imp in star.get(todo.pop(), ()):
            if imp not in mods:
                mods.add(imp)
                todo.append(imp)
    cache[def_path] = mods
    return mods


_FROM_RE = re.compile(r"^[ \t]*from[ \t]+(\.*[\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[^\n]*)", re.M)
_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+([^\n]*)", re.M)


def _parsed_imports(path: str, text: str):
    cache = _sources().setdefault("_imports_parsed", {})
    if path in cache:
        return cache[path]
    froms, imps = [], []
    for mod, names in _FROM_RE.findall(text):
        mod = _resolve_rel(mod, path)
        for item in re.sub(r"#[^\n]*", "", names).strip("()").split(","):
            item = item.strip().strip("()\\ \n")
            if not item:
                continue
            parts = item.split()
            nm = parts[0]
            asn = parts[2] if len(parts) >= 3 and parts[1] == "as" else None
            froms.append((mod, nm, asn))
    for names in _IMPORT_RE.findall(text):
        for item in re.sub(r"#.*", "", names).split(","):
            parts = item.split()
            if not parts:
                continue
            imps.append((parts[0], parts[2] if len(parts) >= 3 and parts[1] == "as" else None))
    cache[path] = (froms, imps)
    return cache[path]


def _file_bindings(path: str, text: str, mods: set, name: str):
    """Local names under which ``path`` binds ``name`` from one of ``mods``,
    and expressions that alias one of ``mods`` (for ``alias.name``)."""
    froms, imps = _parsed_imports(path, text)
    local, aliases = set(), set()
    for mod, nm, asn in froms:
        if mod in mods:
            if nm == name:
                local.add(asn or nm)
            elif nm == "*":
                local.add(name)
        if f"{mod}.{nm}" in mods:
            aliases.add(asn or nm)
    for mod, asn in imps:
        if mod in mods:
            aliases.add(asn or mod)
        elif asn is None and any(m.startswith(mod + ".") or m == mod for m in mods):
            aliases.add(mod)
    return local, aliases


def _span_mentions(path: str, lo: int, hi: int, name: str) -> bool:
    """Is ``name`` an identifier (not just text in a string/comment) in the
    statement spanning lo..hi?  Unparseable fragments count as yes."""
    import textwrap

    text = "\n".join(_sources()["src"][path].split("\n")[lo - 1: hi])
    try:
        tree = ast.parse(textwrap.dedent(text))
    except SyntaxError:
        try:
            tree = ast.parse("if 1:\n" + textwrap.indent(textwrap.dedent(text), " "))
        except SyntaxError:
            return True
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id == name:
            return True
        if isinstance(n, ast.Attribute) and n.attr == name:
            return True
        if isinstance(n, ast.alias) and (n.name == name or n.asname == name
                                         or n.name.split(".")[0] == name):
            return True
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
            return True
        if isinstance(n, ast.keyword) and n.arg == name:
            return True
    return False


def _ref_site_bits(an, path, ln, lo, hi, name, depth, removed) -> int:
    sc = _scope_at(an, path, ln)
    if sc is not None and ln >= sc[4]:
        if sc[0] == "def":
            return an.lines_bits(path, lo, hi)
        # class body: evaluated at import (NameError breaks the module)
        if removed and _span_mentions(path, lo, hi, name):
            return an.import_break(path, f"uses removed {name}")
        return an.family(_module_name(path), sc[3])
    if sc is not None:
        # def/class header: decorators, defaults, bases
        if removed and _span_mentions(path, lo, hi, name):
            return an.import_break(path, f"uses removed {name}")
        if sc[0] == "def":
            owner = sc[3].rsplit(".", 1)[0] if "." in sc[3] else None
            return an.lines_bits(path, sc[4], sc[2]) | (
                an.family(_module_name(path), owner) if owner else 0)
        return an.family(_module_name(path), sc[3])
    # module level
    lines = _sources()["src"][path].split("\n")[lo - 1: hi]
    head = lines[0].lstrip() if lines else ""
    if not _span_mentions(path, lo, hi, name):
        return 0
    if removed:
        return an.import_break(path, f"imports/uses removed {name}")
    if depth >= 4:
        return an.file_bits(path)
    if head.startswith("from ") or head.startswith("import "):
        joined = " ".join(t.strip() for t in lines)
        m = re.search(r"\b" + re.escape(name) + r"\s+as\s+(\w+)", joined)
        alias = m.group(1) if m else name
        return _refs_one(an, alias, path, frozenset(range(lo, hi + 1)), depth + 1, False)
    m = re.match(r"^([A-Za-z_]\w*)\s*(?::[^=]*)?=(?!=)", head)
    if m:
        return _refs_one(an, m.group(1), path, frozenset(range(lo, hi + 1)), depth + 1, False)
    if head.startswith("#"):
        return 0
    return an.lines_bits(path, lo, hi)


# --------------------------------------------------------------------------- API


def _resolve(bits: int, idx: dict, catalog: list[dict]) -> tuple[set[int], set[int]]:
    tests = idx["tests"]
    by_id = {}
    i = 0
    while bits:
        if bits & 1:
            by_id[tests[i]] = True
        bits >>= 1
        i += 1
    known = idx.get("_known")
    if known is None:
        known = idx["_known"] = set(tests)
    sel, unknown = set(), set()
    for t in catalog:
        tid = t["id"]
        if tid in by_id:
            sel.add(int(t["number"]))
        elif tid not in known:
            unknown.add(int(t["number"]))
    return sel, unknown


def _inert_companion(fp):
    if fp.status != "M" or fp.binary:
        return False
    if fp.path.startswith("tests/fixtures/") and not fp.path.endswith(".py"):
        return True
    if not fp.path.startswith("sqlglot/") or not fp.path.endswith(".py"):
        return False
    if any(line.strip() and not line.lstrip().startswith("#") for h in fp.hunks
           for tag, line in h.lines if tag in ("-", "+")):
        return False
    old = _sources()["src"].get(fp.path)
    if old is None:
        return False
    new, _ = apply_hunks(old, fp.hunks)
    return ast.dump(ast.parse(old.encode())) == ast.dump(ast.parse(new.encode()))


_PARSER_TOKEN_SETS = frozenset({
    "TYPE_TOKENS", "FUNC_TOKENS", "ID_VAR_TOKENS", "ALIAS_TOKENS", "TABLE_ALIAS_TOKENS",
})
_PARSER_TOKEN_DICTS = frozenset({
    "PRIMARY_PARSERS", "RANGE_PARSERS", "UNARY_PARSERS", "STATEMENT_PARSERS",
})


def _is_safe_sql_decorator(dec: ast.AST) -> bool:
    """True for ``@unsupported_args("...")`` decorators on Generator ``*_sql`` methods."""
    if not isinstance(dec, ast.Call):
        return False
    fn = dec.func
    is_ua = (
        (isinstance(fn, ast.Name) and fn.id == "unsupported_args")
        or (isinstance(fn, ast.Attribute) and fn.attr == "unsupported_args")
    )
    if not is_ua:
        return False
    return all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in dec.args) and all(
        isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str) for kw in dec.keywords
    )


def _is_brand_new_token_node(node: ast.AST, brand_new_tt: set[str]) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "TokenType"
        and node.attr in brand_new_tt
    )


def _single_attr_rhs(stmts: list[ast.stmt], attr_name: str) -> ast.AST | None:
    non_doc = [s for s in stmts if not _is_docstring(s)]
    if len(non_doc) != 1:
        return None
    s = non_doc[0]
    if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
        return s.value if s.targets[0].id == attr_name else None
    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name) and s.value is not None:
        return s.value if s.target.id == attr_name else None
    return None


def _base_attr_matches_owner(node: ast.AST, attr_name: str, owner_cls: ast.ClassDef) -> bool:
    """True when ``node`` is ``<Base>.<attr_name>`` matching a direct base of ``owner_cls``."""
    if not (isinstance(node, ast.Attribute) and node.attr == attr_name):
        return False
    base_dumps = {_dump(b) for b in owner_cls.bases}
    if _dump(node.value) in base_dumps:
        return True
    # Match e.g. ``parser.Parser.PRIMARY_PARSERS`` when base is ``parser.Parser`` or ``Parser``.
    if isinstance(node.value, ast.Attribute):
        return any(
            (isinstance(b, ast.Name) and b.id == node.value.attr)
            or (isinstance(b, ast.Attribute) and b.attr == node.value.attr)
            for b in owner_cls.bases
        )
    if isinstance(node.value, ast.Name):
        return any(
            (isinstance(b, ast.Name) and b.id == node.value.id)
            or (isinstance(b, ast.Attribute) and b.attr == node.value.id)
            for b in owner_cls.bases
        )
    return False


def _only_adds_brand_new_tokens(
    attr_name: str,
    o_other: list[ast.stmt],
    n_other: list[ast.stmt],
    owner_cls: ast.ClassDef,
    brand_new_tt: set[str],
) -> bool:
    """True when a Parser token-set or TokenType-keyed dispatch-dict edit ONLY
    adds entries for brand-new ``TokenType`` members introduced by this patch."""
    if attr_name not in _PARSER_TOKEN_SETS and attr_name not in _PARSER_TOKEN_DICTS:
        return False
    n_rhs = _single_attr_rhs(n_other, attr_name)
    if n_rhs is None:
        return False
    if attr_name in _PARSER_TOKEN_SETS:
        if not o_other:
            # Brand-new subclass override: {*Base.ATTR, TokenType.NEW} or Base.ATTR | {TokenType.NEW}
            if isinstance(n_rhs, ast.Set):
                starred = [e.value for e in n_rhs.elts if isinstance(e, ast.Starred)]
                plain = [e for e in n_rhs.elts if not isinstance(e, ast.Starred)]
                return (
                    len(starred) == 1
                    and bool(plain)
                    and _base_attr_matches_owner(starred[0], attr_name, owner_cls)
                    and all(_is_brand_new_token_node(e, brand_new_tt) for e in plain)
                )
            if (
                isinstance(n_rhs, ast.BinOp)
                and isinstance(n_rhs.op, ast.BitOr)
                and _base_attr_matches_owner(n_rhs.left, attr_name, owner_cls)
                and isinstance(n_rhs.right, ast.Set)
                and bool(n_rhs.right.elts)
                and all(_is_brand_new_token_node(e, brand_new_tt) for e in n_rhs.right.elts)
            ):
                return True
            return False
        o_rhs = _single_attr_rhs(o_other, attr_name)
        if o_rhs is None:
            return False
        if isinstance(o_rhs, ast.Set) and isinstance(n_rhs, ast.Set):
            kept = [e for e in n_rhs.elts if not _is_brand_new_token_node(e, brand_new_tt)]
            return len(kept) < len(n_rhs.elts) and [_dump(e) for e in kept] == [_dump(e) for e in o_rhs.elts]
        if isinstance(n_rhs, ast.BinOp) and isinstance(n_rhs.op, ast.BitOr) and isinstance(n_rhs.right, ast.Set):
            kept = [e for e in n_rhs.right.elts if not _is_brand_new_token_node(e, brand_new_tt)]
            if len(kept) < len(n_rhs.right.elts):
                if not kept:
                    return _dump(n_rhs.left) == _dump(o_rhs)
                if isinstance(o_rhs, ast.BinOp) and isinstance(o_rhs.op, ast.BitOr) and isinstance(o_rhs.right, ast.Set):
                    return _dump(n_rhs.left) == _dump(o_rhs.left) and [_dump(e) for e in kept] == [_dump(e) for e in o_rhs.right.elts]
        return False

    # _PARSER_TOKEN_DICTS
    if not isinstance(n_rhs, ast.Dict):
        return False
    if not o_other:
        unpacks = [v for k, v in zip(n_rhs.keys, n_rhs.values) if k is None]
        pairs = [(k, v) for k, v in zip(n_rhs.keys, n_rhs.values) if k is not None]
        return (
            len(unpacks) == 1
            and bool(pairs)
            and _base_attr_matches_owner(unpacks[0], attr_name, owner_cls)
            and all(_is_brand_new_token_node(k, brand_new_tt) for k, _ in pairs)
        )
    o_rhs = _single_attr_rhs(o_other, attr_name)
    if not isinstance(o_rhs, ast.Dict):
        return False
    kept_pairs = [
        (k, v) for k, v in zip(n_rhs.keys, n_rhs.values)
        if not (k is not None and _is_brand_new_token_node(k, brand_new_tt))
    ]
    if len(kept_pairs) >= len(n_rhs.keys):
        return False
    old_pairs = list(zip(o_rhs.keys, o_rhs.values))
    return [(_dump(k) if k is not None else None, _dump(v)) for k, v in kept_pairs] == [
        (_dump(k) if k is not None else None, _dump(v)) for k, v in old_pairs
    ]


def _collect_patch_additions(an: Analysis, files) -> None:
    """Pre-collect newly added module-level and class-level symbols across all
    cleanly compiling Python files in the patch so cross-file references to
    newly added symbols (e.g. ``TokenType.NEW`` or ``exp.NewExpr``) do not
    trigger false ``import risk`` full-suite fallbacks."""
    added_mod: dict[str, set[str]] = {}
    added_cls: dict[str, set[str]] = {}
    brand_new_tokens: set[str] = set()
    preparsed: dict[str, tuple] = {}
    srcs = _sources()["src"]
    ns_cls = an.idx.get("ns_cls", {})
    for fp in files:
        if fp.binary or fp.status == "D" or not fp.path or not fp.path.endswith(".py"):
            continue
        # Only sqlglot/ modules and tests/helpers.py export symbols imported by other files.
        if fp.path.startswith("tests/") and fp.path != "tests/helpers.py":
            continue
        try:
            if fp.status == "A":
                new_src = "\n".join(line for h in fp.hunks for tag, line in h.lines if tag == "+") + "\n"
                placed = None
            else:
                old_src = srcs.get(fp.old_path or fp.path)
                if old_src is None or not fp.hunks:
                    continue
                new_src, placed = apply_hunks(old_src, fp.hunks)
            tree = ast.parse(new_src, filename=fp.path)
            patched_code = compile(tree, fp.path, "exec", dont_inherit=True)
            if fp.status != "A" and placed is not None:
                preparsed[fp.old_path or fp.path] = (new_src, placed, patched_code, tree)
        except Exception:
            continue
        mod = _module_name(fp.path)
        reexp = _reexporters(fp.path) | {mod}
        if fp.path == "sqlglot/tokenizer_core.py":
            reexp |= {"sqlglot.tokens", "sqlglot"}
        top_names = _toplevel_names(tree)
        for rm in reexp:
            added_mod.setdefault(rm, set()).update(top_names)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            cls_attrs = set()
            for st in node.body:
                cls_attrs |= _bound_names(st)
                if isinstance(st, _DEFS):
                    cls_attrs.add(st.name)
            for rm in reexp:
                full = f"{rm}.{node.name}"
                if rm == mod or full in ns_cls:
                    added_cls.setdefault(full, set()).update(cls_attrs)
            if fp.path == "sqlglot/tokenizer_core.py" and node.name == "TokenType":
                old_tt = ns_cls.get("sqlglot.tokenizer_core.TokenType", ())
                for st in node.body:
                    if (
                        isinstance(st, ast.Assign)
                        and len(st.targets) == 1
                        and isinstance(st.targets[0], ast.Name)
                        and st.targets[0].id not in old_tt
                        and not st.targets[0].id.startswith("_")
                        and isinstance(st.value, ast.Call)
                        and isinstance(st.value.func, ast.Name)
                        and st.value.func.id == "auto"
                        and not st.value.args
                        and not st.value.keywords
                        and not _name_sites(an, st.targets[0].id)
                    ):
                        brand_new_tokens.add(st.targets[0].id)
    an.patch_added_mod = added_mod
    an.patch_added_cls = added_cls
    an.patch_brand_new_tokens = brand_new_tokens
    an._preparsed = preparsed


def select(patch_text: str, catalog: list[dict]) -> dict:
    an = Analysis()
    try:
        files = parse_patch(patch_text)
        if not files:
            raise Unsupported("no file diffs found")
        an.single_file = len(files) == 1
        an.changed_paths = frozenset(
            p for f in files for p in (f.old_path, f.new_path) if p
        )
        an.fixture_only = all(f.path and f.path.startswith("tests/fixtures/") for f in files)
        an.safe_fixture_proj = not any(
            p in {"tests/helpers.py", "tests/test_executor.py", "tests/test_optimizer.py"}
            for f in files for p in (f.old_path, f.new_path) if p
        )
        # Existing fixture edits contribute their own observed readers. They
        # cannot introduce new Python code before those reads.
        an.one_code_edit = an.single_file or sum(not _inert_companion(f) for f in files) == 1
        if len(files) > 1:
            _collect_patch_additions(an, files)
        for fp in files:
            an.analyze(fp)
    except Unsupported as e:
        return {"required": set(), "plausible": set(), "full": True,
                "reasons": an.reasons + [f"FULL: {e}"]}
    req, unknown = _resolve(an.req, an.idx, catalog)
    plaus, _ = _resolve(an.plaus & ~an.req, an.idx, catalog)
    if unknown:
        an.reasons.append(f"{len(unknown)} catalog tests unknown to the maps -> required")
    if an.check_imports:
        return {"full": True, "if_collects": {"required": req | unknown, "plausible": plaus - req},
                "reasons": an.reasons}
    return {"required": req | unknown, "plausible": plaus - req - unknown, "full": False,
            "precise_fixture": an.precise_fixture,
            "reasons": an.reasons}
