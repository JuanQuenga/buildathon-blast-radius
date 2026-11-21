"""Practice-only ablations. These verdicts intentionally discard uncertainty."""
import ast
import functools
import os
from pathlib import Path
import sys

from .fixture_fields import load_fields
from .impact import apply_hunks, parse_patch
from .schedule import plan_cost, schedule
from .verify import PRISTINE

MODE = os.environ.get("BLAST_PRACTICE_DIAGNOSTIC", "")
DS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
PARSER = "tests/test_parser.py::TestParser::test_max_nodes"
FIXTURE = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"


@functools.lru_cache(maxsize=128)
def direct(text, node):
    path, cls, method = node.split("::")
    for patch in parse_patch(text):
        paths = {patch.old_path, patch.new_path}
        if path in paths:
            if patch.status != "M" or patch.binary:
                return True
            old = (Path(PRISTINE) / path).read_text()
            new, _ = apply_hunks(old, patch.hunks)
            def body(source):
                for item in ast.parse(source).body:
                    if isinstance(item, ast.ClassDef) and item.name == cls:
                        return tuple(ast.dump(fn) for fn in item.body
                                     if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                                     and fn.name == method)
                return ()
            try:
                if body(old) != body(new):
                    return True
            except SyntaxError:
                return True
        if node == DS and FIXTURE in paths:
            if patch.status != "M" or patch.binary:
                return True
            old = (Path(PRISTINE) / FIXTURE).read_text()
            new, _ = apply_hunks(old, patch.hunks)
            # Meta and expected-only changes are deliberately inconclusive here.
            if ([v[1] for v in load_fields(old, True)] !=
                    [v[1] for v in load_fields(new, True)]):
                return True
    return False


def apply(patches, catalog, dur, evidence):
    if MODE not in {"D1", "D2", "D3", "D4-parser", "D4-full"}:
        raise ValueError("unknown practice diagnostic")
    ids = {r["id"]: r["number"] for r in catalog}
    node = PARSER if MODE == "D4-parser" else DS
    n = ids[node]
    for p in patches:
        base = p.workers
        if base is None:
            base = schedule(set(dur), set(), dur)
        selected = set().union(*base)
        if MODE == "D4-full":
            # Restrict to opaque/collection fallbacks, not naturally large plans.
            if not p.full:
                continue
            files = {f.path for f in parse_patch(p.text)}
            keep = {r["number"] for r in catalog if r["file"] in files}
            if p.alt:
                keep.update(p.alt[0])
            result = schedule(keep, set(), dur, free_riders=False)
            reason = "full-fallback"
        else:
            if n not in selected:
                continue
            try:
                protected = direct(p.text, node)
            except (ValueError, OSError, SyntaxError, IndexError, RuntimeError):
                protected = False
            if protected:
                continue
            reason = evidence.get((p.commit, n), "unattempted")
            if reason == "asserted" or (MODE == "D2" and reason == "unattempted") or (
                    MODE == "D3" and reason != "unattempted"):
                continue
            required = set(dur) if p.full else p.req
            if n not in required:
                result = [[t for t in worker if t != n] for worker in base]
                result = [worker for worker in result if worker]
            else:
                durations = {k: dur[k] for k in selected if k != n}
                result = schedule(required - {n}, p.pl - {n}, durations)
        p.workers = result
        saving = plan_cost(base, dur) - plan_cost(result, dur)
        print("practice-diagnostic", MODE, p.commit, reason, f"{saving / 1000:.3f}", file=sys.stderr)
