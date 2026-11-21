"""Bake the runtime maps for selector/impact.py from a traced run.

    python tools/build_maps.py --trace TRACE_DIR --src CHECKOUT --out selector/maps

Inputs come only from the pinned checkout (per-test coverage, fixture opens,
class instantiations, collection imports) - never from labelled answers.

Output (all stdlib-loadable):
  maps/index.pkl.xz   tests, sets, per-file line->set ids, opens, families, ...
  maps/src.pkl.xz     pinned sources of sqlglot/**.py and tests/**.py plus the
                      list of every file of the checkout (to diff against)
"""

from __future__ import annotations

import argparse
import glob
import json
import lzma
import os
import pickle
import subprocess
from collections import defaultdict
from pathlib import Path

IMPORT_NOISE_DIRS = ("sqlglot", "tests")  # dirs listed by the import system


def static_structure(text: str):
    """scopes: sorted (kind, start, end, qualname, body_start) for every
    def/class; spans: (start, end) of multi-line simple statements and
    multi-line compound headers."""
    import ast

    tree = ast.parse(text)
    scopes, spans = [], []
    compound = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
                ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Match)

    def visit(body, prefix):
        for s in body:
            start = min([d.lineno for d in getattr(s, "decorator_list", [])] + [s.lineno])
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}{s.name}"
                kind = "class" if isinstance(s, ast.ClassDef) else "def"
                scopes.append((kind, start, s.end_lineno, q, s.body[0].lineno))
                if s.body[0].lineno - 1 > start:
                    spans.append((start, s.body[0].lineno - 1))
                visit(s.body, q + "." if kind == "class" else q + ".<locals>.")
                continue
            if isinstance(s, compound):
                hdr_end = s.body[0].lineno - 1 if s.body and s.body[0].lineno > s.lineno else s.lineno
                if hdr_end > start:
                    spans.append((start, hdr_end))
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(s, field, []) or [], prefix)
                for h in getattr(s, "handlers", []) or []:
                    visit(h.body, prefix)
                for c in getattr(s, "cases", []) or []:
                    visit(c.body, prefix)
                continue
            if (s.end_lineno or s.lineno) > s.lineno:
                spans.append((s.lineno, s.end_lineno))

    visit(tree.body, "")
    scopes.sort(key=lambda x: (x[1], -x[2]))
    spans.sort()
    return scopes, spans


