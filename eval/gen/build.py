#!/usr/bin/env python3
"""Generate a case set of patches against the pin (unlabeled; label with eval/oracle.py).

  eval/gen/build.py --set dev --seed 1 [--code 200 --fixture 70 --comment 50 --test 40 --multi 40 --exotic 1]

Writes eval/cases/<set>/commits/<id>.patch and eval/cases/<set>/meta.json
({id: {kind, family, area, files, desc}}).  Existing cases in the set are kept;
new ones are appended (ids are content hashes, duplicates skipped).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import mutators as M  # noqa: E402
from patchmaker import GEN, case_id, ensure_gen, make_patch, read  # noqa: E402
from paths import CASES, STARTER  # noqa: E402

MAX_PATCH_BYTES = 150_000  # keep the repo small; organisers' patches are tiny anyway
RESERVED_IDS = {p.stem for p in (STARTER / "sample" / "commits").glob("*.patch")}


def area_of(path: str) -> str:
    parts = path.split("/")
    if path.startswith("sqlglot/"):
        if len(parts) == 2:
            name = parts[1][:-3]
            if name in ("parser", "generator", "tokens", "tokenizer_core", "__init__"):
                return "core"
            return "misc:" + name
        sub = parts[1]
        if sub == "dialects" and parts[2] == "dialect.py":
            return "core"
        if sub in ("dialects", "parsers", "generators", "typing"):
            if parts[2] in ("__init__.py",):
                return sub + ":init"
            return sub
        return sub
    if path.startswith("tests/fixtures/optimizer/"):
        return "fixtures:optimizer"
    if path.startswith("tests/fixtures/"):
        return "fixtures:other"
    if path.startswith("tests/"):
        return "tests"
    return "nonpy"


def prod_files() -> list[str]:
    out = []
    for p in sorted((GEN / "sqlglot").rglob("*.py")):
        rel = str(p.relative_to(GEN))
        if rel.endswith(("__main__.py", "_version.py")):
            continue
        out.append(rel)
    return out


def test_files() -> list[str]:
    return sorted(str(p.relative_to(GEN)) for p in (GEN / "tests").rglob("test_*.py"))


def fixture_files() -> list[str]:
    return sorted(str(p.relative_to(GEN)) for p in (GEN / "tests" / "fixtures").rglob("*.sql"))


def pick_file(rng, files, weight_area=None):
    if weight_area:
        groups: dict[str, list[str]] = {}
        for f in files:
            groups.setdefault(area_of(f).split(":")[0], []).append(f)
        names = sorted(groups)
        w = [weight_area.get(a.split(":")[0], 1.0) for a in names]
        g = rng.choices(names, weights=w)[0]
        return rng.choice(groups[g])
    return rng.choice(files)


# Roughly: many dialect-specific edits (like the sample), plus shared core.
AREA_WEIGHTS = {
    "dialects": 2.0, "parsers": 3.0, "generators": 3.0, "typing": 1.5, "core": 2.0,
    "optimizer": 2.5, "expressions": 1.5, "executor": 1.0, "misc": 2.0,
}


# ------------------------------------------------------------------- edits
# Each edit function returns (changes, desc, op) or None.


def edit_code(rng, path=None):
    files = prod_files()
    for _ in range(50):
        p = path or pick_file(rng, files, AREA_WEIGHTS)
        text = read(p)
        if rng.random() < 0.82:
            sites = M.code_sites(text)
            if not sites:
                continue
            # weight operators: organisers' sample is mostly cmp/bool/const/num
            w = {"cmp": 3, "bool": 3, "const": 3, "num": 2, "is": 1.5, "not": 1.5, "in": 0.7,
                 "return": 1, "arith": 0.8, "key": 1}
            site = rng.choices(sites, weights=[w.get(s[0], 1) for s in sites])[0]
            new = M.apply_code_site(text, site)
            op = site[0]
            line = site[1][0]
        else:
            sites = M.line_sites(text)
            if not sites:
                continue
            site = rng.choice(sites)
            new = M.apply_line_site(text, site)
            op = site[0]
            line = site[1]
        if new == text or not M.compiles(new, p):
            continue
        return [("write", p, new)], f"{op} at {p}:{line}", op
    return None


def edit_fixture(rng, path=None):
    files = fixture_files()
    weights = [6.0 if "/optimizer/" in f and "tpc-" not in f else 1.0 for f in files]
    for _ in range(50):
        p = path or rng.choices(files, weights=weights)[0]
        text = read(p)
        lines = text.splitlines(keepends=True)
        sites = M.fixture_sites(text)
        if not sites:
            continue
        meta_sites = [i for i in sites if lines[i - 1].startswith("#")]
        if meta_sites and rng.random() < 0.15:
            i = rng.choice(meta_sites)
        else:
            i = rng.choice([s for s in sites if s not in meta_sites] or sites)
        old = lines[i - 1].rstrip("\n")
        new_line, op = M.mutate_sql_line(rng, old)
        if new_line == old:
            continue
        lines[i - 1] = new_line + "\n"
        return [("write", p, "".join(lines))], f"{op} at {p}:{i}", op
    return None


def edit_fixture_comment(rng, path=None):
    """'--' comment lines / inline comments in fixtures (some are harmless, some not)."""
    files = fixture_files()
    p = path or rng.choice(files)
    text = read(p)
    lines = text.splitlines(keepends=True)
    sites = M.fixture_sites(text)
    if not sites:
        return None
    i = rng.choice(sites)
    v = rng.random()
    if v < 0.5:
        lines.insert(i - 1, "-- " + rng.choice(M.COMMENT_PHRASES) + "\n")
        op = "fixture-dashcomment"
    elif v < 0.75:
        lines[i - 1] = lines[i - 1].rstrip("\n") + " -- " + rng.choice(M.COMMENT_PHRASES) + "\n"
        op = "fixture-inline-comment"
    else:
        lines.insert(i - 1, "\n")
        op = "fixture-blankline"
    return [("write", p, "".join(lines))], f"{op} at {p}:{i}", op


def edit_comment(rng, path=None):
    files = prod_files()
    for _ in range(50):
        p = path or pick_file(rng, files, AREA_WEIGHTS)
        text = read(p)
        sites, str_lines = M.comment_sites(text)
        if not sites:
            continue
        v = rng.random()
        lines = text.splitlines(keepends=True)
        if v < 0.70:
            i = rng.choice(sites)
            new = M.insert_comment(text, i, rng.choice(M.COMMENT_PHRASES))
            op = "comment-line"
        elif v < 0.80:
            toks = M._tokens(text)
            code_lines = sorted(M._code_line_mask(toks) - str_lines)
            cands = [i for i in code_lines if "#" not in lines[i - 1] and not lines[i - 1].rstrip().endswith("\\")]
            if not cands:
                continue
            i = rng.choice(cands)
            lines[i - 1] = lines[i - 1].rstrip("\n") + "  # " + rng.choice(M.COMMENT_PHRASES) + "\n"
            new = "".join(lines)
            op = "comment-trailing"
        elif v < 0.90:
            # edit inside a docstring (first line of a multi-line string token)
            toks = M._tokens(text)
            docs = [t for t in toks if t.type == M.tokenize.STRING and t.string.startswith(('"""', "'''"))]
            if not docs:
                continue
            t = rng.choice(docs)
            i = t.start[0]
            line = lines[i - 1]
            q = line.find('"""') if '"""' in line else line.find("'''")
            lines[i - 1] = line[: q + 3] + "NOTE: " + line[q + 3:]
            new = "".join(lines)
            op = "docstring-edit"
        else:
            i = rng.choice(sites)
            lines.insert(i - 1, "\n")
            new = "".join(lines)
            op = "blank-line"
        if new == text or not M.compiles(new, p):
            continue
        return [("write", p, new)], f"{op} at {p}:{i}", op
    return None


