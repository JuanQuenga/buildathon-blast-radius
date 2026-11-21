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
import lzma
import os
import pickle
import re
from pathlib import Path

MAPS_DIR = Path(os.environ.get("BLAST_MAPS", str(Path(__file__).resolve().parent / "maps")))

_IDX: dict | None = None
_SRC: dict | None = None
_BUILTINS = frozenset(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__",
                                        "__path__", "__package__", "__loader__",
                                        "__builtins__", "__annotations__", "__dict__",
                                        "__qualname__", "__module__", "__class__"}

FULL_PATHS = {"pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", "tox.ini",
              "conftest.py", "tests/conftest.py", "tests/__init__.py", "requirements.txt",
              "sqlglot/py.typed"}


def _index() -> dict:
    global _IDX
    if _IDX is None:
        _IDX = pickle.loads(lzma.decompress((MAPS_DIR / "index.pkl.xz").read_bytes()))
    return _IDX


def _sources() -> dict:
    global _SRC
    if _SRC is None:
        _SRC = pickle.loads(lzma.decompress((MAPS_DIR / "src.pkl.xz").read_bytes()))
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
    __slots__ = ("old_path", "new_path", "status", "hunks", "binary")

    def __init__(self):
        self.old_path = self.new_path = None
        self.status = "M"
        self.hunks: list[Hunk] = []
        self.binary = False

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
            elif line.startswith("@@"):
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
                if o_left < 0 or n_left < 0:
                    raise Unsupported("hunk length mismatch")
                continue
        i += 1
    known = set(_sources()["files"])
    for f in files:
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


class Analysis:
    def __init__(self):
        self.idx = _index()
        self.req = 0
        self.plaus = 0
        self.full = False
        self.reasons: list[str] = []
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
            raise Unsupported(f"{why}: {path} is imported during collection")
        return self.file_bits(path)

    def family(self, module: str, qualname: str, path: str | None = None,
               node=None) -> int:
        b = 0
        sid = self.idx["families"].get(f"{module}.{qualname}")
        if sid is not None:
            b |= self.sets(sid)
        if path and node is not None:
            # code of the class that ran (classmethods/staticmethods without instances)
            for s in ast.walk(node):
                if isinstance(s, _DEFS) and s.body:
                    b |= self.lines_bits(path, s.body[0].lineno, s.end_lineno)
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
        if fp.binary:
            self.data_file(fp)
            if any(p and (p.endswith(".py") or p.startswith("sqlglot/")) for p in (oldp, newp)):
                raise Unsupported(f"binary change to {path}")
            return
        for p in (oldp, newp):
            if p and (p in FULL_PATHS or p.rsplit("/", 1)[-1] == "conftest.py"):
                raise Unsupported(f"config/collection file {p}")
        is_py = lambda p: p is not None and p.endswith(".py") and (
            p.startswith("sqlglot/") or p.startswith("tests/")) and not p.startswith(
            "tests/fixtures/")
        if is_py(oldp) or is_py(newp):
            self.python_file(fp)
        else:
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
            self.add(self.import_break(oldp, "module removed/renamed"),
                     f"{oldp} removed/renamed: all its importers")
            self.add(self._name_users_of_module(oldp), "")
            return
        if not fp.hunks:
            return
        new_src, _ = apply_hunks(old_src, fp.hunks)
        try:
            old_tree = ast.parse(old_src)
        except SyntaxError:
            raise Unsupported(f"pinned {oldp} does not parse")
        try:
            new_tree = ast.parse(new_src)
        except (SyntaxError, ValueError) as e:
            self.add(self.import_break(oldp, f"no longer compiles ({type(e).__name__})"),
                     f"{oldp} no longer compiles")
            return
        PyDiff(self, oldp, old_tree, new_tree).run()

    def _name_users_of_module(self, path: str) -> int:
        return 0


