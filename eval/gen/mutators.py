"""LLM-free mutators that produce single-file edits in the organisers' style.

Each mutator takes (rng, path, text) and returns (new_text, desc) or None.
Sites are found with ``tokenize`` so strings/comments are never mutated by the
code operators (except the deliberate string/comment traps).
"""

from __future__ import annotations

import io
import keyword
import re
import tokenize

# --------------------------------------------------------------------- helpers


def _tokens(text: str):
    try:
        return list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []


def _line_offsets(text: str) -> list[int]:
    offs = [0]
    for line in text.splitlines(keepends=True):
        offs.append(offs[-1] + len(line))
    return offs


def _replace_span(text, offs, start, end, new):
    a = offs[start[0] - 1] + start[1]
    b = offs[end[0] - 1] + end[1]
    return text[:a] + new + text[b:]


def _code_line_mask(toks) -> set[int]:
    """Lines that carry at least one code token (not only strings/comments)."""
    lines = set()
    for t in toks:
        if t.type in (tokenize.NAME, tokenize.OP, tokenize.NUMBER):
            lines.add(t.start[0])
    return lines


# ------------------------------------------------------------- code operators

CMP_FLIP = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">"}


def code_sites(text: str):
    """Yield (op_name, start, end, replacement) for every mutable token."""
    toks = _tokens(text)
    sites = []
    n = len(toks)
    for i, t in enumerate(toks):
        prev = toks[i - 1] if i else None
        nxt = toks[i + 1] if i + 1 < n else None
        line = t.line
        if t.type == tokenize.OP and t.string in CMP_FLIP:
            # skip annotations like "-> " (not in table) and generics
            sites.append(("cmp", t.start, t.end, CMP_FLIP[t.string]))
        elif t.type == tokenize.NAME:
            s = t.string
            if s == "and":
                sites.append(("bool", t.start, t.end, "or"))
            elif s == "or":
                sites.append(("bool", t.start, t.end, "and"))
            elif s == "True":
                sites.append(("const", t.start, t.end, "False"))
            elif s == "False":
                sites.append(("const", t.start, t.end, "True"))
            elif s == "is" and nxt is not None:
                if nxt.string == "not":
                    sites.append(("is", t.start, nxt.end, "!="))
                else:
                    sites.append(("is", t.start, t.end, "=="))
            elif s == "not" and nxt is not None and nxt.string != "in" and (prev is None or prev.string != "is"):
                # drop the negation: "not x" -> "x"
                sites.append(("not", t.start, nxt.start, ""))
            elif s == "in" and prev is not None and prev.string != "not" and " for " not in line \
                    and not line.lstrip().startswith("for "):
                sites.append(("in", t.start, t.end, "not in"))
            elif s == "None" and prev is not None and prev.string in ("=", "return") and \
                    line.lstrip().startswith("return"):
                pass
            elif s == "return" and nxt is not None and nxt.type not in (tokenize.NEWLINE, tokenize.COMMENT) \
                    and nxt.string != "None":
                # return X -> return None  (keep X so the diff is one line)
                sites.append(("return", nxt.start, nxt.start, "None and "))
        elif t.type == tokenize.NUMBER and re.fullmatch(r"\d+", t.string):
            v = int(t.string)
            if v > 10_000:
                continue
            new = str(v + 1) if v in (0,) else str(v + 1 if (v + t.start[0]) % 2 else v - 1)
            sites.append(("num", t.start, t.end, new))
        elif t.type == tokenize.OP and t.string in ("+", "-") and prev is not None and \
                (prev.type in (tokenize.NAME, tokenize.NUMBER) or prev.string in (")", "]")) and \
                not (prev.type == tokenize.NAME and keyword.iskeyword(prev.string)):
            sites.append(("arith", t.start, t.end, "-" if t.string == "+" else "+"))
        elif t.type == tokenize.STRING and nxt is not None and nxt.string == ":" and \
                re.fullmatch(r'["\'][A-Z][A-Z0-9_ ]{1,40}["\']', t.string):
            # dispatch-table key rename: "FOO_BAR": -> "FOO_BARX":
            q = t.string[0]
            sites.append(("key", t.start, t.end, q + t.string[1:-1] + "X" + q))
    return sites


