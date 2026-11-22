"""Differential replay and warm-fork verification for heavy & cheap tests."""
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
from .runtime_paths import APP_DIR, set_memory_limit
from .schedule import plan_cost, schedule

TARGETS = {
    "tests/test_generator.py::TestGenerator::test_generator_memory": "generator",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "ds",
    "tests/test_parser.py::TestParser::test_max_nodes": "parser",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": "tpch_opt",
    "tests/test_executor.py::TestExecutor::test_execute_tpch": "tpch_exec",
}
SCRIPT = Path(__file__).with_name("heavy_cases.py").resolve()

CHILD = """
import os, resource, runpy, signal, sys
if sys.platform == "linux":
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

_PYTEST_LEVEL_NAMES = frozenset({
    "conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "setup.py",
})
_GEN_LOOP_CORE = frozenset({
    "sqlglot/dialects/dialect.py",
    "sqlglot/generator.py",
    "sqlglot/helper.py",
})
_STDLIB_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+(?:re|math|typing|collections|itertools|functools|datetime|decimal|enum|dataclasses|copy|json|operator|warnings|logging)\b"
    r"|from\s+(?:__future__|typing|collections(?:\.abc)?|itertools|functools|datetime|decimal|enum|dataclasses)\b)"
)
_SQLGLOT_SAFE_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+sqlglot(?:\.(?!dialects\b|executor\b)\w+)*\s+import\b"
    r"|import\s+sqlglot(?:\.(?!dialects\b|executor\b)\w+)*\b)"
)
_DOC_EXTS = (".md", ".rst", ".txt")
_DOC_PREFIXES = (
    "docs/", ".github/", "benchmarks/", "pdoc/", "posts/",
    "sqlglot-integration-tests/", "sqlglotrs/", "sqlglotc/", "scripts/",
)
_DOC_NAMES = frozenset({
    "LICENSE", "Makefile", ".gitignore", ".editorconfig",
    "MANIFEST.in", ".pre-commit-config.yaml", ".gitpod.yml", ".gitmodules", "sqlglot.png",
    ".flake8", "mypy.ini", "ruff.toml", "codecov.yml", "Dockerfile",
})
_PYGEN_TAIL_PATHS = frozenset({
    "sqlglot/generator.py",
    "sqlglot/generators/python.py",
    "sqlglot/dialects/dialect.py",
    "sqlglot/expressions/core.py",
    "sqlglot/expressions/query.py",
    "sqlglot/expressions/datatypes.py",
    "sqlglot/planner.py",
    "sqlglot/schema.py",
    "sqlglot/trie.py",
    "sqlglot/serde.py",
    "sqlglot/helper.py",
})


def _is_doc_path(path):
    if not path or path.startswith(("sqlglot/", "tests/")):
        return False
    base = path.rsplit("/", 1)[-1]
    if base in _PYTEST_LEVEL_NAMES:
        return False
    return path.endswith(_DOC_EXTS) or path.startswith(_DOC_PREFIXES) or path in _DOC_NAMES


@functools.cache
def profile(group):
    with gzip.open(APP_DIR / f"pin-cache/{group}-lines.json.gz") as stream:
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
    with open(APP_DIR / "pin-cache/heavy-methods.json") as stream:
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
    if group == "catalog":
        script = SCRIPT.with_name("catalog_check.py")
    elif group == "fixture-query":
        script = SCRIPT.with_name("fixture_query.py")
    elif group == "rules":
        script = SCRIPT.with_name("rule_corpus.py")
    elif group in {"bundle", "trim"}:
        script = SCRIPT.with_name("bundle.py")
    elif group in {"generator", "dialects"}:
        script = SCRIPT.with_name("generator_cases.py")
    elif group in {"imported", "scaled", "small", "slice", "corpus", "tokens", "front", "full", "partial", "tpch_opt", "tpch_exec"}:
        script = SCRIPT.with_name("stage_check.py")
    else:
        script = SCRIPT
    with tempfile.TemporaryFile() as receipt, tempfile.TemporaryFile() as phase:
        env = dict(os.environ, BLAST_RECEIPT=str(receipt.fileno()), MALLOC_ARENA_MAX="2")
        env["BLAST_PHASE_FD"] = str(phase.fileno())
        if cpu_limit is not None:
            env["BLAST_PROBE_CPU"] = str(cpu_limit)
        if path:
            mods = [impact._module_name(p) for p in path.split(",") if p.endswith(".py") and p.startswith("sqlglot/")]
            if mods:
                env["BLAST_CHANGED_MODULE"] = ",".join(mods)
        changed_mods = env.get("BLAST_CHANGED_MODULE", "")
        can_warm = (
            warm_replay.ready
            and warm_replay.compatible(changed_mods)
            and group not in {"ds", "parser"}
            and (group not in {"generator", "dialects", "catalog"} or warm_replay.active_overlay is None)
        )
        if can_warm:
            result = warm_replay.run(
                script, group, cases, overlay, deadline,
                changed_mods, receipt, phase, cpu_limit,
            )
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
    import marshal
    mpath = APP_DIR / "pin-cache" / "staged.marshal"
    if mpath.is_file():
        return marshal.loads(mpath.read_bytes())["tail"]
    data = json.loads((APP_DIR / "staged.json").read_text())
    return data["tail"]


@functools.cache
def all_tail_paths():
    paths = set()
    for row in downstream():
        paths.update(row["paths"])
    return frozenset(paths)


@functools.cache
def ds_setup_paths():
    # TestExecutor.setUpClass() only invokes sqlglot.executor.table.Table().
    return frozenset({"sqlglot/executor/table.py"})


@functools.cache
def tail_path_queries():
    mapping = {}
    for i, row in enumerate(downstream()):
        for p in row["paths"]:
            mapping.setdefault(p, []).append(i)
    return mapping


@functools.cache
def worker_path_queries():
    """Map sqlglot path -> sorted list of TPC-DS query indices that execute it
    in the worker (optimize + Plan + execute)."""
    mapping = {}
    for i, row in enumerate(query_slice.trace()["worker"]):
        for p in row:
            mapping.setdefault(p, []).append(i)
    return mapping


def _inert_new_class_attrs(path, source, fd=None):
    """Return True if ``source`` differs from the pinned ``path`` ONLY by adding
    new class-level literal constant attribute(s) (with optional docstrings)
    whose names never appear in any pinned source file."""
    if not source or not path.startswith("sqlglot/") or not path.endswith(".py"):
        return False
    if fd is not None:
        # Pure insertion check BEFORE parsing potentially 10k-line files.
        if any(h.removed_old_lines for h in fd.hunks):
            return False
        added_lines = [line[1:].strip() for h in fd.hunks for line in h.lines if line[:1] == "+"]
        if not added_lines or not any("=" in ln for ln in added_lines):
            return False
        if any(
            ln and not ln.startswith(("#", '"""', "'''", '"', "'")) and "=" not in ln
            for ln in added_lines
        ):
            return False
    try:
        old = impact._parse_pinned(path)
        if old is None:
            return False
        new = ast.parse(source)
    except SyntaxError:
        return False
    if len(old.body) != len(new.body):
        return False
    an = impact.Analysis()
    added_any = False
    for a, b in zip(old.body, new.body):
        if ast.dump(a) == ast.dump(b):
            continue
        if not (isinstance(a, ast.ClassDef) and isinstance(b, ast.ClassDef) and a.name == b.name):
            return False
        old_dumps = [ast.dump(s) for s in a.body]
        kept = []
        for s in b.body:
            d = ast.dump(s)
            if d in old_dumps:
                kept.append(s)
                continue
            # Allow attribute docstrings right after an added class constant.
            if (
                isinstance(s, ast.Expr)
                and isinstance(s.value, ast.Constant)
                and isinstance(s.value.value, str)
            ):
                continue
            # Must be a simple literal constant assignment of a brand-new name.
            if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
                name, val = s.targets[0].id, s.value
            elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name) and s.value is not None:
                name, val = s.target.id, s.value
            else:
                return False
            if name.startswith("__") or name.endswith("_sql"):
                return False
            if not (isinstance(val, ast.Constant) and type(val.value) in (bool, int, float, str, type(None))):
                return False
            if impact._name_sites(an, name):
                return False
            added_any = True
        b.body = kept
    return added_any and ast.dump(old) == ast.dump(new)


def _strip_comment(line: str) -> str:
    """Strip a trailing # comment only when there are no quotes before it."""
    if "#" not in line:
        return line
    idx = line.find("#")
    prefix = line[:idx]
    if "'" not in prefix and '"' not in prefix:
        return prefix
    return line


def _has_hazard_token(line: str) -> bool:
    code = _strip_comment(line)
    if not code.strip():
        return False
    # Read-only .__name__ / .__qualname__ attribute accesses are not dunder hooks.
    cleaned = re.sub(r"\.__(?:name|qualname)__\b", "", code)
    return bool(
        re.search(r"\b(global|nonlocal|setattr|delattr|exec|eval|_classes)\b|__\w+__|cache|registr", cleaned, re.I)
        or re.search(r"\bDIALECTS\b", cleaned)
    )


def _no_cross_module_hazards(fd, *, allow_sqlglot_imports=False):
    """True when diff lines have no global/setattr/eval/registry/non-stdlib-import hazards."""
    lines = [line[1:] for h in fd.hunks for line in h.lines if line[:1] in {"+", "-"}]
    for line in lines:
        stripped = line.strip()
        if stripped == "from __future__ import annotations" or stripped.startswith((">>> ", "#")):
            continue
        if _has_hazard_token(line):
            return False
        if re.search(r"\bimport\b", _strip_comment(line)):
            if _STDLIB_IMPORT_RE.match(line):
                continue
            if allow_sqlglot_imports and _SQLGLOT_SAFE_IMPORT_RE.match(line):
                continue
            return False
    return True


def _method_tail_queries(path, source, fd, rows):
    """If every hunk in ``fd`` is strictly confined inside the body of existing
    non-dunder function/method(s) in ``path`` (with unchanged signatures and
    identical code outside those bodies), return the exact union of tail query
    indices whose execution trace entered any of those functions."""
    if source is None or fd.is_new or fd.is_deleted or fd.is_rename or not fd.hunks:
        return None
    before = impact._sources()["src"].get(path)
    if before is None:
        return None
    scopes = impact._index().get("scopes", {}).get(path)
    if not scopes:
        return None
    defs = [
        (start, end, name, body_start)
        for kind, start, end, name, body_start in scopes[1]
        if kind == "def"
    ]
    if not defs:
        return None

    # Map each individual modified line (removed or inserted) to its innermost
    # enclosing non-dunder function so multi-method hunks and pure insertions
    # near function boundaries work cleanly without spilling into context lines.
    def _enclosing(ln):
        cands = [d for d in defs if d[3] <= ln <= d[1]]
        return max(cands, key=lambda x: x[0]) if cands else None

    touched_fns = {}  # (start, end, qual, body_start) -> net line delta
    for h in fd.hunks:
        old_ln = h.old_start
        for raw in h.lines:
            tag = raw[:1]
            if tag == " ":
                old_ln += 1
            elif tag == "-":
                inner = _enclosing(old_ln)
                if inner is None:
                    return None
                touched_fns[inner] = touched_fns.get(inner, 0) - 1
                old_ln += 1
            elif tag == "+":
                inner = _enclosing(old_ln - 1) if old_ln > 1 else None
                if inner is None:
                    inner = _enclosing(old_ln)
                if inner is None:
                    return None
                touched_fns[inner] = touched_fns.get(inner, 0) + 1
    if not touched_fns:
        return None
    for inner in touched_fns:
        simple_name = inner[2].rsplit(".", 1)[-1]
        if simple_name.startswith("__"):
            return None
        if path == "sqlglot/executor/env.py":
            if simple_name in {"null_if_any", "filter_nulls"}:
                return None
        elif path == "sqlglot/executor/table.py" and not simple_name.startswith(
            ("scan", "join", "aggregate", "sort", "execute", "generate")
        ):
            return None
        elif (
            path.startswith("sqlglot/executor/")
            and path not in {"sqlglot/executor/python.py", "sqlglot/executor/context.py"}
            and not simple_name.startswith(
                ("scan", "join", "aggregate", "sort", "execute", "generate")
            )
        ):
            return None

    # Sort non-overlapping enclosing functions; reject nested overlap for simplicity.
    ordered = sorted(touched_fns.items(), key=lambda x: x[0][0])
    for idx in range(1, len(ordered)):
        if ordered[idx][0][0] <= ordered[idx - 1][0][1]:
            return None

    left, right = before.splitlines(), source.splitlines()
    # Verify every byte outside the touched function bodies is identical and
    # every touched function's signature/decorators are AST-identical.
    prev_old = 0
    cum_delta = 0
    for (start, end, _qual, body_start), delta in ordered:
        if left[prev_old : body_start - 1] != right[prev_old + cum_delta : body_start - 1 + cum_delta]:
            return None
        new_start = start - 1 + cum_delta
        new_end = end + cum_delta + delta
        try:
            old_mod = ast.parse(textwrap.dedent("\n".join(left[start - 1 : end])))
            new_mod = ast.parse(textwrap.dedent("\n".join(right[new_start:new_end])))
        except SyntaxError:
            return None
        if len(old_mod.body) != 1 or len(new_mod.body) != 1:
            return None
        old_fn, new_fn = old_mod.body[0], new_mod.body[0]
        if not (isinstance(old_fn, ast.FunctionDef) and isinstance(new_fn, ast.FunctionDef)):
            return None
        new_fn.body = old_fn.body
        if ast.dump(old_fn) != ast.dump(new_fn):
            return None
        prev_old = end
        cum_delta += delta
    if left[prev_old:] != right[prev_old + cum_delta :]:
        return None

    ranges = [(start, end) for (start, end, _, _), _ in ordered]
    return [
        i
        for i, row in enumerate(rows)
        if any(p == path and any(s <= n <= e for s, e in ranges) for p, n in row["calls"])
    ]


