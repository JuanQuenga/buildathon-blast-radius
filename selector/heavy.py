"""Differential replay for local expression edits in pinned per-query code."""
import ast
import functools
import gzip
import json
import os
from pathlib import Path
import re
import resource
import signal
import subprocess
import sys
import textwrap
import tempfile
import time

from . import verify
from .patch import parse
from .schedule import plan_cost, schedule

TARGETS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "ds",
    "tests/test_parser.py::TestParser::test_max_nodes": "parser",
}
SCRIPT = Path(__file__).with_name("heavy_cases.py").resolve()

CHILD = """
import os, runpy, sys
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
module = os.environ.get("BLAST_CHANGED_MODULE")
if module:
    import importlib
    sys.path.insert(0, os.getcwd())
    importlib.import_module(module)
os.write(int(os.environ.pop("BLAST_RECEIPT")), b"OK")
"""


@functools.cache
def profile(group):
    with gzip.open(f"/app/pin-cache/{group}-lines.json.gz") as stream:
        rows = json.load(stream)
    expected = {f"{group}:{i}" for i in range(99 if group == "ds" else 4)}
    expected.add(f"{group}:setup")
    if group == "ds":
        expected.add("ds:teardown")
    if rows.keys() != expected:
        raise ValueError("incomplete subcase map")
    return rows


def local_methods(tree, path):
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for fn in cls.body:
            if not isinstance(fn, ast.FunctionDef) or fn.name.startswith("__"):
                continue
            if ((path == "sqlglot/executor/context.py" and cls.name == "Context")
                or (path == "sqlglot/executor/python.py" and cls.name == "PythonExecutor")
                or (cls.name.endswith("Parser") and fn.name.startswith("_parse_"))
                or (cls.name.endswith("Generator") and fn.name.endswith("_sql"))):
                yield fn


@functools.cache
def method_ranges(path):
    with open("/app/pin-cache/heavy-methods.json") as stream:
        return json.load(stream).get(path, [])


def compare_body(old, new, spans, statement=None):
    if isinstance(old, list):
        return (isinstance(new, list) and len(old) == len(new)
                and all(compare_body(a, b, spans, statement) for a, b in zip(old, new)))
    if not isinstance(old, ast.AST):
        return old == new
    if not isinstance(new, ast.AST):
        return False
    if isinstance(old, ast.stmt):
        statement = old
    # Only expressions can change. Treat the entire statement as reached if
    # any of its lines fired, including an unchanged prefix of a short circuit.
    if statement and isinstance(old, ast.expr) and ast.dump(old) != ast.dump(new):
        compatible = (
            isinstance(old, ast.Compare) and isinstance(new, ast.Compare)
            and len(old.ops) == len(new.ops)
            and ast.dump(old.left) == ast.dump(new.left)
            and [ast.dump(v) for v in old.comparators] == [ast.dump(v) for v in new.comparators]
            or isinstance(old, ast.BoolOp) and isinstance(new, ast.BoolOp)
            and [ast.dump(v) for v in old.values] == [ast.dump(v) for v in new.values]
            or isinstance(old, ast.Call) and isinstance(new, ast.Name)
            and any(ast.dump(v) == ast.dump(new) for v in ast.walk(old))
        )
        if compatible:
            spans.append((statement.lineno, statement.end_lineno))
            return True
    return type(old) is type(new) and all(
        compare_body(getattr(old, field), getattr(new, field), spans, statement)
        for field in old._fields
    )


def shared_state(fn):
    for node in ast.walk(fn):
        if isinstance(node, (ast.Global, ast.Nonlocal, ast.Import, ast.ImportFrom)):
            return True
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in {"_classes", "classes", "DIALECTS", "__dict__", "getattr", "setattr",
                        "globals", "locals", "vars"} or "cache" in name.lower() or "registr" in name.lower():
                return True
    return False


def local_edit(text):
    files = parse(text)
    if len(files) != 1 or text.count("diff --git ") != 1:
        return None
    fd = files[0]
    path = fd.path
    if (fd.paths != {path} or fd.is_new or fd.is_deleted or fd.mode_change or fd.is_binary
        or fd.is_rename or not re.fullmatch(
            r"sqlglot/(parser|generator|(?:parsers|generators)/\w+|executor/(?:context|python))\.py", path)):
        return None
    removed = {line for h in fd.hunks for line in h.removed_old_lines}
    added = {line for h in fd.hunks for line in h.added_new_lines}
    if not removed or not added:
        return None
    changes = verify.apply_patch(text)
    pin = (Path(verify.PRISTINE) / path).read_text()
    if any(h.old_len == 0 or h.new_len == 0 for h in fd.hunks):
        return None
    # Compare a complete method and verify that the module outside it is
    # byte-identical. Parsing the full 9,000-line parser twice costs more
    # than running a small parser subcase.
    for start, end, body_start in method_ranges(path):
        if body_start <= min(removed) and max(removed) <= end:
            break
    else:
        return None
    before = pin.splitlines(keepends=True)
    after = changes[path].splitlines(keepends=True)
    end_new = end + len(after) - len(before)
    if (before[:start-1] != after[:start-1] or before[end:] != after[end_new:]
        or not body_start <= min(added) <= max(added) <= end_new):
        return None
    methods = []
    for lines, last in ((before, end), (after, end_new)):
        module = ast.parse(textwrap.dedent("".join(lines[start-1:last])))
        if len(module.body) != 1 or not isinstance(module.body[0], ast.FunctionDef):
            return None
        methods.append(module.body[0])
    old, new = methods
    if shared_state(old) or shared_state(new):
        return None
    old_body, new_body = old.body, new.body
    old.body = new.body = []
    if ast.dump(old) != ast.dump(new):
        return None
    spans = []
    if not compare_body(old_body, new_body, spans) or not spans:
        return None
    span = min(a for a, _ in spans), max(b for _, b in spans)
    return path, (span[0]+start-1, span[1]+start-1), changes