def edit_comment_in_string(rng, path=None):
    """Trap: a '# comment' line inserted inside a multi-line string literal."""
    files = test_files() if rng.random() < 0.6 else prod_files()
    for _ in range(80):
        p = path or rng.choice(files)
        text = read(p)
        _, str_lines = M.comment_sites(text)
        if not str_lines:
            continue
        i = rng.choice(sorted(str_lines))
        new = M.insert_comment(text, i, rng.choice(M.COMMENT_PHRASES))
        if not M.compiles(new, p):
            continue
        return [("write", p, new)], f"comment-in-string at {p}:{i}", "comment-in-string"
    return None


def edit_test(rng, path=None):
    files = test_files()
    for _ in range(60):
        p = path or rng.choice(files)
        text = read(p)
        lines = text.splitlines(keepends=True)
        v = rng.random()
        toks = M._tokens(text)
        if v < 0.40:
            # change an expectation: corrupt one string literal inside a test method
            strs = [t for t in toks if t.type == M.tokenize.STRING and t.start[0] == t.end[0]
                    and t.start[1] >= 8 and len(t.string) > 5 and not t.string.startswith(("f", "r", "b", "F", "R"))]
            if not strs:
                continue
            t = rng.choice(strs)
            s = t.string
            k = rng.randrange(1, len(s) - 1)
            ch = s[k]
            if ch in "\\'\"{}":
                continue
            repl = ch.swapcase() if ch.isalpha() and ch.swapcase() != ch else (ch + " " if ch != " " else "  ")
            new = M.apply_code_site(text, ("x", t.start, t.end, s[:k] + repl + s[k + 1:]))
            op = "test-expectation"
            i = t.start[0]
        elif v < 0.55:
            # code op inside test code
            sites = M.code_sites(text)
            if not sites:
                continue
            site = rng.choice(sites)
            new = M.apply_code_site(text, site)
            op = "test-codeop"
            i = site[1][0]
        elif v < 0.65:
            # import error at module level
            imps = [k for k, l in enumerate(lines, 1) if l.startswith(("from sqlglot", "import sqlglot", "from tests"))]
            if not imps:
                continue
            i = rng.choice(imps)
            lines.insert(i, rng.choice([
                "from sqlglot.errors import DialectError\n",
                "from sqlglot import exp, parse_one, Dialects, Tokenizer, Unknown\n",
                "from tests.helpers import load_sql_fixture\n",
            ]))
            new = "".join(lines)
            op = "test-import-error"
        elif v < 0.75:
            defs = [k for k, l in enumerate(lines, 1) if re.match(r"^    def test_\w+\(", l)]
            if not defs:
                continue
            i = rng.choice(defs)
            lines[i - 1] = re.sub(r"def (test_\w+)\(", lambda m: f"def {m.group(1)}_v2(", lines[i - 1])
            new = "".join(lines)
            op = "test-rename"
        elif v < 0.82:
            defs = [k for k, l in enumerate(lines, 1) if re.match(r"^    def test_\w+\(", l)]
            if not defs or "import unittest" not in text:
                continue
            i = rng.choice(defs)
            lines.insert(i - 1, '    @unittest.skip("flaky on CI")\n')
            new = "".join(lines)
            op = "test-skip"
        else:
            sites, _ = M.comment_sites(text)
            i = rng.choice(sites)
            new = M.insert_comment(text, i, rng.choice(M.COMMENT_PHRASES))
            op = "test-comment"
        if new == text or not M.compiles(new, p):
            continue
        return [("write", p, new)], f"{op} at {p}:{i}", op
    return None


