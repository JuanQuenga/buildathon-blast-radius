"""Generate source-only mutations and label each with both original heavy tests."""

import argparse
import ast
import collections
import difflib
import itertools
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selector.verify import build_overlay

TARGETS = [
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
]
FILES = [
    "tokens.py",
    "parser.py",
    "optimizer/qualify_columns.py",
    "planner.py",
    "executor/python.py",
    "generator.py",
    "dialects/dialect.py",
]


def mutations(root):
    rng = random.Random(611)
    buckets = []
    for name in FILES:
        path = "sqlglot/" + name
        src = (root / path).read_text().splitlines(True)
        options = collections.defaultdict(list)
        for node in ast.walk(ast.parse("".join(src))):
            if not hasattr(node, "lineno") or node.lineno != node.end_lineno:
                continue
            line = src[node.lineno - 1]
            if not line.isascii():
                continue
            before = line[node.col_offset : node.end_col_offset]
            if isinstance(node, ast.Constant) and type(node.value) is bool:
                replacement = str(not node.value)
            elif isinstance(node, ast.Compare) and len(node.ops) == 1:
                replacement = "(not (" + before + "))"
            elif isinstance(node, ast.Dict) and node.keys:
                replacement = "{}"
            elif isinstance(node, ast.Attribute) and before.startswith("TokenType."):
                replacement = "TokenType.UNKNOWN"
            else:
                continue
            if replacement != before:
                options[type(node).__name__].append(
                    (node.lineno - 1, node.col_offset, node.end_col_offset, replacement)
                )
        for entries in options.values():
            rng.shuffle(entries)
            buckets.append((path, src, entries))
    seen = set()
    while any(entries for _, _, entries in buckets):
        for path, src, entries in buckets:
            if not entries:
                continue
            index, start, end, replacement = entries.pop()
            changed = src.copy()
            changed[index] = src[index][:start] + replacement + src[index][end:]
            patch = f"diff --git a/{path} b/{path}\n" + "".join(
                difflib.unified_diff(
                    src, changed, fromfile="a/" + path, tofile="b/" + path
                )
            )
            if patch not in seen:
                seen.add(patch)
                yield patch, {path: "".join(changed)}


def hot_mutations(root, python):
    # Trace a small independently authored SQL workload to target executed
    # bodies. These sites are inputs to the offline oracle only.
    trace = """
import collections, json, sys
from pathlib import Path
import sqlglot
from sqlglot.executor import execute
base = str(Path.cwd()) + "/"
hits = collections.Counter()
def line(code,n):
    if code.co_filename.startswith(base+"sqlglot/"):
        hits[code.co_filename[len(base):],n] += 1
    return sys.monitoring.DISABLE
sys.monitoring.use_tool_id(3,"generate")
sys.monitoring.register_callback(3,sys.monitoring.events.LINE,line)
sys.monitoring.set_events(3,sys.monitoring.events.LINE)
for sql in ("SELECT 1","SELECT x=x=x, x=2 FROM t WHERE x >= 1 ORDER BY x",
            "SELECT x, COUNT(*) FROM t GROUP BY x ORDER BY x"):
    expr=sqlglot.parse_one(sql)
    expr.sql(dialect="duckdb")
    execute(expr, tables={"t":[{"x":1},{"x":2}]})
sys.monitoring.set_events(3,0)
print(json.dumps(list(hits)))
"""
    sites = json.loads(subprocess.check_output([python, "-c", trace], cwd=root))
    covered = collections.defaultdict(set)
    for path, n in sites:
        covered[path].add(n)
    rng = random.Random(727)
    buckets = []
    for name in [*FILES, "tokenizer_core.py"]:
        path = "sqlglot/" + name
        src = (root / path).read_text().splitlines(True)
        rows = []
        for node in ast.walk(ast.parse("".join(src))):
            if getattr(node, "lineno", 0) not in covered[path]:
                continue
            if not isinstance(node, (ast.Return, ast.If)) or node.lineno != getattr(
                node.value if isinstance(node, ast.Return) else node.test,
                "end_lineno",
                0,
            ):
                continue
            value = node.value if isinstance(node, ast.Return) else node.test
            if value is None or value.lineno != node.lineno:
                continue
            line = src[node.lineno - 1]
            if not line.isascii():
                continue
            previous = line[value.col_offset : value.end_col_offset]
            replacement = "None" if isinstance(node, ast.Return) else "False"
            if previous in {"None", "False", "True"}:
                continue
            # A condition that never fires can suppress advancement and hang
            # the tokenizer. Mutate return values or use an immediate failure.
            if isinstance(node, ast.If):
                replacement = "(_ for _ in ()).throw(ValueError('mutation'))"
            changed = src.copy()
            changed[node.lineno - 1] = (
                line[: value.col_offset] + replacement + line[value.end_col_offset :]
            )
            patch = f"diff --git a/{path} b/{path}\n" + "".join(
                difflib.unified_diff(
                    src, changed, fromfile="a/" + path, tofile="b/" + path
                )
            )
            rows.append((patch, {path: "".join(changed)}))
        rng.shuffle(rows)
        buckets.append(rows)
    for group in itertools.zip_longest(*buckets):
        yield from (row for row in group if row is not None)


