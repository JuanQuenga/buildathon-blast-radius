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
import os, resource, runpy, sys
cap = 1800 * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
module = os.environ.get("BLAST_CHANGED_MODULE")
if module:
    import importlib
    sys.path.insert(0, os.getcwd())
    for name in module.split(","):
        importlib.import_module(name)
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
        env = dict(os.environ, BLAST_RECEIPT=str(receipt.fileno()), MALLOC_ARENA_MAX="2")
        if path:
            env["BLAST_CHANGED_MODULE"] = ",".join(p[:-3].replace("/", ".") for p in path.split(","))
        with subprocess.Popen([sys.executable, "-sB", "-c", CHILD, str(SCRIPT.with_name("staged.py") if group in {"scaled", "front", "full", "partial"} else SCRIPT), group, *cases],
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


def narrow_pass(patches, catalog, dur, t0, start_cpu):
    stop = min(time.monotonic()+60, t0+120)
    targets = {t["number"]: TARGETS[t["id"]] for t in catalog if t["id"] in TARGETS}
    for patch in patches:
        if time.monotonic() >= stop or cpu() - start_cpu > 48:
            break
        if patch.full and patch.alt:
            try:
                collect(patch, dur, min(stop, time.monotonic() + (70-cpu()+start_cpu)/2))
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
                    outcome = replay(group, cases, work, min(stop, time.monotonic()+min(10, (70-cpu()+start_cpu)/2)), path)
                if outcome == "pass":
                    patch.req, patch.pl, patch.workers = req, pl, workers
            except Exception as exc:
                outcome = type(exc).__name__
            finally:
                print(
                    "heavy",
                    patch.commit,
                    group,
                    outcome,
                    count,
                    f"{cpu() - began:.4f}",
                    before - plan_cost(patch.workers, dur),
                    file=sys.stderr,
                )


@functools.cache
def downstream():
    data = json.loads(Path("/app/staged.json").read_text())
    return data["tail"]


def tail_cases(path, source, fd):
    rows = downstream()
    all_cases = list(range(len(rows)))
    # Restrict method-body slicing to transformations producing ASTs, SQL,
    # or executor values. Constructors, imports and class decorators can
    # change the state handed to the execution phase.
    if not re.fullmatch(
        r"sqlglot/(parser|generator|(?:parsers|generators|optimizer|executor)/\w+)\.py",
        path,
    ):
        return all_cases
    lines = {n for h in fd.hunks for n in h.removed_old_lines}
    if not lines or any(not h.old_len or not h.new_len for h in fd.hunks):
        return all_cases
    before = (Path(verify.PRISTINE) / path).read_text()
    old = ast.parse(before)
    for fn in ast.walk(old):
        if (
            not isinstance(fn, ast.FunctionDef)
            or not fn.body
            or fn.name.startswith("__")
        ):
            continue
        if fn.body[0].lineno > min(lines) or max(lines) > fn.end_lineno:
            continue
        start, end = fn.body[0].lineno - 1, fn.end_lineno
        left, right = before.splitlines(), source.splitlines()
        new_end = end + len(right) - len(left)
        if left[:start] != right[:start] or left[end:] != right[new_end:]:
            return all_cases
        new = ast.parse(source)
        updated = next(
            (
                f
                for f in ast.walk(new)
                if isinstance(f, ast.FunctionDef)
                and f.name == fn.name
                and f.lineno == fn.lineno
            ),
            None,
        )
        if updated is None:
            return all_cases
        updated.body = fn.body
        if ast.dump(old) != ast.dump(new):
            return all_cases
        # Methods nested inside this one may be returned as callbacks.
        # Include their first lines as well as decorator first lines.
        first = min([fn.lineno, *(x.lineno for x in fn.decorator_list)])
        if path.startswith("sqlglot/executor/") and not fn.name.startswith(
            ("scan", "join", "aggregate", "sort", "execute", "generate")
        ):
            return all_cases
        return [
            i
            for i, row in enumerate(rows)
            if any(p == path and first <= n <= end for p, n in row["calls"])
        ]
    # A parser dispatch table can influence serialized expressions. The rest
    # of the pipeline receives those expressions by value. Keep every query
    # whose execution reaches the parser module or reads the table by name.
    if path == "sqlglot/parser.py" or path.startswith("sqlglot/parsers/"):
        for cls in old.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for stmt in cls.body:
                if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                    continue
                if not stmt.lineno <= min(lines) <= max(lines) <= stmt.end_lineno:
                    continue
                targets = (
                    stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                )
                if not all(isinstance(t, ast.Name) for t in targets):
                    continue
                ids = {t.id for t in targets}
                # Check the patched module structurally outside the edited value.
                new = ast.parse(source)
                other = next(
                    (
                        c
                        for c in new.body
                        if isinstance(c, ast.ClassDef) and c.name == cls.name
                    ),
                    None,
                )
                if not other or len(other.body) != len(cls.body):
                    return all_cases
                changed = other.body[cls.body.index(stmt)]
                if type(changed) != type(stmt):
                    return all_cases
                changed.value = stmt.value
                if ast.dump(new) != ast.dump(old):
                    return all_cases
                return [
                    i
                    for i, row in enumerate(rows)
                    if path in row["paths"] or ids.intersection(row["names"])
                ]
    return all_cases


def eligible_file(f, group):
    path = f.path
    if (
        not re.fullmatch(r"sqlglot/(?:\w+/)*\w+\.py", path)
        or f.paths != {path}
        or f.is_new
        or f.is_deleted
        or f.is_rename
        or f.mode_change
        or f.is_binary
        or not f.hunks
    ):
        return None
    lines = [line[1:] for h in f.hunks for line in h.lines if line[:1] in {"+", "-"}]
    delta = "\n".join(lines)
    if path in {
        "sqlglot/dialects/dialect.py",
        "sqlglot/dialects/__init__.py",
        "sqlglot/dialects/doris.py",
    } or re.search(
        r"\b(import|global|nonlocal|setattr|delattr|exec|eval|_classes|DIALECTS)\b|__\w+__|cache|registr",
        delta,
        re.I,
    ):
        return None
    if group == "parser":
        # Keep changes reached at full size in the parser/tokenizer implementation.
        # Module and class initializers are absent from call coverage, so also
        # guard thresholds and changes to control flow at either diff coordinate.
        if re.search(
            r"\b(?:\d[\d_]*|for|while|range|len|limit|depth|recursion|max_nodes)\b",
            delta,
            re.I,
        ):
            return None
        reached = profile("parser")["parser:2"]["lines"].get(path, [])
        if any(
            h.old_start <= n < h.old_start + max(h.old_len, 1)
            for n in reached
            for h in f.hunks
        ):
            return None
    return True


def stage_edit(text, group):
    files = parse(text)
    if not files or not all(eligible_file(f, group) for f in files):
        return None
    changes = verify.apply_patch(text)
    paths = ",".join(changes)
    if group == "parser":
        return "scaled", paths, changes, []
    cases = sorted(
        set().union(*(tail_cases(f.path, changes[f.path], f) for f in files))
    )
    return ("partial" if cases else "front"), paths, changes, cases


def run(patches, catalog, dur, t0):
    started = time.monotonic()
    start_cpu = cpu()
    stop = min(t0 + 120, started + 70)
    narrow_pass(patches, catalog, dur, t0, start_cpu)
    queue = []
    targets = [
        (test["number"], TARGETS[test["id"]])
        for test in catalog
        if test["id"] in TARGETS
    ]
    for patch in patches:
        if time.monotonic() >= stop or cpu() - start_cpu >= 68:
            break
        if patch.full or not patch.req:
            continue
        for number, group in targets:
            if number not in patch.req:
                continue
            try:
                edit = stage_edit(patch.text, group)
                if not edit:
                    continue
                mode, path, changes, cases = edit
                estimate = (
                    0.9
                    if mode == "scaled"
                    else 6
                    + sum(
                        profile("ds")[f"ds:{i}"]["phases"].get("execute", 0)
                        for i in cases
                    )
                )
                req = patch.req - {number}
                workers = schedule(req, patch.pl - {number}, dur) if req else []
                gain = plan_cost(patch.workers, dur) - plan_cost(workers, dur)
                if number not in set().union(*workers) and gain > estimate * 1000:
                    queue.append(
                        (
                            gain / estimate,
                            patch,
                            group,
                            number,
                            mode,
                            path,
                            changes,
                            cases,
                        )
                    )
            except Exception:
                continue
    for _, patch, group, number, mode, path, changes, cases in sorted(
        queue, key=lambda x: -x[0]
    ):
        remaining = min(stop - time.monotonic(), (70 - (cpu() - start_cpu)) / 2)
        if remaining <= 0:
            break
        began = cpu()
        before = plan_cost(patch.workers, dur)
        try:
            req, pl = patch.req - {number}, patch.pl - {number}
            workers = schedule(req, pl, dur) if req else []
            gain = before - plan_cost(workers, dur)
            if gain <= 0 or number in set().union(*workers):
                continue
            with tempfile.TemporaryDirectory() as work:
                verify.build_overlay(work, changes)
                result = replay(
                    mode,
                    [json.dumps(cases)] if mode == "partial" else [],
                    work,
                    min(stop, time.monotonic() + min(remaining, 30)),
                    path,
                )
            if result == "pass":
                patch.req, patch.pl, patch.workers = req, pl, workers
        except Exception as exc:
            result = type(exc).__name__
        print(
            "heavy",
            patch.commit,
            group,
            result,
            mode,
            f"{cpu() - began:.4f}",
            before - plan_cost(patch.workers, dur),
            file=sys.stderr,
        )