class PyDiff:
    def __init__(self, an: Analysis, path: str, old_tree, new_tree):
        self.an = an
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

    def block(self, olds, news, entry, scope, owner):
        od = [_dump(s) for s in olds]
        nd = [_dump(s) for s in news]
        if od == nd:
            return
        sm = difflib.SequenceMatcher(None, od, nd, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
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
                if _dump(o) != _dump(n):
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
                self.add(hb, f"condition L{o.lineno}")
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
                    if _dump(o) != _dump(n):
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
        q, node = owner
        return self.an.family(self.module, q, self.path, node)

    def named_pair(self, o, n, scope, owner):
        if isinstance(o, ast.ClassDef) and isinstance(n, ast.ClassDef):
            q = f"{owner[0]}.{o.name}" if owner else o.name
            own = (q, o)
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
            self.add(self.class_family(owner) | self.all_bits(o),
                     f"{owner[0]}.{o.name} removed L{o.lineno}")
        else:
            b = self.all_bits(o) if isinstance(o, _DEFS) else 0
            if isinstance(o, ast.ClassDef):
                b |= self.an.family(self.module, o.name, self.path, o)
            gone = {o.name} - self.module_names_new()
            b |= self.name_refs({o.name}, depth=0, removed=gone)
            self.add(b, f"top-level {o.name} removed L{o.lineno}")

    def named_added(self, n, scope, owner, olds):
        if scope == "class":
            if (self.is_test and isinstance(n, _DEFS) and n.name.startswith("test")):
                self.an.reasons.append(f"{self.path}: new test {owner[0]}.{n.name} (uncatalogued)")
                return
            self.add(self.class_family(owner) | self.risky_bits([n], None),
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
        b = self.class_family(owner) | self.risky_bits(n_other, None, class_scope=owner[1])
        self.add(b, f"class attribute(s) of {q} "
                    f"L{o_other[0].lineno if o_other else '+'}")

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
            self.add(executed, f"module-level statement L{o_other[0].lineno if o_other else '+'}"
                               " (import users)")
            return
        risky = self.risky_bits(n_other, None)
        gone = names - self.module_names_new()
        self.add(risky | self.name_refs(names, depth=0, exclude=o_other, removed=gone),
                 f"module-level binding(s) {sorted(names)[:4]}")

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
    for st in stmts:
        if isinstance(st, ast.Import):
            for a in st.names:
                top = a.name.split(".")[0]
                if a.name not in ns_mod and top not in importable:
                    return f"import {a.name}"
            continue
        if isinstance(st, ast.ImportFrom):
            if st.level:
                base = pd.module.split(".")
                base = base[: len(base) - st.level + (1 if pd.path.endswith("__init__.py") else 0)]
                modname = ".".join(base + ([st.module] if st.module else []))
            else:
                modname = st.module or ""
            if modname in ns_mod:
                for a in st.names:
                    if a.name != "*" and a.name not in ns_mod[modname] and (
                            f"{modname}.{a.name}" not in ns_mod):
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
                    if n.attr not in ns_mod[full] and f"{full}.{n.attr}" not in ns_mod:
                        return f"{n.value.id}.{n.attr} L{n.lineno}"
                elif kind == "cls" and full in ns_cls:
                    if n.attr not in ns_cls[full]:
                        return f"{n.value.id}.{n.attr} L{n.lineno}"
    return None


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
    pat = re.compile(r"\b" + re.escape(name) + r"\b")
    bits = 0
    importers = _importers_of(def_path) if removed else None
    early, late = [], []
    for path, text in srcs.items():
        if name not in text:
            continue
        if importers is not None and path != def_path and not importers(path, text):
            continue  # cannot be bound to the removed definition
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
    # import-time sites first: a removed name there may end the analysis early
    for path, ln, lo, hi in early + late:
        bits |= _ref_site_bits(an, path, ln, lo, hi, name, depth, removed)
    memo[key] = bits
    return bits


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
    """Predicate (path, text) -> may this file import a binding of def_path's
    module (directly, via its package, or via star re-exports)."""
    srcs = _sources()["src"]
    target = _module_name(def_path)
    mods = {target}
    changed = True
    star = _sources().get("_star")
    if star is None:
        star = _sources()["_star"] = {}
        for p, t in srcs.items():
            for m in _STAR_RE.findall(t):
                star.setdefault(_resolve_rel(m, p), set()).add(_module_name(p))
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
        pats.append(re.compile(r"\b" + re.escape(m) + r"\b"))
        if parent:
            pats.append(re.compile(r"from\s+" + re.escape(parent) + r"\s+import\s+[^;]*?\b"
                                   + re.escape(last) + r"\b", re.S))
    rel_last = {m.rpartition(".")[2] for m in mods}

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

    return pred


def _ref_site_bits(an, path, ln, lo, hi, name, depth, removed) -> int:
    sc = _scope_at(an, path, ln)
    if sc is not None and ln >= sc[4]:
        if sc[0] == "def":
            return an.lines_bits(path, lo, hi)
        # class body: evaluated at import (NameError breaks the module)
        if removed:
            return an.import_break(path, f"uses removed {name}")
        return an.family(_module_name(path), sc[3])
    if sc is not None:
        # def/class header: decorators, defaults, bases
        if removed:
            return an.import_break(path, f"uses removed {name}")
        if sc[0] == "def":
            owner = sc[3].rsplit(".", 1)[0] if "." in sc[3] else None
            return an.lines_bits(path, sc[4], sc[2]) | (
                an.family(_module_name(path), owner) if owner else 0)
        return an.family(_module_name(path), sc[3])
    # module level
    lines = _sources()["src"][path].split("\n")[lo - 1: hi]
    head = lines[0].lstrip() if lines else ""
    if removed and not head.startswith("#"):
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


def select(patch_text: str, catalog: list[dict]) -> dict:
    an = Analysis()
    try:
        files = parse_patch(patch_text)
        if not files:
            raise Unsupported("no file diffs found")
        for fp in files:
            an.analyze(fp)
    except Unsupported as e:
        return {"required": set(), "plausible": set(), "full": True,
                "reasons": an.reasons + [f"FULL: {e}"]}
    req, unknown = _resolve(an.req, an.idx, catalog)
    plaus, _ = _resolve(an.plaus & ~an.req, an.idx, catalog)
    if unknown:
        an.reasons.append(f"{len(unknown)} catalog tests unknown to the maps -> required")
    return {"required": req | unknown, "plausible": plaus - req - unknown, "full": False,
            "reasons": an.reasons}