def module_imports(bundle: dict) -> dict:
    """module -> modules it imports while being imported (module level and
    class bodies; not function bodies, not ``if TYPE_CHECKING``)."""
    import ast

    def modname(f):
        p = f[:-3].replace("/", ".")
        return p[: -len(".__init__")] if p.endswith(".__init__") else p

    out = {}
    for f, text in bundle.items():
        me = modname(f)
        pkg = me.split(".") if f.endswith("__init__.py") else me.split(".")[:-1]
        deps = set()

        def visit(body):
            for s in body:
                if isinstance(s, ast.If) and "TYPE_CHECKING" in ast.dump(s.test):
                    visit(s.orelse)
                    continue
                if isinstance(s, ast.Import):
                    deps.update(a.name for a in s.names)
                elif isinstance(s, ast.ImportFrom):
                    base = s.module or ""
                    if s.level:
                        p = pkg[: len(pkg) - (s.level - 1)] if s.level > 1 else pkg
                        base = ".".join(p + ([base] if base else []))
                    deps.add(base)
                    deps.update(f"{base}.{a.name}" for a in s.names)
                elif isinstance(s, (ast.If, ast.Try, ast.With)):
                    visit(s.body)
                    visit(getattr(s, "orelse", []))
                    for h in getattr(s, "handlers", []):
                        visit(h.body)
                    visit(getattr(s, "finalbody", []))
                elif isinstance(s, ast.ClassDef):
                    visit(s.body)

        visit(ast.parse(text).body)
        out[me] = sorted(d for d in deps if d.startswith(("sqlglot", "tests")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True, action="append",
                    help="trace dir (repeatable: runs are unioned)")
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ns", help="output of tools/namespaces.py")
    a = ap.parse_args()
    src = Path(a.src).resolve()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    by_ctx: dict[str, dict] = {}
    collects = []
    for tdir in a.trace:
        for f in sorted(glob.glob(os.path.join(tdir, "*.json"))):
            d = json.load(open(f))
            if d["context"] == "collect":
                collects.append(d)
                continue
            prev = by_ctx.get(d["context"])
            if prev is None:
                by_ctx[d["context"]] = d
                continue
            # union of runs (fork/cold + sequential/warm, several hash seeds)
            for path, lines in d["lines"].items():
                prev["lines"][path] = sorted(set(prev["lines"].get(path, [])) | set(lines))
            for k in ("opens", "listdirs", "classes"):
                prev[k] = sorted(set(prev.get(k, [])) | set(d.get(k, [])))
            prev.setdefault("mro", {}).update(d.get("mro", {}))
            for w, o in d.get("outcome", {}).items():
                if o != "passed":
                    prev.setdefault("outcome", {})[w + "@" + tdir] = o
    tests_data = sorted(by_ctx.values(), key=lambda d: d["context"])
    tests = [d["context"] for d in tests_data]
    tix = {t: i for i, t in enumerate(tests)}
    bad = [d["context"] for d in tests_data
           if any(v != "passed" for v in d.get("outcome", {}).values())]
    print(f"{len(tests)} traced tests, {len(bad)} not passing on the clean pin: {bad[:5]}")

    # line coverage, collection expanded to every test collected with it
    cov: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for d in tests_data:
        bit = 1 << tix[d["context"]]
        for path, lines in d["lines"].items():
            fc = cov[path]
            for ln in lines:
                fc[ln] |= bit
    collect_mods: dict[str, set] = defaultdict(set)  # test file -> files run at import
    for c in collects:
        bits = 0
        for it in c["items"]:
            if it in tix:
                bits |= 1 << tix[it]
        files = {it.split("::")[0] for it in c["items"]}
        for path, lines in c["lines"].items():
            fc = cov[path]
            for ln in lines:
                fc[ln] |= bits
            for tf in files:
                collect_mods[tf].add(path)

    # dedupe bitmaps
    set_ids: dict[int, int] = {}
    sets: list[int] = []

    def sid(b: int) -> int:
        i = set_ids.get(b)
        if i is None:
            i = set_ids[b] = len(sets)
            sets.append(b)
        return i

    cov_ids = {p: {ln: sid(b) for ln, b in sorted(fc.items())} for p, fc in sorted(cov.items())}

    opens: dict[str, int] = defaultdict(int)
    dirs: dict[str, int] = defaultdict(int)
    families: dict[str, int] = defaultdict(int)
    for d in tests_data:
        bit = 1 << tix[d["context"]]
        for o in d["opens"]:
            opens[o] |= bit
        for o in d["listdirs"]:
            dirs[o] |= bit
        for cls, mro in d.get("mro", {}).items():
            for b in mro:
                families[b] |= bit
        for b in d.get("test_mro", []):
            families[b] |= bit

    index = {
        "version": 1,
        "pin": subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip() or None,
        "tests": tests,
        "not_passing": bad,
        "sets": sets,
        "cov": cov_ids,
        "opens": {k: sid(v) for k, v in sorted(opens.items())},
        "listdirs": {k: sid(v) for k, v in sorted(dirs.items())},
        "families": {k: sid(v) for k, v in sorted(families.items())},
        "collect_mods": {k: sorted(v) for k, v in sorted(collect_mods.items())},
    }
    # pinned sources + static structure
    files = subprocess.run(["git", "-C", str(src), "ls-files"], capture_output=True,
                           text=True).stdout.split()
    if not files:
        files = [str(p.relative_to(src)) for p in src.rglob("*") if p.is_file()]
    bundle = {}
    for f in files:
        if f.endswith(".py") and (f.startswith("sqlglot/") or f.startswith("tests/")):
            bundle[f] = (src / f).read_text(encoding="utf-8")
    config = {f: (src / f).read_text(encoding="utf-8") for f in ("pyproject.toml", "setup.cfg")
              if (src / f).exists()}
    scopes, spans = {}, {}
    for f, text in bundle.items():
        sc, sp = static_structure(text)
        scopes[f] = ([x[1] for x in sc], sc)
        spans[f] = ([x[0] for x in sp], [x[1] for x in sp])
    index["imports"] = module_imports(bundle)
    index["scopes"] = scopes
    index["spans"] = spans
    if a.ns:
        ns = json.load(open(a.ns))
        index["ns_mod"] = {k: frozenset(v) for k, v in ns["ns_mod"].items()}
        index["ns_kind"] = ns["ns_kind"]
        index["ns_cls"] = {k: frozenset(v) for k, v in ns["ns_cls"].items()}
        index["importable"] = frozenset(ns["importable"])
        index["mro"] = ns.get("mro", {})

    blob = lzma.compress(pickle.dumps(index, protocol=5), preset=9 | lzma.PRESET_EXTREME)
    (out / "index.pkl.xz").write_bytes(blob)
    print(f"index: {len(sets)} unique sets, {sum(len(v) for v in cov_ids.values())} lines, "
          f"{len(blob) / 1e6:.2f} MB")

    sblob = lzma.compress(pickle.dumps({"files": sorted(files), "src": bundle, "config": config}, protocol=5),
                          preset=9 | lzma.PRESET_EXTREME)
    (out / "src.pkl.xz").write_bytes(sblob)
    print(f"src: {len(bundle)} py files of {len(files)}, {len(sblob) / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
