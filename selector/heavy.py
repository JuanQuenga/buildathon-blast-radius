"""Differential replay for local expression edits in pinned per-query code."""
import ast
import contextlib
from dataclasses import dataclass
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

from . import impact, query_slice, token_options, verify, warm_replay
from .patch import parse
from .schedule import plan_cost, schedule

TARGETS = {
    "tests/test_generator.py::TestGenerator::test_generator_memory": "generator",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "ds",
    "tests/test_parser.py::TestParser::test_max_nodes": "parser",
}
SCRIPT = Path(__file__).with_name("heavy_cases.py").resolve()

CHILD = """
import os, resource, runpy, signal, sys
cap = 1800 * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
if "BLAST_PROBE_CPU" in os.environ:
    signal.setitimer(signal.ITIMER_PROF, float(os.environ["BLAST_PROBE_CPU"]))
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


def replay(group, cases, overlay, deadline, path=None, cpu_limit=None):
    # Require an explicit completion token. An early successful process exit
    # from patched code cannot be mistaken for completed assertions.
    if group == "catalog":
        script = SCRIPT.with_name("catalog_check.py")
    elif group == "rules":
        script = SCRIPT.with_name("rule_corpus.py")
    elif group == "bundle":
        script = SCRIPT.with_name("bundle.py")
    elif group in {"generator", "dialects"}:
        script = SCRIPT.with_name("generator_cases.py")
    elif group in {"imported", "scaled", "small", "slice", "corpus", "tokens", "front", "full", "partial"}:
        script = SCRIPT.with_name("stage_check.py")
    else:
        script = SCRIPT
    with tempfile.TemporaryFile() as receipt, tempfile.TemporaryFile() as phase:
        env = dict(os.environ, BLAST_RECEIPT=str(receipt.fileno()), MALLOC_ARENA_MAX="2")
        env["BLAST_PHASE_FD"] = str(phase.fileno())
        if cpu_limit is not None:
            env["BLAST_PROBE_CPU"] = str(cpu_limit)
        if path:
            env["BLAST_CHANGED_MODULE"] = ",".join(p[:-3].replace("/", ".") for p in path.split(","))
        if (group != "catalog" or "sqlglot" not in sys.modules) and warm_replay.ready and warm_replay.compatible(env.get("BLAST_CHANGED_MODULE", "")) and group not in {"ds", "parser", "generator", "dialects"}:
            result = warm_replay.run(script, group, cases, overlay, deadline,
                                   env.get("BLAST_CHANGED_MODULE", ""), receipt, phase, cpu_limit)
            replay.phase = os.pread(phase.fileno(), 16, 0).decode().strip() or group
            return result
        with subprocess.Popen([sys.executable, "-sB", "-c", CHILD, str(script), group, *cases],
                              cwd=overlay, env=env, pass_fds=(receipt.fileno(), phase.fileno()),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL, start_new_session=warm_replay.active_overlay is None) as child:
            try:
                status = child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.kill()
                child.wait()
                replay.phase = os.pread(phase.fileno(), 16, 0).decode().strip() or group
                return "timeout"
        replay.phase = os.pread(phase.fileno(), 16, 0).decode().strip() or group
        receipt.seek(0)
        if status == -signal.SIGPROF:
            return "timeout"
        return "pass" if status == 0 and receipt.read(3) == b"OK" else "failed"


def cpu():
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.process_time() + children.ru_utime + children.ru_stime


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


def method_spans(path, fd, original, source):
    """Prove the module prefix and suffix identical before comparing a single method."""
    removed = [n for h in fd.hunks for n in h.removed_old_lines]
    if not removed:
        return None
    scopes = impact._index()["scopes"].get(path)
    if not scopes:
        return None
    for kind, start, end, name, body in reversed(scopes[1]):
        if kind != "def" or not body <= min(removed) <= max(removed) <= end:
            continue
        old = original.splitlines(keepends=True)
        new = source.splitlines(keepends=True)
        end_new = end + len(new) - len(old)
        if old[:start-1] != new[:start-1] or old[end:] != new[end_new:]:
            return None
        prefix = old[start-1][:len(old[start-1])-len(old[start-1].lstrip())]
        chunks = []
        for lines in (old[start-1:end], new[start-1:end_new]):
            if any(line.strip() and not line.startswith(prefix) for line in lines):
                return None
            chunks.append("".join(line[len(prefix):] if line.startswith(prefix) else line
                                  for line in lines))
        ranges = query_slice.spans(*chunks)
        if ranges:
            return [(lo+start-1, hi+start-1) for lo, hi in ranges]
        return None
    return None


@functools.lru_cache(maxsize=2)
def changed_regions(text):
    changes = verify.apply_patch(text)
    result = {}
    files = {f.path: f for f in parse(text)}
    for path, source in changes.items():
        if not re.fullmatch(r"sqlglot/(?:\w+/)*\w+\.py", path):
            return None
        original = (Path(verify.PRISTINE) / path).read_text()
        removed = [n for h in files[path].hunks for n in h.removed_old_lines]
        scopes = impact._index()["scopes"].get(path)
        if scopes and any(not any(k == "def" and body <= n <= end
                                  for k, _, end, _, body in scopes[1]) for n in removed):
            return None
        ranges = method_spans(path, files[path], original, source)
        if ranges is None:
            ranges = query_slice.spans(original, source)
        if ranges is None:
            return None
        result[path] = ranges
    return result


@functools.cache
def probe_profiles(group):
    with gzip.open(f"/app/profile-{group}.gz") as stream:
        return json.load(stream)


@functools.lru_cache(maxsize=48)
def stage_edit(text, group):
    files = parse(text)
    if not files or not all(eligible_file(f, "ds") for f in files):
        return None
    changes = verify.apply_patch(text)
    paths = ",".join(changes)
    regions = changed_regions(text)
    if group == "generator":
        if regions:
            rows = probe_profiles("generator")
            if not query_slice.reached(rows["generator-setup"], regions):
                dialects = [k[4:] for k, v in rows.items() if k.startswith("gen:") and query_slice.reached(v, regions)]
                return "dialects", paths, changes, dialects
        return "generator", paths, changes, []
    if group == "parser":
        if regions and not any(query_slice.reached(profile("parser")[k]["lines"], regions) for k in ("parser:setup", "parser:2")):
            return "small", paths, changes, []
        if not all(eligible_file(f, group) for f in files):
            return None
        return "scaled", paths, changes, []
    if len(files) == 1 and regions is None and token_options.corpus_edit(paths, (Path(verify.PRISTINE) / paths).read_text(), changes[paths]):
        return "tokens", paths, changes, []
    if paths == "sqlglot/tokens.py" and query_slice.keyword_edit((Path(verify.PRISTINE) / paths).read_text(), changes[paths]):
        return "tokens", paths, changes, []
    selection = query_slice.query_cases(regions) if regions else None
    if selection is not None and not selection["active"]:
        if local_edit(text):
            return "unreached", paths, changes, []
        selection["execute"] = []
        return "slice", paths, changes, selection
    if regions and all(p == "sqlglot/parser.py" or p.startswith("sqlglot/parsers/") for p in regions):
        outside = json.loads(Path("/app/parse-corpus.pkl.json").read_text())
        if not query_slice.reached(outside, regions):
            rows = probe_profiles("parser")
            # A corpus input can warm shared parser caches. Replay the complete
            # corpus when any input reaches the edit, preserving input order.
            if not any(query_slice.reached(v, regions) for v in rows.values()):
                return "imported", paths, changes, []
            return "corpus", paths, changes, []
    if selection is not None and all(p.startswith("sqlglot/optimizer/") for p in regions):
        from . import rule_corpus
        # Include every enclosing method and its decorators. The complete AST
        # check above has already confined each edit to existing method bodies.
        tails = downstream()
        def tail_hit(path, spans):
            scopes = impact._index()["scopes"].get(path)
            if not scopes:
                return True
            affected = [(start, end) for kind, start, end, _, body in scopes[1]
                        if kind == "def" and any(body <= lo <= hi <= end for lo, hi in spans)]
            if not affected:
                return True
            return any(p == path and any(lo <= line <= hi for lo, hi in affected)
                       for row in tails for p, line in row["calls"])
        if not any(tail_hit(path, spans) for path, spans in regions.items()):
            indices = rule_corpus.select(regions)
            if indices is not None:
                return "rules", paths, changes, indices
    if selection is not None and not selection["worker"]:
        selection["execute"] = []
        return "slice", paths, changes, selection
    cases = sorted(
        set().union(*(tail_cases(f.path, changes[f.path], f) for f in files))
    )
    if selection is not None:
        selection["execute"] = sorted(set(cases).intersection(selection["worker"]))
        return "slice", paths, changes, selection
    return ("partial" if cases else "front"), paths, changes, cases


@dataclass
class Probe:
    patch: object
    group: str
    number: int | None
    mode: str
    paths: str
    cases: object
    proposed: tuple
    gain: float
    probability: float
    estimate: float

    @property
    def priority(self):
        return self.gain * self.probability / self.estimate


def economics(mode, cases):
    # Smoothed rates from tuning, pooled by verification mechanism.
    if mode == "rules":
        from .rule_corpus import estimate
        return 0.53, estimate(cases)
    if mode == "slice":
        exe = cases["execute"]
        cost = 0.18 + 0.010 * len(cases["active"]) + 0.038 * len(cases["worker"])
        return (0.32 if exe else 0.74), cost + 0.1 * len(exe)
    if mode in {"partial", "front"}:
        return 0.60, 4.0 + 0.11 * len(cases)
    if mode == "dialects":
        return 0.79, 0.22 + 0.025 * len(cases)
    return {
        "unreached": (0.99, 0.005), "imported": (0.90, 0.09),
        "tokens": (0.60, 0.22), "corpus": (0.86, 0.46),
        "small": (0.75, 0.29), "scaled": (0.67, 0.61),
        "generator": (0.28, 1.35),
    }[mode]


def options(patch, catalog, targets, dur):
    if patch.full:
        if not patch.alt:
            return
        req, pl = patch.alt
        workers = schedule(req, pl, dur)
        full = plan_cost(schedule(set(dur), set(), dur), dur)
        gain = (full - plan_cost(workers, dur)) / 1000
        # Even if the alternative fills every worker, collection may admit
        # a subsequent individual proof for an expensive test.
        yield Probe(patch, "collection", None, "catalog", "", [],
                    (req, pl, workers), max(gain, 5), 0.65, 1.35)
        return
    if not patch.req:
        return
    for number, group in targets:
        if number not in patch.req:
            continue
        req, pl = patch.req - {number}, patch.pl - {number}
        workers = schedule(req, pl, dur) if req else []
        gain = (plan_cost(patch.workers, dur) - plan_cost(workers, dur)) / 1000
        if gain <= 0 or number in set().union(*workers):
            continue
        edit = stage_edit(patch.text, group)
        if edit:
            mode, paths, changes, cases = edit
            probability, estimate = economics(mode, cases)
            yield Probe(patch, group, number, mode, paths, cases, (req, pl, workers),
                        gain, probability, estimate)
    if len(patch.workers) != 1:
        return
    required = patch.req | (patch.pl & set(patch.workers[0]))
    load = sum(dur[n] for n in required) / 1000
    if len(required) > 12 or load > 0.65:
        return
    files = parse(patch.text)
    if not files or not all(eligible_file(f, "ds") for f in files):
        return
    cases = [catalog[n] for n in sorted(required)]
    setup_cost = 0.0
    for node in cases:
        parts = node.split("::")
        if len(parts) != 3:
            return
        path, cls, _ = parts
        scopes = impact._index()["scopes"].get(path)
        if scopes and any(name == cls + ".setUpClass" for _, _, _, name, _ in scopes[1]):
            setup_cost = 0.75
            break
    yield Probe(patch, "bundle", None, "bundle", ",".join(f.path for f in files),
                cases, (set(), set(), []), plan_cost(patch.workers, dur) / 1000,
                0.30, setup_cost + 0.16 + 0.55 * load)


def run(patches, catalog, dur, t0):
    from .probe_session import PatchSession
    started = cpu()
    stop = min(t0 + 105, time.monotonic() + 53)
    base = max(0.40, len(patches) * 0.065)
    targets = [(t["number"], TARGETS[t["id"]]) for t in catalog if t["id"] in TARGETS]
    ids = {t["number"]: t["id"] for t in catalog}
    saved = 0.0
    successful_cpu = 0.0
    paid = 0
    queue = []
    attempted = set()
    bundle_wins = bundle_tries = 0
    session = None

    def remaining():
        credit = 0.35 * saved if paid >= 2 else 0.0
        budget = min(max(2.85, 0.07125 * len(patches)), base + successful_cpu + credit)
        return min(stop - time.monotonic(), budget - (cpu() - started)
                   - (session.charged if session else 0))

    def enqueue(patch):
        try:
            for probe in options(patch, ids, targets, dur):
                if probe.priority >= 1.25 and (patch, probe.group) not in attempted:
                    queue.append(probe)
        except Exception:
            return

    for patch in patches:
        if remaining() < 0.08:
            break
        enqueue(patch)
    # A dense batch of cheap proofs can have substantially more removable
    # work than the practice batch. Bound this initial investment separately.
    best_gain = {}
    for probe in queue:
        if probe.estimate <= 1.4 and probe.group not in {"bundle", "collection"}:
            value = probe.gain * probe.probability - probe.estimate
            best_gain[probe.patch] = max(best_gain.get(probe.patch, 0), value)
    if any(probe.group == "collection" and probe.gain >= 10 for probe in queue):
        base = max(base, 2.65)
    base += min(4.0, 0.045 * max(0.0, sum(best_gain.values()) - 60.0))
    print("probe-queue", len(queue), f"{cpu()-started:.4f}", file=sys.stderr)
    active = None
    with contextlib.ExitStack() as stack:
        while queue and remaining() > 0.05:
            queue.sort(key=lambda p: p.priority)
            probe = queue.pop()
            if probe.group == "bundle":
                # Start with three pseudocounts. As outcomes arrive, expensive
                # worker checks must still pay for their observed failure rate.
                probability = (0.9 + bundle_wins) / (3 + bundle_tries)
                if probability * probe.gain < 1.25 * probe.estimate:
                    continue
            patch = probe.patch
            if probe.estimate > 1.5 * remaining() and probe.mode != "unreached":
                continue
            began = cpu() + (session.charged if session else 0)
            phase = probe.mode
            attempted.add((patch, probe.group))
            try:
                if active is not patch:
                    stack.close()
                    session = None
                    active = None
                    work = stack.enter_context(tempfile.TemporaryDirectory())
                    changes = verify.apply_patch(patch.text)
                    verify.build_overlay(work, changes)
                    active = patch
                    modules = ",".join(p[:-3].replace("/", ".") for p in changes)
                    if (probe.group != "collection" and any(p.patch is patch for p in queue)
                            and warm_replay.compatible(modules) and changed_regions(patch.text)):
                        session = PatchSession(work, probe.paths)
                        stack.callback(session.close)
                if probe.mode == "bundle" and session is None:
                    warm_replay.prepare_core()
                if probe.mode in {"catalog", "front", "partial"} and not warm_replay.ready:
                    warm_replay.prepare()
                cpu_limit = max(0.02, min(remaining(), max(0.50, 1.8 * probe.estimate + 0.20)))
                deadline = min(stop, time.monotonic() + 3 * cpu_limit + 0.3)
                args = ([json.dumps(probe.cases)] if probe.mode in
                        {"partial", "slice", "rules", "dialects", "bundle"} else [])
                if probe.mode == "unreached":
                    result = "pass"
                elif session:
                    result, phase = session.check(probe.mode, args, deadline, cpu_limit)
                else:
                    replay.phase = phase
                    result = replay(probe.mode, args, work, deadline, probe.paths, cpu_limit)
                    phase = replay.phase
                if result == "failed" and probe.mode == "rules":
                    # Isolated rule replay can diverge before the assertion.
                    # Trace every affected query through its downstream stages.
                    selection = query_slice.query_cases(changed_regions(patch.text))
                    if (selection and len(selection["worker"]) <= 5
                            and time.monotonic() < deadline and remaining() > 0.25):
                        selection["execute"] = []
                        args = [json.dumps(selection)]
                        spent = cpu() + (session.charged if session else 0) - began
                        left = min(cpu_limit - spent, remaining())
                        if left > 0.20:
                            if session:
                                result, phase = session.check("slice", args, deadline, left)
                            else:
                                result = replay("slice", args, work, deadline, probe.paths, left)
                                phase = replay.phase
                if result == "pass":
                    if probe.mode == "catalog":
                        missing = json.loads((Path(work) / ".blast-missing.json").read_text())
                        if not isinstance(missing, list) or any(type(n) is not int or n not in dur for n in missing):
                            raise ValueError("invalid collection result")
                        req, pl = patch.alt
                        req = req.union(missing)
                        patch.req, patch.pl, patch.workers = req, pl - req, schedule(req, pl - req, dur)
                    else:
                        patch.req, patch.pl, patch.workers = probe.proposed
                    patch.full = False
                    if probe.group != "collection":
                        saved += probe.gain
                        paid += int(probe.group != "bundle")
                    queue[:] = [p for p in queue if p.patch is not patch]
                    enqueue(patch)
            except Exception as exc:
                result = type(exc).__name__
            if probe.group == "bundle":
                bundle_tries += 1
                bundle_wins += result == "pass"
            cost = cpu() + (session.charged if session else 0) - began
            if result == "pass" and probe.group not in {"bundle", "collection"}:
                successful_cpu += min(max(0, cost), 0.4 * probe.gain)
            print("heavy", patch.commit, probe.group, result, probe.mode, f"{cost:.4f}",
                  round(1000 * probe.gain) if result == "pass" and probe.group != "collection" else 0,
                  phase, f"{probe.probability:.2f}", file=sys.stderr)
    print("probe-cpu", f"{cpu()-started:.4f}", file=sys.stderr)


def bounded_run(patches, catalog, dur, t0):
    """Keep static plans in the parent until the verifier returns a complete batch."""
    import multiprocessing

    # Reserve at least a minute for fallback and output on both contest limits.
    allowance = min(180, 3 * max(40, len(patches)))
    deadline = t0 + allowance
    if time.monotonic() >= deadline:
        return
    with tempfile.TemporaryFile(mode="w+") as result:
        def work():
            limit = 1800 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            run(patches, catalog, dur, t0)
            json.dump([p.workers for p in patches], result)
            result.flush()

        proc = multiprocessing.get_context("fork").Process(target=work)
        proc.start()
        try:
            proc.join(max(0, deadline - time.monotonic()))
        finally:
            if proc.is_alive():
                proc.kill()
                proc.join()
        if proc.exitcode != 0 or result.tell() > 2_000_000:
            print("heavy batch fallback", proc.exitcode, file=sys.stderr)
            return
        result.seek(0)
        plans = json.load(result)
        if len(plans) != len(patches):
            return
        for patch, workers in zip(patches, plans):
            patch.workers = workers