def _is_simple_static_expr(node) -> bool:
    if isinstance(node, ast.Constant):
        return type(node.value) in (bool, int, float, str, bytes, type(None))
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Attribute):
        return _is_simple_static_expr(node.value)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_simple_static_expr(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            (k is None or _is_simple_static_expr(k)) and _is_simple_static_expr(v)
            for k, v in zip(node.keys, node.values)
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "set", "tuple", "auto"}
        and len(node.args) <= 1
        and not node.keywords
    ):
        return all(_is_simple_static_expr(a) for a in node.args)
    return False


def _class_attr_and_method_tail_queries(path, source, fd, rows):
    """For Generator and Dialect files, handle mixed edits of class-level static
    constant/collection attributes and non-dunder methods by intersecting method
    call sites and attribute names against the recorded execution tail."""
    if (
        source is None
        or fd.is_new
        or fd.is_deleted
        or fd.is_rename
        or not (
            path in {"sqlglot/generator.py", "sqlglot/dialects/dialect.py"}
            or path.startswith("sqlglot/generators/")
        )
    ):
        return None
    try:
        old_mod = impact._parse_pinned(path)
        if old_mod is None:
            return None
        new_mod = ast.parse(source)
    except SyntaxError:
        return None
    if len(old_mod.body) != len(new_mod.body):
        return None
    ranges = []
    attr_names = set()

    def _simple_const_attr(stmt):
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name, val = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            name, val = stmt.target.id, stmt.value
        else:
            return None
        if name.startswith("__") or name.endswith("_sql"):
            return None
        if _is_simple_static_expr(val):
            return name
        return None

    for a, b in zip(old_mod.body, new_mod.body):
        if ast.dump(a) == ast.dump(b):
            continue
        if not (isinstance(a, ast.ClassDef) and isinstance(b, ast.ClassDef) and a.name == b.name):
            return None
        if [ast.dump(x) for x in a.bases] != [ast.dump(x) for x in b.bases]:
            return None
        old_map = {}
        new_map = {}
        for idx_s, s in enumerate(a.body):
            if isinstance(s, ast.FunctionDef):
                key = ("fn", s.name)
            elif (aname := _simple_const_attr(s)) is not None:
                key = ("attr", aname)
            else:
                key = ("other", ast.dump(s), idx_s)
            if key in old_map:
                return None
            old_map[key] = s
        for idx_s, s in enumerate(b.body):
            if isinstance(s, ast.FunctionDef):
                key = ("fn", s.name)
            elif (aname := _simple_const_attr(s)) is not None:
                key = ("attr", aname)
            else:
                key = ("other", ast.dump(s), idx_s)
            if key in new_map:
                return None
            new_map[key] = s
        old_others = [ast.dump(s) for k, s in old_map.items() if k[0] == "other"]
        new_others = [ast.dump(s) for k, s in new_map.items() if k[0] == "other"]
        if old_others != new_others:
            return None
        for key in old_map.keys() | new_map.keys():
            if key[0] == "other":
                continue
            os_ = old_map.get(key)
            ns_ = new_map.get(key)
            if os_ is not None and ns_ is not None and ast.dump(os_) == ast.dump(ns_):
                continue
            if key[0] == "attr":
                attr_names.add(key[1])
            elif key[0] == "fn":
                if key[1].startswith("__"):
                    return None
                if os_ is not None:
                    ranges.append((os_.lineno, os_.end_lineno))
                else:
                    attr_names.add(key[1])
    return [
        i
        for i, row in enumerate(rows)
        if any(p == path and any(s <= n <= e for s, e in ranges) for p, n in row["calls"])
        or attr_names.intersection(row["names"])
    ]


_BASE_EXPR_PROTECTED_DUNDERS = frozenset({
    "__init__", "__new__", "__hash__", "__eq__", "__ne__", "__deepcopy__", "__copy__",
    "__str__", "__getattr__", "__setattr__", "__delattr__", "__getattribute__",
    "__bool__", "__len__", "__iter__", "__contains__", "__class_getitem__",
    "__init_subclass__", "__getstate__", "__setstate__",
})