def affected(group, path, span):
    rows = profile(group)
    hits = [key for key, row in rows.items()
            if any(span[0] <= n <= span[1] for n in row["lines"].get(path, []))]
    if any(k.endswith((":setup", ":teardown")) for k in hits):
        return None
    if group == "parser" and "parser:2" in hits:
        return None
    if sum(rows[k]["cpu"] for k in hits) > 7:
        return None
    return hits


def replay(group, cases, overlay, deadline, path=None):
    # Require an explicit completion token. An early successful process exit
    # from patched code cannot be mistaken for completed assertions.
    with tempfile.TemporaryFile() as receipt:
        env = dict(os.environ, BLAST_RECEIPT=str(receipt.fileno()))
        if path:
            env["BLAST_CHANGED_MODULE"] = path[:-3].replace("/", ".")
        with subprocess.Popen([sys.executable, "-sB", "-c", CHILD, str(SCRIPT), group, *cases],
                              cwd=overlay, env=env, pass_fds=(receipt.fileno(),),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL, start_new_session=True) as child:
            try:
                status = child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                return "timeout"
        receipt.seek(0)
        return "pass" if status == 0 and receipt.read(3) == b"OK" else "failed"


def cpu():
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.process_time() + children.ru_utime + children.ru_stime


def collect(patch, dur, stop):
    req, pl = patch.alt
    files = parse(patch.text)
    # Pytest configuration, helpers, registries and file operations stay full.
    if (len(files) != 1 or files[0].old_path != files[0].new_path or not re.fullmatch(
        r"sqlglot/(?:parser|generator|(?:parsers|generators|optimizer|executor)/\w+)\.py",
        files[0].path
    ) or len(req) == len(dur)):
        return
    changes = verify.apply_patch(patch.text)
    path = files[0].path
    # Reject module/class state and decorators. The only difference may be
    # inside an existing function body. Keep all original file readers.
    bodies = []
    for source, touched in (
        ((Path(verify.PRISTINE) / path).read_text(), {n for h in files[0].hunks for n in h.removed_old_lines}),
        (changes[path], {n for h in files[0].hunks for n in h.added_new_lines}),
    ):
        module = ast.parse(source)
        for fn in ast.walk(module):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(fn.lineno <= n <= fn.end_lineno for n in touched) and shared_state(fn):
                    return
                fn.body = []
        bodies.append(ast.dump(module))
    if bodies[0] != bodies[1]:
        return
    workers = schedule(req, pl, dur)
    if plan_cost(workers, dur) >= plan_cost(schedule(set(dur), set(), dur), dur):
        return
    began = cpu()
    with tempfile.TemporaryDirectory() as work:
        verify.build_overlay(work, changes)
        result = replay("collect", [], work, min(stop, time.monotonic()+6))
    print("collection", patch.commit, result, f"{cpu()-began:.4f}", file=sys.stderr)
    if result == "pass":
        patch.full, patch.req, patch.pl, patch.workers = False, req, pl, workers


def run(patches, catalog, dur, t0):
    stop = min(time.monotonic()+60, t0+120)
    targets = {t["number"]: TARGETS[t["id"]] for t in catalog if t["id"] in TARGETS}
    for patch in patches:
        if time.monotonic() >= stop:
            break
        if patch.full and patch.alt:
            try:
                collect(patch, dur, stop)
            except Exception as exc:
                print("collection", patch.commit, type(exc).__name__, file=sys.stderr)
            continue
        if patch.full:
            continue
        scheduled = set().union(*patch.workers)
        for n in sorted(scheduled & targets.keys()):
            group = targets[n]
            began = cpu()
            before = plan_cost(patch.workers, dur)
            outcome = "hazard"
            count = 0
            try:
                edit = local_edit(patch.text)
                if edit is None:
                    continue
                path, span, changes = edit
                cases = affected(group, path, span)
                if cases is None:
                    outcome = "coverage-or-cost"
                    continue
                count = len(cases)
                req, pl = patch.req - {n}, patch.pl - {n}
                workers = schedule(req, pl, dur) if req else []
                if (n in set().union(*workers) or plan_cost(workers, dur) >= before
                    or not req.issubset(set().union(*workers))):
                    outcome = "no-saving"
                    continue
                with tempfile.TemporaryDirectory() as work:
                    verify.build_overlay(work, changes)
                    outcome = replay(group, cases, work, min(stop, time.monotonic()+10), path)
                if outcome == "pass":
                    patch.req, patch.pl, patch.workers = req, pl, workers
            except Exception as exc:
                outcome = type(exc).__name__
            finally:
                print("heavy", patch.commit, group, outcome, count, f"{cpu()-began:.4f}",
                      before-plan_cost(patch.workers, dur), file=sys.stderr)