def line_sites(text: str):
    """Whole-line edits: (op_name, lineno, new_line or None for delete)."""
    lines = text.splitlines(keepends=True)
    toks = _tokens(text)
    code_lines = _code_line_mask(toks)
    # lines that are part of multi-line strings must not be touched
    str_lines = set()
    for t in toks:
        if t.type == tokenize.STRING and t.end[0] > t.start[0]:
            str_lines.update(range(t.start[0], t.end[0] + 1))
    sites = []
    for i, line in enumerate(lines, 1):
        if i not in code_lines or i in str_lines:
            continue
        s = line.strip()
        # single-line dict entry in a dispatch table: KEY: VALUE,
        if re.match(r'^(["\'][^"\']+["\']|exp\.\w+|TokenType\.\w+|[A-Z_]+)\s*:\s*.+,$', s) and \
                not s.startswith(("def ", "class ", "if ", "elif ", "else", "for ", "while ", "return", "lambda")):
            sites.append(("dict_del", i, None))
        # single-line set/list element: exp.Foo, / TokenType.FOO, / "FOO",
        elif re.match(r'^(exp\.\w+|TokenType\.\w+|["\'][A-Z_ ]+["\']|[A-Z_]{3,}),$', s):
            sites.append(("elem_del", i, None))
        # a bare statement call inside a function (side effect) e.g. self._set_type(...)
        elif re.match(r'^(self\.)?[\w.]+\(.*\)$', s) and line.startswith("        ") and \
                s.count("(") == s.count(")"):
            sites.append(("stmt_del", i, None))
    return sites


def apply_code_site(text: str, site) -> str:
    op, start, end, new = site
    offs = _line_offsets(text)
    return _replace_span(text, offs, start, end, new)


def apply_line_site(text: str, site) -> str:
    op, lineno, new = site
    lines = text.splitlines(keepends=True)
    if new is None:
        del lines[lineno - 1]
    else:
        lines[lineno - 1] = new
    return "".join(lines)


def compiles(text: str, path: str = "<m>") -> bool:
    try:
        compile(text, path, "exec")
        return True
    except SyntaxError:
        return False


# ---------------------------------------------------------- comment operators

COMMENT_PHRASES = [
    # phrases seen in the public sample
    "Cheap check first", "Kept for backwards compatibility", "Keep in sync with the generator",
    "Intentionally not cached", "Mirrors the behavior of the reference implementation",
    # more in the same voice
    "TODO: revisit once the upstream fix lands", "See the dialect docs for details",
    "Order matters here", "NOTE: this is hot code", "Fast path", "Normalize before comparing",
    "Defensive: should not happen", "pragma: no cover", "noqa", "type: ignore",
]


def comment_sites(text: str):
    """Line numbers before which a standalone comment can be inserted safely."""
    lines = text.splitlines(keepends=True)
    toks = _tokens(text)
    str_lines = set()
    for t in toks:
        if t.type == tokenize.STRING and t.end[0] > t.start[0]:
            str_lines.update(range(t.start[0] + 1, t.end[0] + 1))
    sites = []
    for i, line in enumerate(lines, 1):
        if i in str_lines or not line.strip():
            continue
        if i > 1 and lines[i - 2].rstrip().endswith("\\"):
            continue
        sites.append(i)
    return sites, str_lines


def insert_comment(text: str, lineno: int, phrase: str) -> str:
    lines = text.splitlines(keepends=True)
    target = lines[lineno - 1]
    indent = target[: len(target) - len(target.lstrip())]
    lines.insert(lineno - 1, f"{indent}# {phrase}\n")
    return "".join(lines)


# --------------------------------------------------------- fixture operators

FIXTURE_TOKENS = ["42", "0", "1", "x", "NULL", "a"]


def fixture_sites(text: str):
    """SQL lines (not comments/metadata/blank) of a fixture file."""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        out.append(i)
    return out


def mutate_sql_line(rng, line: str) -> tuple[str, str]:
    """One token-level corruption in the organisers' style."""
    if line.startswith("#"):
        # metadata line
        m = re.match(r"^#\s*(\w+)\s*:\s*(.*)$", line)
        if m:
            key, val = m.groups()
            choice = rng.random()
            if key == "dialect":
                alt = rng.choice(["bigquery", "snowflake", "duckdb", "spark", "postgres", "tsql", "mysql"])
                return f"# dialect: {alt if alt != val else 'hive'}", "meta-dialect"
            if key == "execute":
                return f"# execute: {'false' if val.strip().lower() == 'true' else 'true'}", "meta-execute"
            if choice < 0.5:
                return f"# {key}: {val} 42", "meta-value"
            return f"# {key}:{val}", "meta-space"
        return line + " 42", "meta-garbage"
    words = list(re.finditer(r"\S+", line))
    op = rng.random()
    if op < 0.55 or len(words) < 2:
        tok = rng.choice(FIXTURE_TOKENS)
        cut = rng.choice([w.start() for w in words] + [len(line)])
        return line[:cut] + tok + " " + line[cut:] if cut < len(line) else line + " " + tok, "insert-token"
    if op < 0.8:
        w = rng.choice(words)
        return (line[: w.start()] + line[w.end():]).replace("  ", " ", 1) if rng.random() < 0.5 \
            else line[: w.start()] + " " + line[w.end():], "delete-token"
    # delete one punctuation char
    puncts = [m.start() for m in re.finditer(r"[(),.<>=*]", line)]
    if puncts:
        k = rng.choice(puncts)
        return line[:k] + line[k + 1:], "delete-char"
    w = rng.choice(words)
    return line[: w.start()] + w.group().swapcase() + line[w.end():], "swapcase"