def _strip_docstrings(node):
    """Return a shallow-cleaned copy of module/class/function AST without docstrings."""
    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        body = list(node.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        for st in body:
            _strip_docstrings(st)
        node.body = body
    return node


_DOCSTRING_CODE_RE = re.compile(
    r"^\s*(?:def|async\s+def|class|if|elif|else:|for|while|return|raise|try:|except|finally:|with|import|from|yield|assert|del|pass|break|continue|self\.|@)\b"
)


def _is_docstring_only_diff(path, source, fd=None):
    """True when ``source`` differs from pinned ``path`` ONLY in comments/docstrings."""
    if source is None:
        return False
    if fd is not None and fd.hunks:
        # Fast O(hunk_lines) pre-filter avoiding 2x full-file ast.parse on code edits.
        saw_quote_or_comment = False
        for h in fd.hunks:
            for raw in h.lines:
                if raw[:1] not in {"+", "-"}:
                    continue
                s = raw[1:].strip()
                if not s:
                    continue
                if s.startswith("#"):
                    saw_quote_or_comment = True
                    continue
                if '"""' in s or "'''" in s:
                    saw_quote_or_comment = True
                    continue
                if _DOCSTRING_CODE_RE.match(s) or re.match(r"^[A-Za-z_]\w*\s*(?::[^=]*)?=(?!=)", s):
                    return False
        if not saw_quote_or_comment:
            return False
    before = impact._sources()["src"].get(path)
    if before is None:
        return False
    try:
        old_m = _strip_docstrings(ast.parse(before))
        new_m = _strip_docstrings(ast.parse(source))
    except SyntaxError:
        return False
    return ast.dump(old_m) == ast.dump(new_m)


def _expr_subclass_tail_queries(path, source, fd, rows):
    """For sqlglot/expressions/*.py, handle edits inside Expr classes (including
    adding brand-new Expr subclasses and editing non-implicit-dunder methods/attrs
    on base Expr/Expression) by intersecting changed methods and attribute names
    against the recorded TPC-DS execution tail."""
    if (
        source is None
        or fd.is_new
        or fd.is_deleted
        or fd.is_rename
        or not path.startswith("sqlglot/expressions/")
        or not path.endswith(".py")
    ):
        return None
    try:
        old_mod = impact._parse_pinned(path)
        if old_mod is None:
            return None
        new_mod = ast.parse(source)
    except SyntaxError:
        return None

    old_top = {}
    new_top = {}
    for idx_m, st in enumerate(old_mod.body):
        k = ("cls", st.name) if isinstance(st, ast.ClassDef) else ("other", ast.dump(st), idx_m)
        if k in old_top:
            return None
        old_top[k] = st
    for idx_m, st in enumerate(new_mod.body):
        k = ("cls", st.name) if isinstance(st, ast.ClassDef) else ("other", ast.dump(st), idx_m)
        if k in new_top:
            return None
        new_top[k] = st
    if [ast.dump(s) for k, s in old_top.items() if k[0] == "other"] != [
        ast.dump(s) for k, s in new_top.items() if k[0] == "other"
    ]:
        return None

    an = None
    ranges = []
    attr_names = set()
    for key in old_top.keys() | new_top.keys():
        if key[0] == "other":
            continue
        a = old_top.get(key)
        b = new_top.get(key)
        if a is not None and b is not None and ast.dump(a) == ast.dump(b):
            continue
        # Brand-new Expr subclass added to sqlglot/expressions/*.py whose name
        # never appeared in pinned sources cannot be instantiated by TPC-DS tail.
        if a is None and b is not None:
            if an is None:
                an = impact.Analysis()
            if impact._name_sites(an, b.name):
                return None
            continue
        if a is None or b is None:
            return None
        if [ast.dump(x) for x in a.bases] != [ast.dump(x) for x in b.bases]:
            return None
        is_base_cls = a.name in {"Expr", "Expression", "Func", "Binary", "Unary", "Condition", "Predicate", "Connector"}
        old_map = {}
        new_map = {}
        old_fns = {}
        new_fns = {}
        for idx_s, s in enumerate(a.body):
            if isinstance(s, ast.FunctionDef):
                old_fns.setdefault(s.name, []).append(s)
                continue
            elif (
                isinstance(s, ast.Assign)
                and len(s.targets) == 1
                and isinstance(s.targets[0], ast.Name)
                and s.targets[0].id == "arg_types"
            ):
                ckey = ("arg_types", "arg_types")
            elif (
                (
                    isinstance(s, ast.Assign)
                    and len(s.targets) == 1
                    and isinstance(s.targets[0], ast.Name)
                    and not s.targets[0].id.startswith("__")
                    and _is_simple_static_expr(s.value)
                )
                or (
                    isinstance(s, ast.AnnAssign)
                    and isinstance(s.target, ast.Name)
                    and s.value is not None
                    and not s.target.id.startswith("__")
                    and _is_simple_static_expr(s.value)
                )
            ):
                nm = s.targets[0].id if isinstance(s, ast.Assign) else s.target.id
                ckey = ("attr", nm)
            else:
                ckey = ("other", ast.dump(s), idx_s)
            if ckey in old_map:
                return None
            old_map[ckey] = s
        for idx_s, s in enumerate(b.body):
            if isinstance(s, ast.FunctionDef):
                new_fns.setdefault(s.name, []).append(s)
                continue
            elif (
                isinstance(s, ast.Assign)
                and len(s.targets) == 1
                and isinstance(s.targets[0], ast.Name)
                and s.targets[0].id == "arg_types"
            ):
                ckey = ("arg_types", "arg_types")
            elif (
                (
                    isinstance(s, ast.Assign)
                    and len(s.targets) == 1
                    and isinstance(s.targets[0], ast.Name)
                    and not s.targets[0].id.startswith("__")
                    and _is_simple_static_expr(s.value)
                )
                or (
                    isinstance(s, ast.AnnAssign)
                    and isinstance(s.target, ast.Name)
                    and s.value is not None
                    and not s.target.id.startswith("__")
                    and _is_simple_static_expr(s.value)
                )
            ):
                nm = s.targets[0].id if isinstance(s, ast.Assign) else s.target.id
                ckey = ("attr", nm)
            else:
                ckey = ("other", ast.dump(s), idx_s)
            if ckey in new_map:
                return None
            new_map[ckey] = s
        old_others = [ast.dump(s) for k, s in old_map.items() if k[0] == "other"]
        new_others = [ast.dump(s) for k, s in new_map.items() if k[0] == "other"]
        if old_others != new_others:
            return None
        for ckey in old_map.keys() | new_map.keys():
            if ckey[0] == "other":
                continue
            os_ = old_map.get(ckey)
            ns_ = new_map.get(ckey)
            if os_ is not None and ns_ is not None and ast.dump(os_) == ast.dump(ns_):
                continue
            if ckey[0] == "arg_types":
                attr_names.add("arg_types")
            elif ckey[0] == "attr":
                attr_names.add(ckey[1])
        for fn_name in old_fns.keys() | new_fns.keys():
            ol = old_fns.get(fn_name, ())
            nl = new_fns.get(fn_name, ())
            if [ast.dump(x) for x in ol] == [ast.dump(x) for x in nl]:
                continue
            if fn_name in _BASE_EXPR_PROTECTED_DUNDERS or (not is_base_cls and fn_name.startswith("__")):
                return None
            if fn_name.startswith("__") and fn_name != "__repr__":
                return None
            if ol:
                for fn_node in ol:
                    ranges.append((fn_node.lineno, fn_node.end_lineno))
            else:
                attr_names.add(fn_name)
    return [
        i
        for i, row in enumerate(rows)
        if any(p == path and any(s <= n <= e for s, e in ranges) for p, n in row["calls"])
        or attr_names.intersection(row["names"])
    ]


def _env_tail_queries(path, source, fd, rows):
    """For sqlglot/executor/env.py, handle edits confined to top-level helper
    functions (other than null_if_any/filter_nulls) and/or ENV dict entries by
    intersecting changed helper/lambda line ranges against recorded tail calls."""
    if path != "sqlglot/executor/env.py" or source is None or fd.is_new or fd.is_deleted or fd.is_rename:
        return None
    try:
        old_mod = impact._parse_pinned(path)
        if old_mod is None:
            return None
        new_mod = ast.parse(source)
    except SyntaxError:
        return None
    old_defs, new_defs = {}, {}
    old_env = new_env = None
    old_other, new_other = [], []
    for st in old_mod.body:
        if isinstance(st, ast.FunctionDef):
            if st.name in old_defs:
                return None
            old_defs[st.name] = st
        elif (
            isinstance(st, ast.Assign)
            and len(st.targets) == 1
            and isinstance(st.targets[0], ast.Name)
            and st.targets[0].id == "ENV"
            and isinstance(st.value, ast.Dict)
        ):
            old_env = st.value
        else:
            old_other.append(ast.dump(st))
    for st in new_mod.body:
        if isinstance(st, ast.FunctionDef):
            if st.name in new_defs:
                return None
            new_defs[st.name] = st
        elif (
            isinstance(st, ast.Assign)
            and len(st.targets) == 1
            and isinstance(st.targets[0], ast.Name)
            and st.targets[0].id == "ENV"
            and isinstance(st.value, ast.Dict)
        ):
            new_env = st.value
        else:
            new_other.append(ast.dump(st))
    if old_other != new_other or old_env is None or new_env is None:
        return None

    ranges = []
    for name in old_defs.keys() | new_defs.keys():
        od, nd = old_defs.get(name), new_defs.get(name)
        if od is not None and nd is not None and ast.dump(od) == ast.dump(nd):
            continue
        if name in {"null_if_any", "filter_nulls"} or name.startswith("__"):
            return None
        if od is not None:
            ranges.append((od.lineno, od.end_lineno))

    def _dict_map(d):
        out = {}
        for k, v in zip(d.keys, d.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                return None
            out[k.value] = v
        return out

    om, nm = _dict_map(old_env), _dict_map(new_env)
    if om is None or nm is None:
        return None

    def _env_val_range(val):
        if isinstance(val, ast.Name) and val.id in old_defs:
            fn = old_defs[val.id]
            return (fn.lineno, fn.end_lineno)
        if isinstance(val, ast.Lambda):
            return (val.lineno, val.end_lineno)
        if (
            isinstance(val, ast.Call)
            and isinstance(val.func, ast.Name)
            and val.func.id in {"null_if_any", "filter_nulls"}
            and val.args
        ):
            return _env_val_range(val.args[0])
        return None

    import builtins as _bi
    for k in om.keys() | nm.keys():
        ov, nv = om.get(k), nm.get(k)
        if ov is not None and nv is not None and ast.dump(ov) == ast.dump(nv):
            continue
        if ov is None:
            if not k.isupper() or hasattr(_bi, k):
                return None
            continue
        rng = _env_val_range(ov)
        if rng is None:
            return None
        ranges.append(rng)

    return [
        i
        for i, row in enumerate(rows)
        if any(p == path and any(s <= n <= e for s, e in ranges) for p, n in row["calls"])
    ]


def tail_cases_info(path, source, fd, *, strict=True):
    """Return ``(forced_query_indices, needs_pygen)`` for ``path``.

    ``needs_pygen`` is True ONLY when ``_PYGEN_TAIL_PATHS`` fallback is used
    (so ``pygen_plan_digest`` does NOT burn CPU when method/class-attr slicing
    already proves ``forced_query_indices == []``).
    """
    rows = downstream()
    all_cases = list(range(len(rows)))
    if not path.startswith("sqlglot/") or not path.endswith(".py"):
        return [], False
    if _inert_new_class_attrs(path, source, fd):
        return [], False
    # Pure insertion of brand-new AutoName enum member(s) (e.g. TokenType / DataType.Type).
    if (
        path in {"sqlglot/tokenizer_core.py", "sqlglot/expressions/datatypes.py"}
        and not (fd.is_new or fd.is_deleted or fd.is_rename)
        and fd.hunks
        and not any(h.removed_old_lines for h in fd.hunks)
    ):
        added = [line[1:] for h in fd.hunks for line in h.lines if line[:1] == "+"]
        names = []
        ok = bool(added)
        for ln in added:
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            m = re.fullmatch(r"\s+([A-Z][A-Z0-9_]*)\s*=\s*auto\(\)\s*(?:#.*)?", ln)
            if not m:
                ok = False
                break
            names.append(m.group(1))
        if ok and names:
            an = impact.Analysis()
            if all(not impact._name_sites(an, nm) for nm in names):
                return [], False
    tpaths = all_tail_paths()
    if fd.is_new and path not in tpaths:
        return [], False
    if path not in tpaths and (strict or _no_cross_module_hazards(fd, allow_sqlglot_imports=True)):
        return [], False
    if fd.is_new or fd.is_deleted or fd.is_rename:
        return all_cases, False
    if _is_docstring_only_diff(path, source, fd):
        return [], False
    # Fast O(method_lines) method-body confinement FIRST (zero full-file ast.parse,
    # zero pygen CPU when it already proves tail_cases == []!).
    if strict or _no_cross_module_hazards(fd):
        mq = _method_tail_queries(path, source, fd, rows)
        if mq is not None:
            if mq and path in _PYGEN_TAIL_PATHS and (APP_DIR / "pin-cache" / "pygen-digests.json").is_file():
                return [], True
            return mq, False
        if path == "sqlglot/executor/env.py":
            eq = _env_tail_queries(path, source, fd, rows)
            if eq is not None:
                return eq, False
    # Fallback for non-executor tail paths covered by pygen_plan_digest BEFORE
    # full-file AST parsers so huge core files never waste queue CPU.
    if (
        path in _PYGEN_TAIL_PATHS
        and (APP_DIR / "pin-cache" / "pygen-digests.json").is_file()
        and (strict or _no_cross_module_hazards(fd, allow_sqlglot_imports=True))
    ):
        return [], True
    if strict or _no_cross_module_hazards(fd):
        gm = _class_attr_and_method_tail_queries(path, source, fd, rows)
        if gm is not None:
            return gm, False
        em = _expr_subclass_tail_queries(path, source, fd, rows)
        if em is not None:
            return em, False
    if not re.fullmatch(
        r"sqlglot/(parser|generator|(?:parsers|generators|optimizer|executor)/\w+)\.py",
        path,
    ):
        if (strict or _no_cross_module_hazards(fd)) and path in tail_path_queries() and len(tail_path_queries()[path]) <= 5:
            return list(tail_path_queries()[path]), False
        return all_cases, False
    if path.startswith("sqlglot/optimizer/") and source is not None:
        old = ast.parse((Path(verify.PRISTINE) / path).read_text())
        new = ast.parse(source)
        for a, b in zip(old.body, new.body):
            if not (isinstance(a, ast.FunctionDef) and isinstance(b, ast.FunctionDef)
                    and not a.decorator_list and len(a.args.defaults) == len(b.args.defaults)):
                continue
            for i, (x, y) in enumerate(zip(a.args.defaults, b.args.defaults)):
                if not (isinstance(x, ast.Constant) and isinstance(y, ast.Constant)
                        and type(x.value) is type(y.value) is bool and x.value != y.value):
                    continue
                b.args.defaults[i] = x
                try:
                    if ast.dump(old) == ast.dump(new):
                        return [i for i, row in enumerate(rows)
                                if path in row["paths"] or a.name in row["names"]], False
                finally:
                    b.args.defaults[i] = y
    if source is None:
        return all_cases, False
    lines = {n for h in fd.hunks for n in h.removed_old_lines}
    if lines and not any(not h.old_len or not h.new_len for h in fd.hunks):
        before = (Path(verify.PRISTINE) / path).read_text()
        old = ast.parse(before)
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
                        return all_cases, False
                    changed = other.body[cls.body.index(stmt)]
                    if type(changed) != type(stmt):
                        return all_cases, False
                    changed.value = stmt.value
                    if ast.dump(new) != ast.dump(old):
                        return all_cases, False
                    return [
                        i
                        for i, row in enumerate(rows)
                        if path in row["paths"] or ids.intersection(row["names"])
                    ], False
    if (strict or _no_cross_module_hazards(fd, allow_sqlglot_imports=True)) and path in tail_path_queries() and len(tail_path_queries()[path]) <= 5:
        return list(tail_path_queries()[path]), False
    return all_cases, False


def tail_cases(path, source, fd, *, strict=True):
    return tail_cases_info(path, source, fd, strict=strict)[0]


def _harmless_config(fd) -> bool:
    """True when an edit to pyproject.toml or setup.cfg leaves pytest/build
    configuration identical to the pin (matching impact.Analysis.config_file)."""
    p = fd.path
    if (
        p not in {"pyproject.toml", "setup.cfg"}
        or fd.is_new
        or fd.is_deleted
        or fd.is_rename
        or fd.is_binary
        or fd.mode_change
        or not fd.hunks
    ):
        return False
    old = impact._sources().get("config", {}).get(p)
    if old is None:
        return False
    try:
        new = verify._apply_file(fd, verify.PRISTINE)
        if new is None:
            return False
        if p == "pyproject.toml":
            import tomllib
            o, n = tomllib.loads(old), tomllib.loads(new)
            keep = lambda d: {"pytest": d.get("tool", {}).get("pytest"), "build": d.get("build-system")}
            return keep(o) == keep(n)
        import configparser
        def _sect(text):
            cp = configparser.RawConfigParser()
            cp.read_string(text)
            return {s: dict(cp.items(s)) for s in cp.sections() if "pytest" in s.lower()}
        return _sect(old) == _sect(new)
    except Exception:
        return False


def patch_verifiable_files(files, *, forbid_prefixes=(), allow_structural=False, sqlglot_only=False):
    """Basic safety check for running faithful probes in a clean overlay fork."""
    if not files:
        return False
    for f in files:
        if f.mode_change:
            return False
        if f.is_binary:
            if sqlglot_only or not all(_is_doc_path(p) for p in f.paths):
                return False
            continue
        if not allow_structural and (f.is_new or f.is_deleted or f.is_rename):
            return False
        if not sqlglot_only and _harmless_config(f):
            continue
        for p in f.paths:
            if not p or ".." in p.split("/") or p.startswith("/"):
                return False
            base = p.rsplit("/", 1)[-1]
            if base in _PYTEST_LEVEL_NAMES:
                return False
            if sqlglot_only:
                if not (p.startswith("sqlglot/") and p.endswith(".py")):
                    return False
            elif not (p.startswith("sqlglot/") or p.startswith("tests/") or _is_doc_path(p)):
                return False
            if p.endswith((".so", ".pyd", ".pth")):
                return False
            if any(p == pre or p.startswith(pre) for pre in forbid_prefixes):
                return False
    return True


def _parser2_disjoint_file(f):
    """True when ``f`` provably cannot affect ``parser:2`` once ``small``
    (``parser:setup`` + ``parser:0, 1, 3``) passes."""
    path = f.path
    if not path or not path.startswith("sqlglot/") or not path.endswith(".py") or not f.hunks:
        return False
    pprof = profile("parser")
    p2_lines = pprof["parser:2"]["lines"]
    setup_lines = pprof["parser:setup"]["lines"]
    if path not in p2_lines:
        if path == "sqlglot/dialects/__init__.py":
            delta = "\n".join(line[1:] for h in f.hunks for line in h.lines if line[:1] in {"+", "-"})
            return not re.search(r"\b(Parser|max_nodes|setattr|delattr|exec|eval)\b", delta)
        if f.is_deleted or f.is_rename:
            return False
        return _no_cross_module_hazards(f, allow_sqlglot_imports=True)
    if f.is_new or f.is_deleted or f.is_rename or not _no_cross_module_hazards(f, allow_sqlglot_imports=True):
        return False
    # Pure addition of brand-new TokenType enum member(s) in tokenizer_core.py
    # cannot affect parser:2 once small() passes.
    if path == "sqlglot/tokenizer_core.py" and not any(h.removed_old_lines for h in f.hunks):
        added = [line[1:] for h in f.hunks for line in h.lines if line[:1] == "+"]
        if added and all(
            not ln.strip() or re.fullmatch(r"\s+[A-Z][A-Z0-9_]*\s*=\s*auto\(\)\s*(?:#.*)?", ln)
            for ln in added
        ):
            return True
    scopes = impact._index().get("scopes", {}).get(path)
    if not scopes:
        return False
    defs = [
        (start, end, name, body_start)
        for kind, start, end, name, body_start in scopes[1]
        if kind == "def"
    ]
    if not defs:
        return False

    def _enclosing_def(ln):
        cands = [d for d in defs if d[0] <= ln <= d[1]]
        return max(cands, key=lambda x: x[0]) if cands else None

    before_lines = impact._sources()["src"].get(path, "").splitlines()
    touched = set()
    for h in f.hunks:
        old_ln = h.old_start
        for raw in h.lines:
            tag = raw[:1]
            if tag == " ":
                old_ln += 1
            elif tag == "-":
                inner = _enclosing_def(old_ln)
                if inner is None:
                    return False
                touched.add(inner)
                old_ln += 1
            elif tag == "+":
                inner = _enclosing_def(old_ln - 1) if old_ln > 1 else None
                if inner is None:
                    inner = _enclosing_def(old_ln)
                if inner is None:
                    return False
                line_body = raw[1:]
                if line_body.strip():
                    def_line = before_lines[inner[0] - 1]
                    def_indent = len(def_line) - len(def_line.lstrip(" "))
                    add_indent = len(line_body) - len(line_body.lstrip(" "))
                    simple = inner[2].rsplit(".", 1)[-1]
                    if add_indent <= def_indent and not (
                        add_indent == def_indent
                        and line_body.lstrip(" ").startswith(f"def {simple}(")
                    ):
                        return False
                touched.add(inner)
    if not touched:
        return False
    p2_hit = p2_lines.get(path, ())
    setup_hit = setup_lines.get(path, ())
    for start, end, qual, body_start in touched:
        simple = qual.rsplit(".", 1)[-1]
        if simple.startswith("__"):
            return False
        if any(start <= n <= end for n in p2_hit):
            return False
        body_in_setup = any(body_start <= n <= end for n in setup_hit)
        if body_in_setup and not (path == "sqlglot/dialects/dialect.py" and simple.endswith("_sql")):
            return False
    return True


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
    delta_code = "\n".join(_strip_comment(ln) for ln in lines if not ln.lstrip().startswith("#"))
    if path in {
        "sqlglot/dialects/dialect.py",
        "sqlglot/dialects/__init__.py",
        "sqlglot/dialects/doris.py",
    } or re.search(
        r"\b(import|global|nonlocal|setattr|delattr|exec|eval|_classes)\b|__\w+__|cache|registr",
        delta_code,
        re.I,
    ) or re.search(r"\bDIALECTS\b", delta_code):
        return None
    if group == "parser":
        if re.search(
            r"\b(?:\d[\d_]*|for|while|range|len|limit|depth|recursion|max_nodes)\b",
            delta_code,
            re.I,
        ):
            return None
        # errors.py is only entered on ParseError; scaled() already triggers the
        # exact max_nodes ParseError and compares [type, str(exc)] bit-for-bit.
        if path != "sqlglot/errors.py":
            reached = profile("parser")["parser:2"]["lines"].get(path, [])
            if any(
                h.old_start <= n < h.old_start + max(h.old_len, 1)
                for n in reached
                for h in f.hunks
            ):
                return None
    return True


def _parser_scaled_eligible(f, *, allow_monitored_loops=False, allow_monitored_edits=False):
    """Sound eligibility gate for ``scaled()`` verification of ``test_max_nodes``.
    Allows edits across any ``sqlglot/*.py`` provided hard node-limit/recursion/
    metaprogramming hazards are absent; when ``allow_monitored_edits=True``,
    big numeric literals (>= 50), ``depth``, and loops in ``p2_lines`` files are
    verified via ``sys.monitoring`` LINE reachability in ``staged.py scaled``."""
    path = f.path
    if (
        not re.fullmatch(r"sqlglot/(?:\w+/)*\w+\.py", path)
        or (not allow_monitored_edits and (f.paths != {path} or f.is_new))
        or f.is_deleted
        or f.is_rename
        or f.mode_change
        or f.is_binary
        or not f.hunks
    ):
        return False
    p2_lines = profile("parser")["parser:2"]["lines"]
    lines = [line[1:] for h in f.hunks for line in h.lines if line[:1] in {"+", "-"}]
    code_lines = [
        re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', _strip_comment(ln))
        for ln in lines
        if not ln.lstrip().startswith("#")
    ]
    delta_code = "\n".join(code_lines)
    # Hard hazards that can never be safely cleared by scaled parity alone.
    if re.search(
        r"\b(max_nodes|_node_count|recursion|setrecursionlimit|max_depth|eval|exec|setattr|delattr|gc|global|nonlocal|_classes)\b",
        delta_code,
        re.I,
    ) or re.search(r"\bDIALECTS\b", delta_code):
        return False
    if path in p2_lines and not allow_monitored_edits:
        if re.search(r"\bdepth\b", delta_code, re.I):
            return False
        # Reject any numeric literal >= 50 (could distinguish width=100 from width=10000).
        for m in re.finditer(r"\b(\d[\d_]*)\b", delta_code):
            try:
                if int(m.group(1).replace("_", "")) >= 50:
                    return False
            except ValueError:
                return False
        if not allow_monitored_loops:
            # Reject any loop edit in a file executed by parser:2 unless monitored.
            if re.search(r"\b(for|while)\b", delta_code):
                return False
    return True


def _hunk_has_parser_scale_hazard(code_text: str) -> bool:
    if re.search(r"\b(for|while|depth)\b", code_text, re.I):
        return True
    for m in re.finditer(r"\b(\d[\d_]*)\b", code_text):
        try:
            if int(m.group(1).replace("_", "")) >= 50:
                return True
        except ValueError:
            return True
    return False


def _parser_edited_lines(f, after_src):
    """Return executable new-file line numbers from hunks containing scale hazards
    (loops, ``depth``, or numeric literals >= 50) in ``p2_lines`` files, for
    exact-reachability monitoring in ``scaled()``."""
    p2_lines = profile("parser")["parser:2"]["lines"]
    if f.path not in p2_lines:
        return set()
    if not after_src:
        return None
    try:
        ast.parse(after_src)
    except SyntaxError:
        return None
    lines = after_src.splitlines()
    watch = set()
    for h in f.hunks:
        new_ln = h.new_start
        h_added_lines = []
        h_add_code = []
        h_rem_code = []
        for raw in h.lines:
            tag = raw[:1]
            if tag == "+":
                if 1 <= new_ln <= len(lines):
                    s = lines[new_ln - 1].strip()
                    if s and not s.startswith("#"):
                        h_added_lines.append(new_ln)
                        h_add_code.append(_strip_comment(raw[1:]))
                new_ln += 1
            elif tag == "-":
                h_rem_code.append(_strip_comment(raw[1:]))
            elif tag == " ":
                new_ln += 1
        h_all_code = "\n".join(h_add_code + h_rem_code)
        if not _hunk_has_parser_scale_hazard(h_all_code):
            continue
        if h_added_lines:
            watch.update(h_added_lines)
        elif h_rem_code:
            rem_s = "\n".join(h_rem_code)
            if re.search(r"\bdepth\b", rem_s, re.I) or any(
                int(m.group(1).replace("_", "")) >= 50 for m in re.finditer(r"\b(\d[\d_]*)\b", rem_s)
            ):
                return None
    return watch


def _parser_new_loop_lines(f, after_src):
    """For a ``p2_lines`` file with ``for``/``while`` in its diff, return the
    set of new-file line numbers inside every newly added ``For``/``While`` loop
    body (or ``None`` on parse failure). Monitored in ``staged.py scaled``."""
    p2_lines = profile("parser")["parser:2"]["lines"]
    if f.path not in p2_lines:
        return set()
    needs_monitor = False
    added_new_lines = set()
    replaced_for_lines = set()
    for h in f.hunks:
        new_ln = h.new_start
        removed_for_indents = set()
        hunk_has_loop = False
        hunk_added = []
        for raw in h.lines:
            tag = raw[:1]
            body = raw[1:]
            code = _strip_comment(body)
            if tag == "-":
                if re.match(r"^\s*for\b", code):
                    removed_for_indents.add(len(body) - len(body.lstrip(" ")))
                if re.search(r"\b(for|while)\b", code):
                    hunk_has_loop = True
            elif tag == "+":
                added_new_lines.add(new_ln)
                if re.search(r"\b(for|while)\b", code):
                    hunk_has_loop = True
                    hunk_added.append((new_ln, body, code))
                new_ln += 1
            else:
                new_ln += 1
        if hunk_has_loop:
            needs_monitor = True
            for ln_no, body, code in hunk_added:
                indent = len(body) - len(body.lstrip(" "))
                if re.match(r"^\s*for\b", code) and indent in removed_for_indents:
                    replaced_for_lines.add(ln_no)
    if not needs_monitor:
        return set()
    if not after_src:
        return None
    try:
        tree = ast.parse(after_src)
    except SyntaxError:
        return None
    watch = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.While)):
            if node.lineno in added_new_lines and node.lineno not in replaced_for_lines:
                for st in (*node.body, *node.orelse):
                    end = st.end_lineno or st.lineno
                    watch.update(range(st.lineno, end + 1))
    # If a loop was edited inside an existing loop without adding a brand-new loop
    # header, watch every added line inside any loop overlapping p2_lines.
    if not watch:
        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.While)):
                end = node.end_lineno or node.lineno
                for ln_no in added_new_lines:
                    if node.lineno < ln_no <= end:
                        watch.add(ln_no)
    return watch


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


