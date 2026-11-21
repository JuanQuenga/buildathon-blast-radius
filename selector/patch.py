"""Minimal, dependency-free unified-diff parser (git format).

``parse(text) -> list[FileDiff]``.  Raises ``PatchError`` on anything it does
not understand, so callers can fall back to the full suite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


class PatchError(ValueError):
    pass


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    section: str  # text after the second @@ (git's enclosing-function hint)
    lines: list[str] = field(default_factory=list)  # raw lines incl. ' ', '+', '-', '\\'

    @property
    def removed_old_lines(self) -> list[int]:
        """Old-side line numbers of '-' lines."""
        out, o = [], self.old_start
        for l in self.lines:
            c = l[:1]
            if c == "-":
                out.append(o)
                o += 1
            elif c == " " or l == "":
                o += 1
        return out

    @property
    def added_new_lines(self) -> list[int]:
        out, n = [], self.new_start
        for l in self.lines:
            c = l[:1]
            if c == "+":
                out.append(n)
                n += 1
            elif c == " " or l == "":
                n += 1
        return out

    @property
    def insertion_points_old(self) -> list[int]:
        """For each run of '+' lines, the old-side line number *before* which
        it is inserted (old_start-based)."""
        out, o, prev_plus = [], self.old_start, False
        for l in self.lines:
            c = l[:1]
            if c == "+":
                if not prev_plus:
                    out.append(o)
                prev_plus = True
                continue
            prev_plus = False
            if c == "-" or c == " " or l == "":
                o += 1
        return out

    @property
    def removed(self) -> list[str]:
        return [l[1:] for l in self.lines if l[:1] == "-"]

    @property
    def added(self) -> list[str]:
        return [l[1:] for l in self.lines if l[:1] == "+"]


@dataclass
class FileDiff:
    old_path: str | None  # None for /dev/null (new file)
    new_path: str | None  # None for /dev/null (deleted file)
    hunks: list[Hunk] = field(default_factory=list)
    is_new: bool = False
    is_deleted: bool = False
    is_rename: bool = False
    is_binary: bool = False
    mode_change: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def paths(self) -> set[str]:
        return {p for p in (self.old_path, self.new_path) if p}


def _unquote(p: str) -> str:
    """git C-style quoted path ("a/f\\303\\251.py") -> str."""
    if not (len(p) >= 2 and p[0] == '"' and p[-1] == '"'):
        return p
    body, out, i = p[1:-1], bytearray(), 0
    esc = {"n": 10, "t": 9, '"': 34, "\\": 92, "a": 7, "b": 8, "f": 12, "r": 13, "v": 11}
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            d = body[i + 1]
            if d in "01234567" and i + 3 < len(body) + 1:
                out.append(int(body[i + 1:i + 4], 8) & 0xFF)
                i += 4
                continue
            out.append(esc.get(d, ord(d)))
            i += 2
            continue
        out += c.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def _strip(p: str) -> str | None:
    """Path from a ---/+++ line, with one leading component removed (-p1),
    exactly like ``git apply``: works for a/ b/, c/ i/ w/ o/ prefixes."""
    p = p.rstrip("\n")
    if "\t" in p:
        p = p.split("\t", 1)[0]
    p = _unquote(p.strip())
    if p == "/dev/null":
        return None
    if "/" in p:
        p = p.split("/", 1)[1]
    return p


def _header_paths(rest: str) -> tuple[str | None, str | None]:
    """Paths from 'diff --git <a> <b>' (both -p1 stripped)."""
    rest = rest.strip()
    if rest.startswith('"'):
        j = rest.find('" ', 1)
        while j != -1 and rest[j - 1] == "\\":
            j = rest.find('" ', j + 1)
        if j == -1:
            return None, None
        return _strip(rest[:j + 1]), _strip(rest[j + 2:])
    if rest.endswith('"'):
        j = rest.rfind(' "')
        return _strip(rest[:j]), _strip(rest[j + 1:])
    # unquoted: same path on both sides unless renamed; split in the middle
    n = len(rest)
    if n % 2 == 1:
        h = n // 2
        a, b = rest[:h], rest[h + 1:]
        if rest[h] == " " and _strip(a) == _strip(b):
            return _strip(a), _strip(b)
    parts = rest.split(" ")
    if len(parts) == 2:
        return _strip(parts[0]), _strip(parts[1])
    return None, None


def parse(text: str) -> list[FileDiff]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    old_left = new_left = 0
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")
        if hunk is not None and (old_left > 0 or new_left > 0):
            c = line[:1]
            if c == "\\":
                hunk.lines.append(line)
            elif c == "-":
                hunk.lines.append(line); old_left -= 1
            elif c == "+":
                hunk.lines.append(line); new_left -= 1
            elif c == " " or line == "":
                hunk.lines.append(line if line else " "); old_left -= 1; new_left -= 1
            else:
                raise PatchError(f"bad hunk line {i + 1}: {line[:40]!r}")
            if old_left < 0 or new_left < 0:
                raise PatchError(f"hunk overflow at line {i + 1}")
            i += 1
            continue
        if line.startswith("\\") and hunk is not None:
            hunk.lines.append(line); i += 1; continue
        hunk = None
        if line.startswith("diff --git "):
            a, b = _header_paths(line[len("diff --git "):])
            cur = FileDiff(old_path=a, new_path=b)
            files.append(cur)
        elif cur is None and (line.startswith("--- ") or line.startswith("+++ ")):
            # plain unified diff without a git header
            cur = FileDiff(old_path=None, new_path=None)
            files.append(cur)
            continue
        elif cur is None:
            pass  # preamble (e.g. format-patch mail header)
        elif line.startswith("--- "):
            cur.old_path = _strip(line[4:])
            if cur.old_path is None:
                cur.is_new = True
        elif line.startswith("+++ "):
            cur.new_path = _strip(line[4:])
            if cur.new_path is None:
                cur.is_deleted = True
        elif line.startswith("new file mode"):
            cur.is_new = True
        elif line.startswith("deleted file mode"):
            cur.is_deleted = True
        elif line.startswith("rename from ") or line.startswith("copy from "):
            cur.is_rename = True; cur.old_path = _unquote(line.split(" ", 2)[2])
        elif line.startswith("rename to ") or line.startswith("copy to "):
            cur.is_rename = True; cur.new_path = _unquote(line.split(" ", 2)[2])
        elif line.startswith("old mode") or line.startswith("new mode"):
            cur.mode_change = True
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur.is_binary = True
        elif line.startswith("@@"):
            m = _HUNK.match(line)
            if not m:
                raise PatchError(f"bad hunk header line {i + 1}")
            os_, ol, ns, nl, sec = m.groups()
            hunk = Hunk(int(os_), 1 if ol is None else int(ol),
                        int(ns), 1 if nl is None else int(nl), sec.strip())
            cur.hunks.append(hunk)
            old_left, new_left = hunk.old_len, hunk.new_len
        # index lines, similarity, etc. are ignored
        i += 1
    if hunk is not None and (old_left > 0 or new_left > 0):
        raise PatchError("truncated hunk")
    if cur is not None and cur.is_deleted:
        cur.new_path = None
    if cur is not None and cur.is_new:
        cur.old_path = None
    for f in files:
        if f.is_new:
            f.old_path = None
        if f.is_deleted:
            f.new_path = None
    if not files:
        raise PatchError("no file diffs")
    return files


def changed_paths(text: str) -> set[str]:
    out: set[str] = set()
    for f in parse(text):
        out |= f.paths
    return out
