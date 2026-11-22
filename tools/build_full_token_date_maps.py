"""Build full 1,620-test keyword-reads.json.gz and date-reads.json.gz in parallel.

Uses the exact same AST instrumentation as tools/keyword_trace and tools/date_trace,
plus pytest --collect-only union and _InlinePool so TPC-DS/TPC-H workers are tracked.
"""
from __future__ import annotations

import ast
import builtins
import concurrent.futures as cf
import concurrent.futures.process as cfp
import gzip
import importlib
from importlib.machinery import SourceFileLoader
import json
import os
import pickle
import subprocess
import sys
import tempfile
import time
import unittest
from collections import defaultdict
from pathlib import Path


def _install_inline_pool():
    class _InlinePool:
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
            if initializer is not None:
                fn, args = pickle.loads(pickle.dumps((initializer, tuple(initargs))))
                fn(*args)

        def submit(self, fn, /, *args, **kwargs):
            f = cf.Future()
            try:
                fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
                f.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
            except BaseException as e:
                if not isinstance(e, Exception):
                    raise
                f.set_exception(e)
            return f

        def map(self, fn, *iterables, timeout=None, chunksize=1):
            return [self.submit(fn, *a).result() for a in zip(*iterables)]

        def shutdown(self, wait=True, *, cancel_futures=False):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    cf.ProcessPoolExecutor = _InlinePool
    cfp.ProcessPoolExecutor = _InlinePool


def _install_hooks(cur_kw: set[str], cur_date: set[str], active: list[bool]):
    original = SourceFileLoader.get_code

    def observe_kw(table, key):
        if active[0] and (
            key in table
            or (isinstance(key, str) and len(key) <= 64 and key.isascii() and key.isidentifier())
        ):
            cur_kw.add(key)
        return key

    def observe_date(key):
        if not isinstance(key, str):
            raise TypeError("unexpected interval lookup")
        if active[0]:
            cur_date.add(key)
        return key

    def instrument(loader, name):
        if name == "sqlglot.tokenizer_core":
            tree = ast.parse(loader.get_data(loader.path), loader.path)
            count = 0
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.In):
                    obj, key = node.comparators[0], node.left
                elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
                    obj, key = node.value, node.slice
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and node.args
                ):
                    obj, key = node.func.value, node.args[0]
                else:
                    continue
                if not (
                    (isinstance(obj, ast.Name) and obj.id == "keywords")
                    or (isinstance(obj, ast.Attribute) and obj.attr == "keywords")
                ):
                    continue
                wrapped = ast.copy_location(
                    ast.Call(ast.Name("_br_keyword_read", ast.Load()), [obj, key], []), key
                )
                if isinstance(node, ast.Compare):
                    node.left = wrapped
                elif isinstance(node, ast.Subscript):
                    node.slice = wrapped
                else:
                    node.args[0] = wrapped
                count += 1
            assert count == 6, f"unexpected keyword hook count {count}"
            return compile(ast.fix_missing_locations(tree), loader.path, "exec", dont_inherit=True)

        if name in {"sqlglot.parser", "sqlglot.dialects.dialect"}:
            tree = ast.parse(loader.get_data(loader.path), loader.path)
            count = 0
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Compare)
                    and len(node.ops) == 1
                    and isinstance(node.ops[0], (ast.In, ast.NotIn))
                    and isinstance(node.comparators[0], ast.Attribute)
                    and node.comparators[0].attr == "VALID_INTERVAL_UNITS"
                ):
                    key = node.left
                    node.left = ast.copy_location(
                        ast.Call(ast.Name("_br_date_read", ast.Load()), [key], []), key
                    )
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "DATE_PART_MAPPING"
                ):
                    key = node.args[0]
                    node.args[0] = ast.copy_location(
                        ast.Call(ast.Name("_br_date_read", ast.Load()), [key], []), key
                    )
                else:
                    continue
                count += 1
            assert count == (4 if name == "sqlglot.parser" else 1), f"unexpected date hook count {name}: {count}"
            return compile(ast.fix_missing_locations(tree), loader.path, "exec", dont_inherit=True)

        return original(loader, name)

    builtins._br_keyword_read = observe_kw
    builtins._br_date_read = observe_date
    SourceFileLoader.get_code = instrument