def optimizer_tuple_reads(path, source):
    if not path.startswith("sqlglot/optimizer/"):
        return None
    before = ast.parse((Path(verify.PRISTINE) / path).read_text())
    after = ast.parse(source)
    for a, b in zip(before.body, after.body):
        if not (isinstance(a, ast.Assign) and isinstance(b, ast.Assign)
                and len(a.targets) == 1 and isinstance(a.targets[0], ast.Name)):
            continue
        name = a.targets[0].id
        if not name.isupper() or not isinstance(a.value, ast.Tuple) or not isinstance(b.value, ast.Tuple):
            continue
        if not all(isinstance(e, ast.Attribute) and isinstance(e.value, ast.Name) and e.value.id == "exp"
                   for e in [*a.value.elts, *b.value.elts]):
            continue
        if ast.dump(a.value) == ast.dump(b.value):
            continue
        b.value = a.value
        if ast.dump(before) != ast.dump(after):
            return None
        reads = []
        for p, line, lo, hi, scope in impact._name_sites(impact.Analysis(), name):
            if p == path and a.lineno <= line <= a.end_lineno:
                continue
            if p != path or not scope or scope[0] != "def" or line < scope[4]:
                return None
            reads.append((lo, hi))
        return reads or None
    return None


@functools.cache
def _target_bit(group):
    idx = impact._index()
    tests = idx["tests"]
    for tid, grp in TARGETS.items():
        if grp == group and tid in tests:
            return 1 << tests.index(tid)
    return 0


_TARGET_TEST_PROTECTED = {
    "ds": (
        "tests/test_executor.py",
        "TestExecutor",
        frozenset({
            "test_execute_tpcds", "setUpClass", "tearDownClass", "setUp", "tearDown",
            "subtestHelper", "cached_execute", "_mp_execute",
        }),
    ),
    "tpch_opt": (
        "tests/test_executor.py",
        "TestExecutor",
        frozenset({
            "test_optimized_tpch", "setUpClass", "tearDownClass", "setUp", "tearDown",
            "cached_execute", "rename_anonymous",
        }),
    ),
    "tpch_exec": (
        "tests/test_executor.py",
        "TestExecutor",
        frozenset({
            "test_execute_tpch", "setUpClass", "tearDownClass", "setUp", "tearDown",
            "subtestHelper", "cached_execute", "_mp_execute",
        }),
    ),
    "parser": (
        "tests/test_parser.py",
        "TestParser",
        frozenset({"test_max_nodes", "setUpClass", "tearDownClass", "setUp", "tearDown"}),
    ),
    "generator": (
        "tests/test_generator.py",
        "TestGenerator",
        frozenset({"test_generator_memory", "setUpClass", "tearDownClass", "setUp", "tearDown"}),
    ),
}