def targeted_mutations(root):
    # Threshold changes distinguish real large-only failures from the scaled
    # probe. Dispatch-table replacements exercise import-time evaluation.
    cases = [
        (
            "parser.py",
            "if self._node_count > self.max_nodes:",
            "if self._node_count == 40_000:\n                raise ValueError('node budget')\n            if self._node_count > self.max_nodes:",
        ),
        (
            "parser.py",
            'f"Maximum number of AST nodes ({self.max_nodes}) exceeded"',
            '("out of nodes" if self.max_nodes >= 50_000 else f"Maximum number of AST nodes ({self.max_nodes}) exceeded")',
        ),
        ("tokens.py", '",": TokenType.COMMA,', '",": TokenType.R_PAREN,'),
        ("tokens.py", '"SELECT": TokenType.SELECT,', '"SELECT": TokenType.VAR,'),
        ("tokens.py", '"FROM": TokenType.FROM,', '"FROM": TokenType.VAR,'),
        ("parser.py", "TokenType.EQ: exp.EQ,", "TokenType.EQ: exp.NEQ,"),
        ("parser.py", "TokenType.PLUS: exp.Add,", "TokenType.PLUS: exp.Sub,"),
    ]
    for name, old, new in cases:
        path = "sqlglot/" + name
        before = (root / path).read_text()
        if before.count(old) != 1:
            raise ValueError(f"unexpected pin at {path}")
        after = before.replace(old, new)
        patch = f"diff --git a/{path} b/{path}\n" + "".join(
            difflib.unified_diff(
                before.splitlines(True),
                after.splitlines(True),
                fromfile="a/" + path,
                tofile="b/" + path,
            )
        )
        yield patch, {path: after}


def main():
    ap = argparse.ArgumentParser()
    for name in ("pin", "python", "output"):
        ap.add_argument(name, type=Path)
    ap.add_argument("--count", type=int, default=100)
    args = ap.parse_args()
    root, python, out = (p.absolute() for p in (args.pin, args.python, args.output))
    out.mkdir(exist_ok=True, parents=True)
    broad = mutations(root)
    # Keep broad controls, then concentrate on the instrumented code paths.
    stream = itertools.chain(
        itertools.islice(broad, 20),
        targeted_mutations(root),
        hot_mutations(root, python),
        broad,
    )
    for i, (patch, changes) in enumerate(stream):
        if i >= args.count:
            break
        stem = out / f"m{i:03}"
        patchfile = stem.with_suffix(".patch")
        if patchfile.exists() and patchfile.read_text() != patch:
            raise RuntimeError(f"mutation seed differs at {stem}")
        patchfile.write_text(patch)
        if stem.with_suffix(".json").exists():
            continue
        labels = {}
        with tempfile.TemporaryDirectory() as tmp:
            build_overlay(tmp, changes, str(root))
            for target in TARGETS:
                env = dict(
                    os.environ,
                    PYTHONPATH=tmp,
                    PYTHON_CPU_COUNT="1",
                    OPENBLAS_NUM_THREADS="1",
                    OMP_NUM_THREADS="1",
                    SKIP_INTEGRATION="0",
                    PYTHONDONTWRITEBYTECODE="1",
                )
                try:
                    with subprocess.Popen(
                        [
                            python,
                            "-m",
                            "pytest",
                            "-xq",
                            "-p",
                            "no:cacheprovider",
                            target,
                        ],
                        cwd=tmp,
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    ) as child:
                        try:
                            output, _ = child.communicate(timeout=180)
                        except subprocess.TimeoutExpired:
                            os.killpg(child.pid, signal.SIGKILL)
                            child.communicate()
                            raise
                    stem.with_suffix("." + target.split("::")[-1] + ".log").write_bytes(
                        output
                    )
                    labels[target] = {0: "pass", 1: "fail"}.get(
                        child.returncode, "unlabelled"
                    )
                    if (
                        child.returncode in (2, 4)
                        and b"ERROR collecting " + target.split("::")[0].encode()
                        in output
                    ):
                        labels[target] = "fail"
                except subprocess.TimeoutExpired:
                    labels[target] = "unlabelled"
        stem.with_suffix(".json").write_text(json.dumps(labels))


if __name__ == "__main__":
    main()