def _worker_run(args):
    pin_dir, items = args
    sys.path.insert(0, pin_dir)
    os.environ["SKIP_INTEGRATION"] = "0"
    _install_inline_pool()

    cur_kw: set[str] = set()
    cur_date: set[str] = set()
    active = [False]
    _install_hooks(cur_kw, cur_date, active)

    by_cls = defaultdict(list)
    for tid in items:
        parts = tid.split("::")
        if len(parts) == 3:
            by_cls[(parts[0], parts[1])].append(tid)

    kw_out = {}
    date_out = {}
    for (fpath, cname), group in by_cls.items():
        try:
            mod = importlib.import_module(fpath[:-3].replace("/", "."))
            cls = getattr(mod, cname)
        except Exception:
            continue
        cur_kw.clear()
        cur_date.clear()
        active[0] = True
        setup_ok = True
        try:
            cls.setUpClass()
        except Exception:
            setup_ok = False
        finally:
            active[0] = False
        setup_kw = set(cur_kw)
        setup_date = set(cur_date)
        try:
            for tid in group:
                if not setup_ok:
                    continue
                method = tid.split("::")[2]
                cur_kw.clear()
                cur_date.clear()
                active[0] = True
                ok = True
                try:
                    case = cls(method)
                    res = unittest.TestResult()
                    case.run(res)
                    ok = res.wasSuccessful() and not res.skipped
                except Exception:
                    ok = False
                finally:
                    active[0] = False
                if ok:
                    kw_out[tid] = sorted(setup_kw | cur_kw)
                    date_out[tid] = sorted(setup_date | cur_date)
        finally:
            try:
                cls.tearDownClass()
            except Exception:
                pass
    return kw_out, date_out


def _collect_only(pin_dir: str, family: str) -> set[str]:
    trace_var = "BR_" + family.upper() + "_TRACE"
    tools_dir = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as work:
        env = dict(
            os.environ,
            **{trace_var: work},
            PYTHON_CPU_COUNT="1",
            PYTHONPATH=f"{tools_dir / (family + '_trace')}:{pin_dir}",
        )
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--collect-only", "tests"],
            cwd=pin_dir,
            env=env,
            capture_output=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr.decode()
        keys = set()
        for f in sorted(Path(work).glob("*.json")):
            installed, obs = json.loads(f.read_text())
            assert installed
            keys.update(obs)
        return keys


def main():
    t0 = time.monotonic()
    pin_dir = os.environ.get(
        "BLAST_SQLGLOT",
        "/Users/juan/.t3/scratch/2026-11-21-act-as-the-research-sub-c3678457/upstream/pin",
    )
    cat_path = Path(
        os.environ.get(
            "BLAST_CATALOG",
            "/Users/juan/.t3/scratch/2026-11-21-act-as-the-research-sub-c3678457/sample/sample/tests.json",
        )
    )
    cat = json.loads(cat_path.read_text())["tests"]

    coll_kw = _collect_only(pin_dir, "keyword")
    coll_date = _collect_only(pin_dir, "date")
    print(f"Collection baseline: kw={len(coll_kw)}, date={len(coll_date)}")

    by_cls = defaultdict(list)
    dur = {t["id"]: float(t["seconds"]) for t in cat}
    for t in cat:
        parts = t["id"].split("::")
        if len(parts) == 3:
            by_cls[(parts[0], parts[1])].append(t["id"])
    groups = sorted(by_cls.values(), key=lambda g: -sum(dur[tid] for tid in g))
    n_workers = 6
    buckets = [[] for _ in range(n_workers)]
    loads = [0.0] * n_workers
    for g in groups:
        b = min(range(n_workers), key=loads.__getitem__)
        buckets[b].extend(g)
        loads[b] += sum(dur[tid] for tid in g)

    kw_profiles = {}
    date_profiles = {}
    with cf.ProcessPoolExecutor(max_workers=n_workers) as pool:
        for k_part, d_part in pool.map(_worker_run, [(pin_dir, b) for b in buckets if b]):
            for tid, vals in k_part.items():
                kw_profiles[tid] = sorted(coll_kw | set(vals))
            for tid, vals in d_part.items():
                date_profiles[tid] = sorted(coll_date | set(vals))

    # Reuse excluded & single from existing keyword-reads.json
    maps_dir = Path(__file__).resolve().parents[1] / "selector" / "maps"
    old_kw = json.loads((maps_dir / "keyword-reads.json").read_text())
    kw_payload = dict(profiles=kw_profiles, excluded=old_kw["excluded"], single=old_kw["single"])
    kw_bytes = gzip.compress(json.dumps(kw_payload, separators=(",", ":")).encode(), compresslevel=9)
    (maps_dir / "keyword-reads.json.gz").write_bytes(kw_bytes)

    date_bytes = gzip.compress(json.dumps(date_profiles, separators=(",", ":")).encode(), compresslevel=9)
    (maps_dir / "date-reads.json.gz").write_bytes(date_bytes)
    print(
        f"Built keyword-reads.json.gz ({len(kw_bytes):,}B, {len(kw_profiles)} tests) and "
        f"date-reads.json.gz ({len(date_bytes):,}B, {len(date_profiles)} tests) in {time.monotonic()-t0:.1f}s"
    )


if __name__ == "__main__":
    main()