def _apply_verifiable(text: str) -> dict[str, str | None]:
    """Apply patch text, ignoring only verified binary doc/image files."""
    files = parse(text)
    bin_docs = {
        p
        for f in files
        if f.is_binary and f.paths and all(_is_doc_path(p) for p in f.paths)
        for p in f.paths
    }
    return verify.apply_patch(text, ignore_binary=bin_docs) if bin_docs else verify.apply_patch(text)


def _test_file_disjoint_from_target(fd, group):
    """True when a modification to ``tests/test_{executor,parser,generator}.py``
    ONLY touches unrelated ``def test_*`` methods inside the test class and
    never edits module-level code, setup/helper methods, or the heavy target."""
    spec = _TARGET_TEST_PROTECTED.get(group)
    if not spec or fd.path != spec[0] or fd.is_new or fd.is_deleted or fd.is_rename or fd.is_binary or fd.mode_change or not fd.hunks:
        return False
    tpath, cls_name, protected = spec
    try:
        old_mod = impact._parse_pinned(tpath)
        if old_mod is None:
            return False
        after = verify._apply_file(fd, verify.PRISTINE)
        if after is None:
            return False
        new_mod = ast.parse(after)
    except Exception:
        return False
    old_bound = set()
    for st in old_mod.body:
        old_bound |= impact._bound_names(st)
    old_body = list(old_mod.body)
    new_body = []
    old_dumps = {ast.dump(st) for st in old_body}
    for st in new_mod.body:
        if ast.dump(st) in old_dumps or (isinstance(st, ast.ClassDef) and st.name == cls_name):
            new_body.append(st)
            continue
        # Allow adding safe top-level stdlib/sqlglot imports that do not shadow
        # any existing top-level binding in the test module.
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            line = ast.unparse(st)
            bound = impact._bound_names(st)
            if (
                bound
                and not (bound & old_bound)
                and (_STDLIB_IMPORT_RE.match(line) or _SQLGLOT_SAFE_IMPORT_RE.match(line))
            ):
                continue
        return False
    if len(old_body) != len(new_body):
        return False
    for a, b in zip(old_body, new_body):
        if ast.dump(a) == ast.dump(b):
            continue
        if not (isinstance(a, ast.ClassDef) and isinstance(b, ast.ClassDef) and a.name == b.name == cls_name):
            return False
        if [ast.dump(x) for x in a.bases] != [ast.dump(x) for x in b.bases]:
            return False
        old_fns, new_fns = {}, {}
        old_other, new_other = [], []
        for s in a.body:
            if isinstance(s, ast.FunctionDef):
                if s.name in old_fns:
                    return False
                old_fns[s.name] = s
            else:
                old_other.append(ast.dump(s))
        for s in b.body:
            if isinstance(s, ast.FunctionDef):
                if s.name in new_fns:
                    return False
                new_fns[s.name] = s
            else:
                new_other.append(ast.dump(s))
        if old_other != new_other:
            return False
        for nm in old_fns.keys() | new_fns.keys():
            os_, ns_ = old_fns.get(nm), new_fns.get(nm)
            if os_ is not None and ns_ is not None and ast.dump(os_) == ast.dump(ns_):
                continue
            if nm in protected or not nm.startswith("test_"):
                return False
    return True


_HELPERS_UNUSED = {
    "ds": frozenset({"TPCH_SCHEMA", "assert_logger_contains", "load_sql_fixtures"}),
    "tpch_opt": frozenset({"TPCDS_SCHEMA", "assert_logger_contains", "load_sql_fixtures"}),
    "tpch_exec": frozenset({"TPCDS_SCHEMA", "assert_logger_contains", "load_sql_fixtures"}),
    "parser": frozenset({"TPCH_SCHEMA", "TPCDS_SCHEMA", "assert_logger_contains", "load_sql_fixtures", "load_sql_fixture_pairs", "_filter_comments", "_extract_meta"}),
    "generator": frozenset({"TPCH_SCHEMA", "TPCDS_SCHEMA", "assert_logger_contains", "load_sql_fixtures", "load_sql_fixture_pairs", "_filter_comments", "_extract_meta"}),
}


def _helpers_disjoint_from_target(fd, group):
    """True when an edit to ``tests/helpers.py`` ONLY modifies helpers/constants
    unused by ``group`` (preserving all module-level imports and shared symbols)."""
    allowed = _HELPERS_UNUSED.get(group)
    if not allowed or fd.path != "tests/helpers.py" or fd.is_new or fd.is_deleted or fd.is_rename or fd.is_binary or fd.mode_change or not fd.hunks:
        return False
    try:
        old_mod = impact._parse_pinned("tests/helpers.py")
        if old_mod is None:
            return False
        after = verify._apply_file(fd, verify.PRISTINE)
        if after is None:
            return False
        compile(after.encode(), "tests/helpers.py", "exec", dont_inherit=True)
        new_mod = ast.parse(after)
    except Exception:
        return False
    if len(old_mod.body) != len(new_mod.body):
        return False
    for a, b in zip(old_mod.body, new_mod.body):
        if ast.dump(a) == ast.dump(b):
            continue
        names = impact._bound_names(a) | impact._bound_names(b)
        if not names or not (names <= allowed):
            return False
    return True


def _target_unreachable(fd, group):
    """True when a non-sqlglot file under tests/ (or harmless doc/config file) is
    provably never imported, executed, or opened by the heavy target test."""
    p = fd.path
    if not p or fd.mode_change:
        return False
    if fd.is_binary:
        return all(_is_doc_path(path) for path in fd.paths)
    if _harmless_config(fd):
        return True
    if _test_file_disjoint_from_target(fd, group) or _helpers_disjoint_from_target(fd, group):
        return True
    if not (p.startswith("tests/") or _is_doc_path(p)):
        return False
    if p in {"tests/__init__.py", "tests/helpers.py", "tests/conftest.py"}:
        return False
    if p.rsplit("/", 1)[-1] in _PYTEST_LEVEL_NAMES:
        return False
    bit = _target_bit(group)
    if not bit:
        return False
    idx = impact._index()
    sets = idx["sets"]
    for path in fd.paths:
        if not path or not (path.startswith("tests/") or _is_doc_path(path)):
            return False
        cov = idx["cov"].get(path)
        if cov and any(sets[sid] & bit for sid in set(cov.values())):
            return False
        osid = idx["opens"].get(path)
        if osid is not None and (sets[osid] & bit):
            # For TPC SQL fixtures on ds, field-level projection proves whether
            # test_execute_tpcds reads any modified field (e.g. tpc-ds.sql expected-only
            # or valid tpc-h.sql changes never affect test_execute_tpcds).
            if (
                group == "ds"
                and path in {
                    "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql",
                    "tests/fixtures/optimizer/tpc-h/tpc-h.sql",
                }
                and not (fd.is_new or fd.is_deleted or fd.is_rename)
            ):
                from . import fixture_fields
                fp = impact.FilePatch()
                fp.old_path = fd.old_path
                fp.new_path = fd.new_path
                fp.status = "M"
                fp.binary = False
                for h in fd.hunks:
                    ih = impact.Hunk(h.old_start, h.old_len, h.new_start, h.new_len)
                    ih.lines = [(ln[:1], ln[1:]) for ln in h.lines]
                    fp.hunks.append(ih)
                proj = fixture_fields.project(fp, sets[osid], idx["tests"], impact.apply_hunks)
                if not (proj & bit):
                    continue
            return False
        if fd.is_new or fd.is_deleted or fd.is_rename:
            d = path
            while "/" in d:
                d = d.rsplit("/", 1)[0]
                dsid = idx["listdirs"].get(d)
                if dsid is not None and (sets[dsid] & bit):
                    return False
    return True


@functools.lru_cache(maxsize=4)
def changed_regions(text, *, sqlglot_only=False):
    changes = _apply_verifiable(text)
    result = {}
    files = {f.path: f for f in parse(text)}
    for path, source in changes.items():
        if sqlglot_only and not path.startswith("sqlglot/"):
            continue
        if source is None or not re.fullmatch(r"sqlglot/(?:\w+/)*\w+\.py", path):
            return None
        original = (Path(verify.PRISTINE) / path).read_text()
        removed = [n for h in files[path].hunks for n in h.removed_old_lines]
        scopes = impact._index()["scopes"].get(path)
        if scopes and any(not any(k == "def" and body <= n <= end
                                  for k, _, end, _, body in scopes[1]) for n in removed):
            ranges = optimizer_tuple_reads(path, source)
            if ranges:
                result[path] = ranges
                continue
            return None
        ranges = method_spans(path, files[path], original, source)
        if ranges is None:
            ranges = query_slice.spans(original, source)
        if ranges is None:
            return None
        result[path] = ranges
    return result or None


@functools.cache
def probe_profiles(group):
    with gzip.open(APP_DIR / f"profile-{group}.gz") as stream:
        return json.load(stream)


@functools.cache
def _dialect_reverse_deps():
    idx = impact._index()
    imports = idx.get("imports", {})
    all_dialects = sorted(k[4:] for k in probe_profiles("generator") if k.startswith("gen:"))
    dep_to_dialects = {}
    for d in all_dialects:
        mod = "sqlglot.dialects.dialect" if d == "" else f"sqlglot.dialects.{d}"
        seen = set()
        stack = [mod]
        while stack:
            m = stack.pop()
            if m in seen:
                continue
            seen.add(m)
            for nxt in imports.get(m, ()):
                if nxt.startswith("sqlglot"):
                    stack.append(nxt)
        for cls_full, bases in idx.get("mro", {}).items():
            if cls_full.startswith(mod + "."):
                for b in bases:
                    parts = b.rsplit(".", 1)[0]
                    if parts.startswith("sqlglot"):
                        seen.add(parts)
        for m in seen:
            dep_to_dialects.setdefault(m, set()).add(d)
    return dep_to_dialects, all_dialects


_GEN_LOOP_ATTRS_RE = re.compile(
    r"\b(__slots__|_DISPATCH_CACHE|NORMALIZE_FUNCTIONS|QUOTE_END|BYTE_END|"
    r"IDENTIFIER_END|IDENTIFIER_ESCAPED_SEQUENCES|IDENTIFIER_START|"
    r"STRING_ESCAPES|SELECT_KINDS|generator_class|tokenizer_class|"
    r"_classes|DIALECTS|__init__|_build_dispatch|name_sequence|"
    r"get_or_raise|exec|eval|setattr|delattr)\b"
)


def _core_file_disjoint_from_gen_loop(f):
    """True when an edit to generator.py, dialects/dialect.py, or helper.py
    never touches any line executed inside ``for _ in range(1000): dialect.generator()``
    or any class attribute read during Generator.__init__."""
    path = f.path
    if f.is_new or f.is_deleted or f.is_rename or not f.hunks:
        return False
    lines = [line[1:] for h in f.hunks for line in h.lines if line[:1] in {"+", "-"}]
    code = "\n".join(_strip_comment(ln) for ln in lines if not ln.lstrip().startswith("#"))
    if _GEN_LOOP_ATTRS_RE.search(code):
        return False
    if re.search(r"\bimport\b", code):
        for ln in lines:
            if re.search(r"\bimport\b", _strip_comment(ln)) and not (
                _STDLIB_IMPORT_RE.match(ln) or _SQLGLOT_SAFE_IMPORT_RE.match(ln)
            ):
                return False
    gp = probe_profiles("generator")
    gen_lines = {n for k, v in gp.items() if k.startswith("gen:") for n in v.get(path, ())}
    for h in f.hunks:
        lo = h.old_start
        hi = h.old_start + max(h.old_len, 1)
        if any(lo <= n < hi for n in gen_lines):
            return False
    return True


def _affected_generator_dialects(sqlglot_files):
    """Return the exact subset of Dialects whose ``dialect.generator()`` memory
    loop can be affected by ``sqlglot_files``, or None if full check is needed."""
    dep_map, all_dialects = _dialect_reverse_deps()
    affected_set = set()
    for f in sqlglot_files:
        p = f.path
        if p == "sqlglot/dialects/__init__.py":
            return None
        if p in _GEN_LOOP_CORE:
            if _core_file_disjoint_from_gen_loop(f):
                continue
            return None
        if re.fullmatch(r"sqlglot/(dialects|generators)/\w+\.py", p):
            stem = p.rsplit("/", 1)[-1][:-3]
            if stem in {"dialect", "__init__", "base", "python"}:
                return None
            hits = dep_map.get(impact._module_name(p))
            if hits is None:
                return None
            affected_set.update(hits)
            continue
        # Files outside dialects/generators/core are never executed inside
        # ``for _ in range(1000): dialect.generator()``. The cold/reset
        # Dialect.get_or_raise check in generator_cases.py still verifies every
        # dialect imports and resets cleanly.
        if re.fullmatch(
            r"sqlglot/(parser|tokens|tokenizer_core|errors|schema|planner|lineage|diff|serde|transforms|trie|anonymize|jsonpath|(?:parsers|typing|optimizer|expressions|executor)/\w+)\.py",
            p,
        ):
            continue
        return None
    if len(affected_set) >= len(all_dialects):
        return None
    return sorted(affected_set)