def edit_helper(rng):
    """Edits to shared test helpers (tests/helpers.py, Validator in test_dialect.py)."""
    for _ in range(50):
        p = rng.choice(["tests/helpers.py", "tests/helpers.py", "tests/dialects/test_dialect.py"])
        text = read(p)
        sites = M.code_sites(text)
        if p.endswith("test_dialect.py"):
            sites = [s for s in sites if s[1][0] < 140]  # the Validator class
        if not sites:
            continue
        site = rng.choice(sites)
        new = M.apply_code_site(text, site)
        if new == text or not M.compiles(new, p):
            continue
        return [("write", p, new)], f"helper-{site[0]} at {p}:{site[1][0]}", "helper"
    return None


SINGLE = {
    "code": edit_code,
    "fixture": edit_fixture,
    "comment": edit_comment,
    "test": edit_test,
}


def edit_multi(rng):
    # organisers' multi: comment(s) + a code/fixture change across 2-3 files
    n = rng.choice([2, 2, 3])
    pool = [("comment", 3), ("code", 3), ("fixture", 3), ("test", 1)]
    changes, descs, ops, used = [], [], [], set()
    tries = 0
    while len(changes) < n and tries < 40:
        tries += 1
        k = rng.choices([p[0] for p in pool], weights=[p[1] for p in pool])[0]
        r = SINGLE[k](rng)
        if not r:
            continue
        ch, d, op = r
        if ch[0][1] in used:
            continue
        used.add(ch[0][1])
        changes += ch
        descs.append(f"[{k}] {d}")
        ops.append(k)
    if len(changes) < 2:
        return None
    return changes, "; ".join(descs), "+".join(sorted(ops))