def _hunks_miss_lines(files, outside):
    for f in files:
        blocked = outside.get(f.path)
        if not blocked:
            continue
        for h in f.hunks:
            lo = h.old_start
            hi = h.old_start + max(h.old_len, 1)
            if any(lo <= n < hi for n in blocked):
                return False
    return True


@functools.lru_cache(maxsize=256)
def stage_edit(text, group):
    files = parse(text)
    if (group == "ds" and len(files) == 1
            and files[0].path == "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
            and files[0].paths == {files[0].path} and files[0].hunks
            and not (files[0].is_new or files[0].is_deleted or files[0].is_rename
                     or files[0].is_binary or files[0].mode_change)):
        return "fixture-query", "", verify.apply_patch(text), []

    sql_files = [f for f in files if f.path and f.path.startswith("sqlglot/") and f.path.endswith(".py")]
    non_sql_files = [f for f in files if f not in sql_files]
    non_sql_inert = all(_target_unreachable(f, group) for f in non_sql_files)
    strict_all = bool(sql_files) and non_sql_inert and all(eligible_file(f, "ds") for f in sql_files)

    if group in {"tpch_opt", "tpch_exec"}:
        if not (APP_DIR / "pin-cache" / "tpch-digests.json").is_file():
            return None
        forbid_tpch = (
            ()
            if non_sql_inert
            else (
                "tests/test_executor.py",
                "tests/helpers.py",
                "tests/fixtures/optimizer/tpc-h/",
                "tests/fixtures/optimizer/tpc-ds/",
            )
        )
        if not patch_verifiable_files(
            files,
            forbid_prefixes=forbid_tpch,
            allow_structural=True,
        ):
            return None
        if not sql_files or not non_sql_inert:
            return None
        if group == "tpch_opt" and any(f.path == "sqlglot/executor/table.py" for f in sql_files):
            return None
        if group == "tpch_exec" and any(f.path.startswith("sqlglot/executor/") for f in sql_files):
            return None
        changes = _apply_verifiable(text)
        paths = ",".join(f.path for f in sql_files if changes.get(f.path) is not None)
        return group, paths, changes, []

    if group == "generator":
        forbid = () if non_sql_inert else ("tests/test_generator.py", "tests/helpers.py")
        if not patch_verifiable_files(
            files,
            forbid_prefixes=forbid,
            allow_structural=True,
        ):
            return None
        changes = _apply_verifiable(text)
        paths = ",".join(p for p, src in changes.items() if src is not None and p.startswith("sqlglot/") and p.endswith(".py"))
        if strict_all:
            regions = changed_regions(text, sqlglot_only=bool(non_sql_files))
            if regions:
                rows = probe_profiles("generator")
                if not query_slice.reached(rows["generator-setup"], regions):
                    dialects = [k[4:] for k, v in rows.items() if k.startswith("gen:") and query_slice.reached(v, regions)]
                    return "dialects", paths, changes, dialects
        if sql_files and non_sql_inert and not any(f.is_new or f.is_deleted or f.is_rename for f in sql_files):
            aff = _affected_generator_dialects(sql_files)
            if aff is not None:
                return "dialects", paths, changes, aff
        return "generator", paths, changes, []

    if group == "parser":
        forbid = () if non_sql_inert else ("tests/test_parser.py", "tests/helpers.py")
        if not patch_verifiable_files(
            files,
            forbid_prefixes=forbid,
            allow_structural=True,
        ):
            return None
        if not sql_files or not non_sql_inert:
            return None
        changes = _apply_verifiable(text)
        paths = ",".join(f.path for f in sql_files if changes.get(f.path) is not None)
        if all(_parser2_disjoint_file(f) for f in sql_files):
            return "small", paths, changes, []
        regions = changed_regions(text, sqlglot_only=bool(non_sql_files)) if strict_all else None
        if regions and not any(query_slice.reached(profile("parser")[k]["lines"], regions) for k in ("parser:setup", "parser:2")):
            return "small", paths, changes, []
        if all(eligible_file(f, "ds") and eligible_file(f, "parser") for f in sql_files):
            return "scaled", paths, changes, []
        if all(_parser_scaled_eligible(f) for f in sql_files):
            return "scaled", paths, changes, []
        if all(_parser_scaled_eligible(f, allow_monitored_loops=True) for f in sql_files):
            forbidden = {}
            for f in sql_files:
                w = _parser_new_loop_lines(f, changes.get(f.path))
                if w is None:
                    return None
                if w:
                    forbidden[f.path] = sorted(w)
            return "scaled", paths, changes, (forbidden or [])
        if all(_parser_scaled_eligible(f, allow_monitored_edits=True) for f in sql_files):
            forbidden = {}
            for f in sql_files:
                w = _parser_edited_lines(f, changes.get(f.path))
                if w is None:
                    return None
                if w:
                    forbidden[f.path] = sorted(w)
            return "scaled", paths, changes, (forbidden or [])
        return None

    # group == "ds"
    forbid_ds = (
        ()
        if non_sql_inert
        else (
            "tests/test_executor.py",
            "tests/helpers.py",
            "tests/fixtures/optimizer/tpc-ds/",
            "tests/fixtures/optimizer/tpc-h/",
        )
    )
    if not patch_verifiable_files(
        files,
        forbid_prefixes=forbid_ds,
        allow_structural=True,
    ):
        return None
    if not sql_files:
        return None
    changes = _apply_verifiable(text)
    active_sql_files = [f for f in sql_files if not _inert_new_class_attrs(f.path, changes.get(f.path), f)]
    if not active_sql_files:
        if non_sql_inert:
            return "unreached", "", changes, []
        return None
    paths = ",".join(f.path for f in active_sql_files if changes.get(f.path) is not None)

    if strict_all:
        regions = changed_regions(text, sqlglot_only=bool(non_sql_files))
        if len(files) == 1 and regions is None and token_options.corpus_edit(paths, (Path(verify.PRISTINE) / paths).read_text(), changes[paths]):
            return "tokens", paths, changes, []
        if paths == "sqlglot/tokens.py" and len(sql_files) == 1 and query_slice.keyword_edit((Path(verify.PRISTINE) / paths).read_text(), changes[paths]):
            return "tokens", paths, changes, []
        selection = query_slice.query_cases(regions) if regions else None
        if selection is not None and not selection["active"]:
            if not non_sql_files and local_edit(text):
                return "unreached", paths, changes, []
            selection["execute"] = []
            return "slice", paths, changes, selection
        if all(f.path == "sqlglot/parser.py" or f.path.startswith("sqlglot/parsers/") for f in active_sql_files):
            outside = json.loads((APP_DIR / "parse-corpus.pkl.json").read_text())
            if regions and not query_slice.reached(outside, regions):
                rows = probe_profiles("parser")
                if not any(query_slice.reached(v, regions) for v in rows.values()):
                    return "imported", paths, changes, []
                return "corpus", paths, changes, []
            if not regions and _hunks_miss_lines(active_sql_files, outside):
                return "corpus", paths, changes, []
        if selection is not None and all(p.startswith("sqlglot/optimizer/") for p in regions):
            from . import rule_corpus
            tails = downstream()
            def tail_hit(path, spans):
                scopes = impact._index()["scopes"].get(path)
                if not scopes:
                    return True
                affected_scopes = [(start, end) for kind, start, end, _, body in scopes[1]
                                   if kind == "def" and any(body <= lo <= hi <= end for lo, hi in spans)]
                if not affected_scopes:
                    return True
                return any(p == path and any(lo <= line <= hi for lo, hi in affected_scopes)
                           for row in tails for p, line in row["calls"])
            if not any(tail_hit(path, spans) for path, spans in regions.items()):
                indices = rule_corpus.select(regions)
                if indices is not None:
                    return "rules", paths, changes, indices
        if selection is not None and not selection["worker"]:
            selection["execute"] = []
            selection["pygen"] = False
            return "slice", paths, changes, selection
        infos = [tail_cases_info(f.path, changes[f.path], f, strict=True) for f in active_sql_files]
        cases = sorted(set().union(*(c for c, _ in infos)))
        needs_pygen = any(p for _, p in infos)
        if selection is not None:
            selection["execute"] = sorted(set(cases).intersection(selection["worker"]))
            selection["pygen"] = needs_pygen
            return "slice", paths, changes, selection
        # File-level worker slicing when changed_regions() is None (e.g. added
        # method or TRANSFORMS edit in generators/duckdb.py or typing/hive.py):
        # if none of the active files are in ds-setup, restrict worker optimize/plan
        # to the exact queries whose worker trace touched an active file.
        setup = ds_setup_paths()
        if all(f.path not in setup for f in active_sql_files):
            wmap = worker_path_queries()
            wqs = sorted(set().union(*(wmap.get(f.path, ()) for f in active_sql_files)))
            if len(wqs) < 57:
                sel = {
                    "active": list(range(99)),
                    "worker": wqs,
                    "execute": sorted(set(cases).intersection(wqs)),
                    "pygen": needs_pygen,
                }
                return "slice", paths, changes, sel
        return ("partial" if cases else "front"), paths, changes, (cases, needs_pygen)

    # Non-strict verifiable SQLGlot patch (dialect.py, import, __dunder__, companion tests):
    infos = [
        tail_cases_info(f.path, changes.get(f.path), f, strict=bool(eligible_file(f, "ds")))
        for f in active_sql_files
    ]
    cases = sorted(set().union(*(c for c, _ in infos)))
    needs_pygen = any(p for _, p in infos)
    setup = ds_setup_paths()
    if (
        non_sql_inert
        and all(
            not (f.is_new or f.is_deleted or f.is_rename)
            and f.path not in setup
            and _no_cross_module_hazards(f)
            for f in active_sql_files
        )
    ):
        wmap = worker_path_queries()
        wqs = sorted(set().union(*(wmap.get(f.path, ()) for f in active_sql_files)))
        if len(wqs) < 57:
            sel = {
                "active": list(range(99)),
                "worker": wqs,
                "execute": sorted(set(cases).intersection(wqs)),
                "pygen": needs_pygen,
            }
            return "slice", paths, changes, sel
    return ("partial" if cases else "front"), paths, changes, (cases, needs_pygen)


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
    heavy_mod: bool = False
    needs_pygen: bool = False

    @property
    def priority(self):
        return self.gain / max(0.5, self.estimate)


@functools.cache
def query_costs():
    with gzip.open(APP_DIR / "ds-costs.json.gz") as stream:
        rows = json.load(stream)
    return [rows[f"ds:{i}"]["phases"] for i in range(99)]


def query_price(active, worker, forced):
    phases = query_costs()
    front = 0.22 + 0.004 * len(active)
    planning = sum(phases[i].get("optimize", 0) + phases[i].get("plan", 0) for i in worker)
    execution = sum(phases[i].get("execute", 0) for i in forced)
    return front + 1.05 * (planning + execution)


def economics(mode, cases):
    if mode == "rules":
        from .rule_corpus import estimate
        return 0.55, estimate(cases)
    if mode == "slice":
        return 0.76, query_price(cases["active"], cases["worker"], cases["execute"])
    if mode in {"partial", "front"}:
        return 0.76, query_price(range(99), range(99), cases)
    if mode == "dialects":
        return 0.85, 0.16 + 0.025 * len(cases)
    return {
        "fixture-query": (0.75, 0.18),
        "unreached": (1.00, 0.005),
        "imported": (0.90, 0.09),
        "tokens": (0.60, 0.20),
        "corpus": (0.86, 0.38),
        "small": (0.75, 0.25),
        "scaled": (0.67, 0.55),
        "generator": (0.35, 1.85),
        "tpch_opt": (0.90, 0.06),
        "tpch_exec": (0.92, 0.05),
    }[mode]