# ----------------------------------------------------------------- builder


def add_case(set_dir: Path, meta: dict, changes, desc, kind, op, seen_patches: set[str]) -> str | None:
    patch = make_patch(changes)
    if not patch.strip() or patch in seen_patches or len(patch) > MAX_PATCH_BYTES:
        return None
    seen_patches.add(patch)
    salt = 0
    while True:
        cid = case_id(f"{patch}{salt}")
        if cid not in meta and cid not in RESERVED_IDS:
            break
        salt += 1
    files = sorted({c[1] for c in changes} | {c[2] for c in changes if c[0] == "rename"})
    areas = sorted({area_of(f) for f in files})
    (set_dir / "commits").mkdir(parents=True, exist_ok=True)
    (set_dir / "commits" / f"{cid}.patch").write_text(patch)
    meta[cid] = {
        "kind": kind,
        "family": f"{kind}:{op}:{areas[0] if len(areas) == 1 else 'multi'}",
        "op": op,
        "area": areas[0] if len(areas) == 1 else "+".join(a.split(":")[0] for a in areas),
        "files": files,
        "desc": desc,
    }
    return cid


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True)
    ap.add_argument("--seed", type=int, required=True)
    for k, d in [("code", 0), ("fixture", 0), ("fixture-comment", 0), ("comment", 0), ("comment-in-string", 0),
                 ("test", 0), ("helper", 0), ("multi", 0)]:
        ap.add_argument(f"--{k}", type=int, default=d)
    ap.add_argument("--exotic", action="store_true", help="add the exotic families (see exotic.py)")
    a = ap.parse_args(argv)
    ensure_gen()
    rng = random.Random(a.seed)
    set_dir = CASES / a.set
    set_dir.mkdir(parents=True, exist_ok=True)
    meta_path = set_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    seen = {p.read_text() for p in (set_dir / "commits").glob("*.patch")} if (set_dir / "commits").exists() else set()
    plan = [
        ("code", a.code, edit_code),
        ("fixture", a.fixture, edit_fixture),
        ("fixture", a.fixture_comment, edit_fixture_comment),
        ("comment", a.comment, edit_comment),
        ("comment", a.comment_in_string, edit_comment_in_string),
        ("test", a.test, edit_test),
        ("test", a.helper, edit_helper),
        ("multi", a.multi, edit_multi),
    ]
    made = 0
    for kind, n, fn in plan:
        k = 0
        while k < n:
            r = fn(rng)
            if not r:
                continue
            changes, desc, op = r
            if add_case(set_dir, meta, changes, desc, kind, op, seen):
                k += 1
                made += 1
    if a.exotic:
        import exotic
        for changes, desc, kind, op in exotic.cases(rng):
            if add_case(set_dir, meta, changes, desc, kind, op, seen):
                made += 1
    meta_path.write_text(json.dumps(dict(sorted(meta.items())), indent=1) + "\n")
    print(f"{a.set}: +{made} cases, {len(meta)} total", file=sys.stderr)


if __name__ == "__main__":
    main()