def _order_bundle_cases(required_nums, catalog, dur, files):
    """Order tests for fast fail-fast: edited test files & matching module/dialect
    test files first, then shortest-first within each tier."""
    edited_tests = {f.path for f in files if f.path and f.path.startswith("tests/")}
    stems = set()
    for f in files:
        if f.path and f.path.startswith("sqlglot/"):
            stem = f.path.rsplit("/", 1)[-1].removesuffix(".py")
            if stem not in {"__init__", "core", "dialect", "base"}:
                stems.add(stem)

    def key(n):
        node = catalog[n]
        tfile = node.split("::", 1)[0]
        if tfile in edited_tests or any(f"test_{s}.py" in tfile or f"_{s}" in node for s in stems):
            tier = 0
        elif tfile in {"tests/test_executor.py", "tests/test_optimizer.py"}:
            tier = 2
        else:
            tier = 1
        return (tier, dur[n], n)

    return [catalog[n] for n in sorted(required_nums, key=key)]


# Empirically calibrated Linux-Docker CPU estimates (seconds) at ~1.22x Apple Silicon
# measurements with _InlinePool (matched to gmktech linux/amd64 Docker gate).
_MEDIUM_DOCKER_CPU = {
    "tests/test_optimizer.py::TestOptimizer::test_canonicalize": 0.06,
    "tests/test_schema.py::TestSchema::test_cache_does_not_retain_queries": 0.19,
    "tests/test_serde.py::TestSerde::test_serde": 0.21,
    "tests/test_optimize_cross_joins.py::TestOptimizeCrossJoins::test_conditional_cross_joins": 0.22,
    "tests/dialects/test_redshift.py::TestRedshift::test_values": 0.25,
    "tests/test_optimizer.py::TestOptimizer::test_unnest_subqueries": 0.26,
    "tests/dialects/test_snowflake.py::TestSnowflake::test_snowflake": 0.27,
    "tests/test_optimizer.py::TestOptimizer::test_deep_ast_type_annotation": 0.28,
    "tests/test_optimizer.py::TestOptimizer::test_tpch": 0.34,
    "tests/test_optimizer.py::TestOptimizer::test_optimize_correlated_all": 0.42,
    "tests/test_optimizer.py::TestOptimizer::test_optimize": 0.44,
    "tests/test_serde.py::TestSerde::test_recursion": 0.47,
    "tests/test_optimizer.py::TestOptimizer::test_qualify_struct_star_long_cte_chain": 0.49,
    "tests/test_optimizer.py::TestOptimizer::test_simplify": 0.53,
    "tests/test_optimizer.py::TestOptimizer::test_merge_subqueries": 0.66,
    "tests/test_optimizer.py::TestOptimizer::test_optimize_in_nulls": 0.78,
    "tests/test_optimizer.py::TestOptimizer::test_annotate_funcs": 0.90,
    "tests/test_executor.py::TestExecutor::test_agg_order": 0.40,
    "tests/test_optimizer.py::TestOptimizer::test_qualify_columns": 1.01,
    "tests/test_executor.py::TestExecutor::test_execute_tpch": 1.30,
    "tests/test_optimizer.py::TestOptimizer::test_scope": 1.36,
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": 4.20,
    "tests/test_optimizer.py::TestOptimizer::test_tpcds": 3.85,
}

# Negative-ROI or TPC-setup tests excluded from warm-fork bundle/trim.
_BUNDLE_EXCLUDE_NODES = frozenset({
    "tests/test_optimizer.py::TestOptimizer::test_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch",
}) | frozenset(TARGETS)


def _test_docker_cpu(node: str, dur_ms: int) -> float:
    known = _MEDIUM_DOCKER_CPU.get(node)
    if known is not None:
        return known
    return 0.66 * (dur_ms / 1000.0)


def _bundle_setup_cost(tfiles: set[str], cases: list[str], *, no_exec_setup: bool = False) -> tuple[float, bool]:
    uses_exec = "tests/test_executor.py" in tfiles
    uses_opt = "tests/test_optimizer.py" in tfiles
    needs_tpc_setup = any(
        n.endswith(("::test_execute_tpch", "::test_optimized_tpch", "::test_execute_tpcds"))
        for n in cases
    )
    if needs_tpc_setup:
        setup_cost = 0.65
    elif not uses_exec or no_exec_setup:
        setup_cost = 0.0
    else:
        setup_cost = 0.90
    if not setup_cost:
        for node in cases:
            parts = node.split("::")
            if len(parts) == 3:
                tpath, cls, _ = parts
                if no_exec_setup and tpath == "tests/test_executor.py":
                    continue
                scopes = impact._index()["scopes"].get(tpath)
                if scopes and any(name == cls + ".setUpClass" for _, _, _, name, _ in scopes[1]):
                    setup_cost = 0.10
                    break
    return setup_cost, (uses_exec or uses_opt)


def _estimate_bundle_set(nums, catalog, dur, *, no_exec_setup: bool = False):
    cases = [catalog[n] for n in nums]
    tfiles = {node.split("::", 1)[0] for node in cases}
    setup_cost, uses_heavy = _bundle_setup_cost(tfiles, cases, no_exec_setup=no_exec_setup)
    run_est = sum(_test_docker_cpu(catalog[n], dur[n]) for n in nums)
    est = setup_cost + (0.14 if uses_heavy else 0.0) + 0.12 + run_est
    return run_est, est, uses_heavy


def _best_trim_subset(patch, cand_nums, catalog, dur, sim_cost, *, no_exec_setup: bool = False):
    """Pick a high-ROI non-fail-fast subset S <= cand_nums for ``trim``."""
    best = None  # (score, subset_set, gain, est, uses_heavy)

    # Option 1: trim ALL eligible non-TPC required tests when compact.
    if cand_nums < patch.req and len(cand_nums) <= 350:
        run_est, est, uses_heavy = _estimate_bundle_set(
            cand_nums, catalog, dur, no_exec_setup=no_exec_setup
        )
        if run_est <= 2.8:
            rem = patch.req - cand_nums
            after = plan_cost(schedule(rem, patch.pl - cand_nums, dur), dur) / 1000 if rem else 0.0
            gain = sim_cost - after
            if gain >= 1.80 * est and gain - est >= 1.30:
                best = (gain - 1.45 * est, cand_nums, gain, est, uses_heavy)

    # Option 2: greedy high-ROI prefix (highest catalog/docker_cpu ratio first).
    ranked = []
    for n in cand_nums:
        node = catalog[n]
        c_est = _test_docker_cpu(node, dur[n])
        cat_s = dur[n] / 1000.0
        ratio = cat_s / max(0.01, c_est)
        if dur[n] >= 350 or ratio >= 1.45:
            ranked.append((ratio, dur[n], c_est, n))
    ranked.sort(key=lambda x: (-x[0], -x[1], x[3]))

    prefix = set()
    run_acc = 0.0
    for idx, (_ratio, d_ms, c_est, n) in enumerate(ranked):
        if len(prefix) >= 120 or run_acc + c_est > 2.60:
            continue
        prefix.add(n)
        run_acc += c_est
        if d_ms >= 350 or len(prefix) % 8 == 0 or idx == len(ranked) - 1:
            _, est, uses_heavy = _estimate_bundle_set(
                prefix, catalog, dur, no_exec_setup=no_exec_setup
            )
            rem = patch.req - prefix
            after = plan_cost(schedule(rem, patch.pl - prefix, dur), dur) / 1000 if rem else 0.0
            gain = sim_cost - after
            if gain >= 1.80 * est and gain - est >= 1.30:
                score = gain - 1.50 * est
                if best is None or score > best[0]:
                    best = (score, set(prefix), gain, est, uses_heavy)
    return best


def options(patch, catalog, targets, dur, attempted=()):
    if patch.full:
        if not patch.alt:
            return
        req, pl = patch.alt
        workers = schedule(req, pl, dur)
        full = plan_cost(schedule(set(dur), set(), dur), dur)
        gain = (full - plan_cost(workers, dur)) / 1000
        yield Probe(patch, "collection", None, "catalog", "", [],
                    (req, pl, workers), max(gain, 5), 0.80, 1.5, heavy_mod=True)
        return
    if not patch.req:
        return
    heavy_yielded = False
    dominant_heavy_in_req = False
    for number, group in targets:
        if number not in patch.req or number in patch.missing:
            continue
        if group in {"ds", "parser"}:
            dominant_heavy_in_req = True
        req, pl = patch.req - {number}, patch.pl - {number}
        workers = schedule(req, pl, dur) if req else []
        gain = (plan_cost(patch.workers, dur) - plan_cost(workers, dur)) / 1000
        if gain <= 0 or number in set().union(*workers):
            continue
        if group == "tpch_opt" and gain < 0.15:
            continue
        if group == "tpch_exec" and gain < 0.15:
            continue
        try:
            edit = stage_edit(patch.text, group)
            from .cheap_proofs import proven
            if group in {"ds", "parser", "generator"} and proven(patch.text, group, edit):
                heavy_yielded = True
                yield Probe(patch, group, number, "unreached", "", [], (req, pl, workers),
                            gain, 1.0, 0.005)
                continue
        except Exception:
            continue
        if edit:
            mode, paths, changes, cases = edit
            needs_pygen = False
            if mode in {"partial", "front"} and isinstance(cases, tuple) and len(cases) == 2:
                cases, needs_pygen = cases
            probability, estimate = economics(mode, cases)
            needs_heavy = False
            if group == "ds" and mode in {"front", "partial"}:
                changed_paths = {p for p in paths.split(",") if p}
                if changed_paths & ds_setup_paths():
                    estimate += 0.9
                    needs_heavy = True
            heavy_yielded = True
            yield Probe(patch, group, number, mode, paths, cases, (req, pl, workers),
                        gain, probability, estimate, heavy_mod=needs_heavy, needs_pygen=needs_pygen)

    # Warm-fork verification (bundle / trim) once no heavy probes are yielded.
    if patch.missing or heavy_yielded or dominant_heavy_in_req or not patch.workers:
        return
    files = parse(patch.text)
    sql_files = [f for f in files if f.path and f.path.startswith("sqlglot/") and f.path.endswith(".py")]
    if not sql_files:
        return
    if not patch_verifiable_files(
        files,
        forbid_prefixes=(
            "tests/helpers.py",
            "tests/test_executor.py",
            "tests/fixtures/optimizer/tpc-ds/",
            "tests/fixtures/optimizer/tpc-h/",
        ),
    ):
        return
    sql_paths = ",".join(f.path for f in sql_files)
    no_exec_setup = "sqlglot/executor/table.py" not in {f.path for f in sql_files}
    sim_cost = plan_cost(patch.workers, dur) / 1000
    if sim_cost < 2.0:
        return

    cand_nums = {
        n for n in patch.req
        if len(catalog[n].split("::")) == 3
        and catalog[n] not in _BUNDLE_EXCLUDE_NODES
        and (no_exec_setup or not catalog[n].startswith("tests/test_executor.py::"))
    }
    if not cand_nums:
        return

    has_fixture_edit = any(f.path and f.path.startswith("tests/fixtures/") for f in files)
    if has_fixture_edit:
        return

    # Path A: complete patch.req clearance (zero remaining tests on all-pass).
    if (
        cand_nums == patch.req
        and len(cand_nums) <= 550
        and (patch, "bundle") not in attempted
        and (patch, "trim") not in attempted
    ):
        run_est, est, uses_heavy = _estimate_bundle_set(
            cand_nums, catalog, dur, no_exec_setup=no_exec_setup
        )
        # Non-fail-fast trim when even a 1-failure patch yields >= 1.90x Docker ROI.
        if sim_cost >= 6.5 and run_est <= 2.8 and (sim_cost - 2.15) >= 1.90 * est:
            cases = _order_bundle_cases(cand_nums, catalog, dur, files)
            yield Probe(patch, "trim", None, "trim", sql_paths,
                        cases, (set(), set(), []), sim_cost,
                        0.85, est, heavy_mod=uses_heavy)
            return
        # Fail-fast bundle for cheaper / smaller-sim patches (aborts in ~0.2s on fail).
        is_tiny = (not uses_heavy) and est <= 0.55
        min_roi = 1.75 if is_tiny else (1.95 if uses_heavy else 1.85)
        max_run = 3.5 if sim_cost >= 6.5 else (2.4 if sim_cost >= 5.0 else 1.8)
        if run_est <= max_run and sim_cost >= min_roi * est:
            cases = _order_bundle_cases(cand_nums, catalog, dur, files)
            yield Probe(patch, "bundle", None, "bundle", sql_paths,
                        cases, (set(), set(), []), sim_cost,
                        0.45 if is_tiny else 0.40, est, heavy_mod=uses_heavy)
            return

    # Path B: high-ROI non-fail-fast subset trim for multi-worker / mixed patches.
    if (patch, "trim") not in attempted and sim_cost >= 5.5:
        pick = _best_trim_subset(
            patch, cand_nums, catalog, dur, sim_cost, no_exec_setup=no_exec_setup
        )
        if pick is not None:
            _score, sub, gain, est, uses_heavy = pick
            rem_req = patch.req - sub
            rem_pl = patch.pl - sub
            rem_workers = schedule(rem_req, rem_pl, dur) if rem_req else []
            cases = _order_bundle_cases(sub, catalog, dur, files)
            yield Probe(patch, "trim", None, "trim", sql_paths,
                        cases, (rem_req, rem_pl, rem_workers), gain,
                        0.80, est, heavy_mod=uses_heavy)


def run(patches, catalog, dur, t0, *, deadline=None):
    from .probe_session import PatchSession
    started = cpu()
    allowance = min(80.0, max(16.0, 1.35 * len(patches)))
    cpu_ceiling = max(16.0, min(55.0, 0.90 * len(patches)))
    if deadline is None:
        deadline = t0 + min(180, 3 * max(40, len(patches)))
    targets = [(t["number"], TARGETS[t["id"]]) for t in catalog if t["id"] in TARGETS]
    ids = {t["number"]: t["id"] for t in catalog}
    queue = []
    attempted = set()
    small_wins = small_tries = 0
    med_wins = med_tries = 0
    trim_wins = trim_tries = 0
    session = None

    def enqueue(patch):
        try:
            for probe in options(patch, ids, [(n, g) for n, g in targets
                                              if (patch, g) not in attempted], dur, attempted):
                if (patch, probe.group) in attempted:
                    continue
                if probe.estimate > max(1.0, probe.gain):
                    if probe.mode not in {"front", "partial", "slice"}:
                        continue
                    if probe.mode == "slice":
                        cases = probe.cases
                        cost = 0.25 + 0.004 * len(cases["active"]) + 0.03 * len(cases["worker"])
                        executed = cases["execute"]
                    else:
                        cost, executed = 2.0, probe.cases
                    cost += sum(query_costs()[i].get("execute", 0) for i in executed)
                    if cost >= probe.gain:
                        continue
                # Never admit a full 33-dialect generator probe unless gain > estimate.
                if probe.mode == "generator" and probe.gain <= probe.estimate + 0.20:
                    continue
                queue.append(probe)
        except Exception:
            return

    for patch in patches:
        enqueue(patch)
    impact._parse_pinned.cache_clear()
    print("probe-queue", len(queue), f"{cpu()-started:.4f}", file=sys.stderr)
    if any(p.mode != "unreached" for p in queue):
        warm_replay.prepare_base()
    # Pre-import numpy/pandas/duckdb ONCE in parent ONLY when >= 3 queued probes
    # truly need them, and pytest ONLY when >= 2 collection probes exist.
    if sum(1 for p in queue if p.heavy_mod) >= 3:
        warm_replay.prepare_data_libs()
    if sum(1 for p in queue if p.mode == "catalog") >= 2:
        warm_replay.prepare_pytest()
    if any(p.mode != "unreached" for p in queue):
        import gc
        gc.collect()
        gc.freeze()
    active = None
    with contextlib.ExitStack() as stack:
        while queue and time.monotonic() < deadline:
            queue.sort(key=lambda p: (p.priority, p.patch is active, p.patch.commit))
            probe = queue.pop()
            if probe.group == "trim" and trim_tries >= 4 and (2 + trim_wins) / (2 + trim_tries) < 0.40:
                continue
            if probe.group == "bundle":
                is_small = (not probe.heavy_mod) and probe.estimate <= 0.55
                if is_small:
                    p_clean = (1.0 + small_wins) / (3.0 + small_tries)
                    fail_cost = min(probe.estimate, 0.20)
                    min_net = 0.08
                else:
                    p_clean = (0.9 + med_wins) / (3.0 + med_tries)
                    fail_cost = min(probe.estimate, 0.25 if warm_replay.heavy_ready else 0.45)
                    min_net = 0.25
                expected_net = p_clean * (probe.gain - probe.estimate) - (1.0 - p_clean) * fail_cost
                if expected_net <= min_net:
                    continue
            if probe.group == "collection":
                cpu_limit = min(8.5, max(3.5, 2.2 * probe.estimate + 1.0))
            elif probe.group == "ds":
                if probe.mode == "rules":
                    # verify-results executes affected TPC-DS queries when rule output changes.
                    cpu_limit = min(6.5, max(2.8, 0.50 * probe.gain))
                else:
                    ds_cap = min(11.5, max(6.5, 0.85 * probe.gain))
                    cpu_limit = min(ds_cap, max(2.8, 2.2 * probe.estimate + 1.0))
            elif probe.group in {"bundle", "trim"}:
                cpu_limit = min(6.5, max(2.0, 2.0 * probe.estimate + 1.0))
            else:
                cpu_limit = min(6.0, max(2.0, 2.4 * probe.estimate + 1.0))
            # Skip ds probes that require heavy forced execution exceeding cpu_limit.
            if probe.mode in {"partial", "slice"}:
                forced = probe.cases["execute"] if probe.mode == "slice" else probe.cases
                min_exec = sum(query_costs()[i].get("execute", 0) for i in forced)
                if min_exec > 0.92 * cpu_limit:
                    continue
            patch = probe.patch
            if probe.group != "collection" and probe.mode != "unreached":
                if probe.estimate > allowance:
                    continue
                allowance -= probe.estimate
            began = cpu() + (session.charged if session else 0)
            sim_before = plan_cost(patch.workers, dur) / 1000 if patch.workers else 0.0
            phase = probe.mode
            attempted.add((patch, probe.group))
            try:
                if active is not patch and probe.mode != "unreached":
                    stack.close()
                    session = None
                    active = None
                    work = stack.enter_context(tempfile.TemporaryDirectory())
                    changes = _apply_verifiable(patch.text)
                    verify.build_overlay(work, changes)
                    active = patch
                    modules = ",".join(
                        impact._module_name(p) for p in changes
                        if p.startswith("sqlglot/") and p.endswith(".py")
                    )
                    other_same = [
                        p for p in queue
                        if p.patch is patch and p.group not in {"collection", "generator"} and p.mode != "unreached"
                    ]
                    if (probe.group not in {"collection", "generator"} and other_same
                            and modules and warm_replay.compatible(modules) and changed_regions(patch.text)):
                        session = PatchSession(work, probe.paths)
                        stack.callback(session.close)
                if probe.mode == "catalog" and not warm_replay.pytest_ready:
                    if warm_replay.prepare_pytest():
                        import gc
                        gc.collect()
                        gc.freeze()
                if probe.heavy_mod and not warm_replay.heavy_ready and sum(1 for p in queue if p.heavy_mod) >= 2:
                    if warm_replay.prepare_data_libs():
                        import gc
                        gc.collect()
                        gc.freeze()
                args = ([json.dumps(probe.cases)] if (
                    probe.mode in {"partial", "slice", "rules", "dialects", "bundle", "trim"}
                    or (probe.mode == "scaled" and probe.cases)
                ) else [])
                if probe.mode == "rules":
                    args.append("verify-results")
                elif probe.mode in {"front", "partial"}:
                    args.append("pygen" if probe.needs_pygen else "no-pygen")
                elif probe.mode in {"bundle", "trim"} and "sqlglot/executor/table.py" not in probe.paths.split(","):
                    args.append("no-exec-setup")
                remaining_cpu = cpu_ceiling - (cpu() - started) - (session.charged if session else 0)
                if remaining_cpu < 0.1 and probe.mode != "unreached":
                    break
                cpu_limit = min(cpu_limit, max(0.1, remaining_cpu))
                if probe.mode == "unreached":
                    result = "pass"
                elif session and probe.group not in {"collection", "generator"}:
                    result, phase = session.check(probe.mode, args, deadline, cpu_limit)
                else:
                    replay.phase = phase
                    result = replay(probe.mode, args, work, deadline, probe.paths, cpu_limit)
                    phase = replay.phase
                if result == "failed" and probe.mode in {"corpus", "tokens"}:
                    files = parse(patch.text)
                    sql_files = [f for f in files if f.path and f.path.startswith("sqlglot/") and f.path.endswith(".py")]
                    regions = changed_regions(patch.text, sqlglot_only=len(sql_files) < len(files))
                    selection = query_slice.query_cases(regions) if regions else None
                    if selection is None:
                        infos = [tail_cases_info(f.path, changes.get(f.path), f, strict=bool(eligible_file(f, "ds"))) for f in sql_files]
                        forced = sorted(set().union(*(c for c, _ in infos)))
                        fb_pygen = any(p for _, p in infos)
                        mode = "partial" if forced else "front"
                        args = ([json.dumps(forced)] if forced else []) + ["pygen" if fb_pygen else "no-pygen"]
                        price = query_price(range(99), range(99), forced)
                    else:
                        infos = [tail_cases_info(f.path, changes[f.path], f, strict=True) for f in sql_files]
                        forced = set().union(*(c for c, _ in infos))
                        selection["execute"] = sorted(forced.intersection(selection["worker"]))
                        selection["pygen"] = any(p for _, p in infos)
                        mode, args = "slice", [json.dumps(selection)]
                        price = economics(mode, selection)[1]
                    limit = min(8.5, max(3.0, 2.2 * price + 1.0),
                                cpu_ceiling - (cpu() - started) - (session.charged if session else 0))
                    if price <= min(allowance, probe.gain, 0.92 * limit) and limit >= 0.1:
                        allowance -= price
                        if session:
                            result, phase = session.check(mode, args, deadline, limit)
                        else:
                            result = replay(mode, args, work, deadline, probe.paths, limit)
                            phase = replay.phase
                if result == "pass":
                    if probe.mode == "catalog":
                        missing = json.loads((Path(work) / ".blast-missing.json").read_text())
                        if not isinstance(missing, list) or any(type(n) is not int or n not in dur for n in missing):
                            raise ValueError("invalid collection result")
                        req, pl = patch.alt
                        patch.missing = set(missing)
                        req = req.union(patch.missing)
                        patch.req, patch.pl, patch.workers = req, pl - req, schedule(req, pl - req, dur)
                    elif probe.mode in {"bundle", "trim"} and (Path(work) / ".blast-passed.json").is_file():
                        passed = json.loads((Path(work) / ".blast-passed.json").read_text())
                        if not isinstance(passed, list) or not set(passed) < set(probe.cases):
                            raise ValueError("invalid partial bundle receipt")
                        passed_set = set(passed)
                        passed_nums = {n for n, node in ids.items() if node in passed_set}
                        patch.req = patch.req - passed_nums
                        patch.pl = patch.pl - passed_nums
                        patch.workers = schedule(patch.req, patch.pl, dur) if patch.req else []
                        result = "partial"
                    else:
                        patch.req, patch.pl, patch.workers = probe.proposed
                    patch.full = False
                    queue[:] = [p for p in queue if p.patch is not patch]
                    enqueue(patch)
                elif probe.group in {"tpch_opt", "tpch_exec", "generator"} and not any(
                    p.patch is patch for p in queue
                ):
                    enqueue(patch)
            except Exception as exc:
                result = type(exc).__name__
            if result == "timeout" and probe.group != "collection":
                allowance -= max(0.0, cpu_limit - probe.estimate)
            if probe.group == "bundle":
                is_small = (not probe.heavy_mod) and probe.estimate <= 0.65
                if is_small:
                    small_tries += 1
                    small_wins += result == "pass"
                else:
                    med_tries += 1
                    med_wins += result == "pass"
            cost = cpu() + (session.charged if session else 0) - began
            sim_after = plan_cost(patch.workers, dur) / 1000 if patch.workers else 0.0
            realized_gain = max(0.0, sim_before - sim_after) if result in {"pass", "partial"} else 0.0
            if probe.group == "trim":
                trim_tries += 1
                trim_wins += realized_gain > cost
            print("heavy", patch.commit, probe.group, result, probe.mode, f"{cost:.4f}",
                  round(1000 * realized_gain) if probe.group != "collection" else 0,
                  phase, f"{probe.probability:.2f}", file=sys.stderr)
    print("probe-cpu", f"{cpu()-started:.4f}", file=sys.stderr)


def bounded_run(patches, catalog, dur, t0, *, deadline=None):
    """Keep static plans in the parent until the verifier returns a complete batch."""
    import multiprocessing

    allowance = min(180, 3 * max(40, len(patches)))
    deadline = min(deadline, t0 + allowance) if deadline is not None else t0 + allowance
    if time.monotonic() >= deadline:
        return
    with tempfile.TemporaryFile(mode="w+") as result:
        def work():
            set_memory_limit(1800)
            # Leave a 4s flush window so completed proofs are never lost at the deadline.
            run(patches, catalog, dur, t0, deadline=deadline - 4.0)
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
