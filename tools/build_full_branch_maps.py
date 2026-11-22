"""Build full ~1,619-test inverted branch and operand maps in parallel.

Uses the exact AST instrumentation of tools/branch_trace/sitecustomize.py,
plus pytest --collect-only baseline and _InlinePool so spawned executor workers
(TPC-DS / TPC-H) are captured in-process.

Outputs:
  - selector/maps/branches.json.gz  (compact inverted site -> [always_0_idxs, always_1_idxs, both_idxs])
  - selector/maps/operands.json.gz  (compact site -> [[test_idxs, concrete_values], ...])
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


class _Conditions(ast.NodeTransformer):
    def __init__(self, module: str):
        self.module = module

    def wrap(self, value, key):
        return ast.copy_location(
            ast.Call(ast.Name("_br_branch_truth", ast.Load()), [ast.Constant(key), value], []),
            value,
        )

    def visit_Compare(self, node):
        self.generic_visit(node)
        if (
            len(node.ops) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and isinstance(node.ops[0], (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE))
        ):
            site = f"{self.module}:{node.lineno}:{node.col_offset}"
            left = node.left
            node.left = ast.copy_location(
                ast.Call(ast.Name("_br_operand", ast.Load()), [ast.Constant(site), left], []),
                left,
            )
        return node

    def visit_If(self, node):
        self.generic_visit(node)
        test = node.test
        site = f"{self.module}:{test.lineno}:{test.col_offset}"
        if isinstance(test, ast.BoolOp):
            test.values = [
                self.wrap(v, site + (":tail" if i == len(test.values) - 1 else f":{i}"))
                for i, v in enumerate(test.values)
            ]
        node.test = self.wrap(test, site)
        return node

    visit_While = visit_If


def _install_hooks(cur_0: set[str], cur_1: set[str], cur_ops: dict, active: list[bool]):
    original = SourceFileLoader.get_code

    def observe(site, value):
        truth = bool(value)
        if active[0]:
            if truth:
                cur_1.add(site)
            else:
                cur_0.add(site)
        return truth

    def operand(site, value):
        if active[0]:
            observed = cur_ops.get(site, False)
            if observed is not None:
                if observed is False:
                    observed = set()
                    cur_ops[site] = observed
                if type(value) not in (str, int, bool, type(None)) or (
                    type(value) is str and len(value) > 80
                ):
                    cur_ops[site] = None
                else:
                    observed.add((type(value).__name__, value))
                    if len(observed) > 64:
                        cur_ops[site] = None
        return value

    def instrument(loader, fullname):
        if not fullname.startswith("sqlglot."):
            return original(loader, fullname)
        tree = ast.parse(loader.get_data(loader.path), loader.path)
        _Conditions(fullname).visit(tree)
        return compile(ast.fix_missing_locations(tree), loader.path, "exec", dont_inherit=True)

    builtins._br_branch_truth = observe
    builtins._br_operand = operand
    SourceFileLoader.get_code = instrument


def _merge_ops(a: dict, b: dict) -> dict:
    out = {}
    for k in a.keys() | b.keys():
        va = a.get(k, False)
        vb = b.get(k, False)
        if va is None or vb is None:
            out[k] = None
        else:
            s = set()
            if va is not False:
                s.update(va)
            if vb is not False:
                s.update(vb)
            out[k] = None if len(s) > 64 else s
    return out


def _worker_run(args):
    pin_dir, items, coll_0, coll_1, coll_ops = args
    sys.path.insert(0, pin_dir)
    os.environ["SKIP_INTEGRATION"] = "0"
    _install_inline_pool()

    cur_0: set[str] = set()
    cur_1: set[str] = set()
    cur_ops: dict = {}
    active = [False]
    _install_hooks(cur_0, cur_1, cur_ops, active)

    by_cls = defaultdict(list)
    for tid in items:
        parts = tid.split("::")
        if len(parts) == 3:
            by_cls[(parts[0], parts[1])].append(tid)

    # Return per-worker inverted structures directly to minimize IPC size.
    # test_id -> (always_0_sites, always_1_sites, both_tail_sites, ops_dict)
    out = {}
    for (fpath, cname), group in by_cls.items():
        try:
            mod = importlib.import_module(fpath[:-3].replace("/", "."))
            cls = getattr(mod, cname)
        except Exception:
            continue
        cur_0.clear()
        cur_1.clear()
        cur_ops.clear()
        active[0] = True
        setup_ok = True
        try:
            cls.setUpClass()
        except Exception:
            setup_ok = False
        finally:
            active[0] = False
        setup_0 = set(cur_0)
        setup_1 = set(cur_1)
        setup_ops = {k: (None if v is None else set(v)) for k, v in cur_ops.items()}
        try:
            for tid in group:
                if not setup_ok:
                    continue
                if tid in {
                    "tests/dialects/test_dialect.py::TestDialect::test_lazy_load",
                    "tests/dialects/test_sqlite.py::TestSQLite::test_variadic_iif_execution",
                }:
                    continue
                method = tid.split("::")[2]
                cur_0.clear()
                cur_1.clear()
                cur_ops.clear()
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
                    all_0 = coll_0 | setup_0 | cur_0
                    all_1 = coll_1 | setup_1 | cur_1
                    a0 = all_0 - all_1
                    a1 = all_1 - all_0
                    both_tail = {s for s in (all_0 & all_1) if s.endswith(":tail")}
                    merged_ops = _merge_ops(_merge_ops(coll_ops, setup_ops), cur_ops)
                    ops_clean = {
                        k: [val for _, val in sorted(v)]
                        for k, v in merged_ops.items()
                        if v
                    }
                    out[tid] = (sorted(a0), sorted(a1), sorted(both_tail), ops_clean)
        finally:
            try:
                cls.tearDownClass()
            except Exception:
                pass
    return out


def _collect_only(pin_dir: str):
    tools_dir = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as work:
        env = dict(
            os.environ,
            BR_BRANCH_OUT=work,
            PYTHON_CPU_COUNT="1",
            SKIP_INTEGRATION="0",
            PYTHONDONTWRITEBYTECODE="1",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            PYTHONPATH=f"{tools_dir / 'branch_trace'}:{pin_dir}",
        )
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--collect-only", "tests"],
            cwd=pin_dir,
            env=env,
            capture_output=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr.decode()
        c0, c1 = set(), set()
        for p in Path(work).iterdir():
            if p.suffix == ".0":
                c0.add(p.stem)
            elif p.suffix == ".1":
                c1.add(p.stem)
        cops = {}
        for f in Path(work).glob("operands-*.json"):
            for site, items in json.loads(f.read_text()).items():
                prev = cops.setdefault(site, set())
                if prev is None or items is None:
                    cops[site] = None
                else:
                    prev.update(tuple(pair) for pair in items)
                    if len(prev) > 64:
                        cops[site] = None
        return c0, c1, cops


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
    maps_dir = Path(__file__).resolve().parents[1] / "selector" / "maps"

    # Load existing pinned single-process profiles for the 2 heavy tests so we
    # never lose a single site or relax anything vs the original single-process runs.
    old_branches = json.loads((maps_dir / "branches.json").read_text())
    old_operands = json.loads((maps_dir / "operands.json").read_text())

    coll_0, coll_1, coll_ops = _collect_only(pin_dir)
    print(f"Collection baseline: b0={len(coll_0)}, b1={len(coll_1)}, ops={len(coll_ops)}", flush=True)

    # Index order must match selector/impact.py _index()["tests"]!
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from selector import impact

    idx_tests = impact._index()["tests"]
    tid_to_i = {t: i for i, t in enumerate(idx_tests)}
    dur = {t["id"]: float(t["seconds"]) for t in cat}

    by_cls = defaultdict(list)
    for t in idx_tests:
        if t in old_branches:
            # Keep the exact single-process heavy-test profiles for these 2 tests!
            continue
        parts = t.split("::")
        if len(parts) == 3:
            by_cls[(parts[0], parts[1])].append(t)

    groups = sorted(by_cls.values(), key=lambda g: -sum(dur.get(tid, 0.01) for tid in g))
    n_workers = 6
    buckets = [[] for _ in range(n_workers)]
    loads = [0.0] * n_workers
    for g in groups:
        b = min(range(n_workers), key=loads.__getitem__)
        buckets[b].extend(g)
        loads[b] += sum(dur.get(tid, 0.01) for tid in g)

    # Inverted structures:
    # b_sites[site] = [sorted_always_0_idxs, sorted_always_1_idxs]
    # tail_both[site] = sorted_both_idxs (only for :tail sites that saw both 0 and 1)
    # op_groups[site][vals_json] = sorted_test_idxs
    b0_map = defaultdict(list)
    b1_map = defaultdict(list)
    btail_map = defaultdict(list)
    op_groups = defaultdict(lambda: defaultdict(list))
    profiled = set()

    # Seed the 2 heavy tests from their exact single-process profiles.
    for tid, sites in old_branches.items():
        if tid not in tid_to_i:
            continue
        ti = tid_to_i[tid]
        profiled.add(tid)
        for s, val in sites.items():
            if val == 0:
                b0_map[s].append(ti)
            else:
                b1_map[s].append(ti)
    for tid, ops in old_operands.items():
        if tid not in tid_to_i:
            continue
        ti = tid_to_i[tid]
        for s, vals in ops.items():
            key = json.dumps(vals, separators=(",", ":"))
            op_groups[s][key].append(ti)

    with cf.ProcessPoolExecutor(max_workers=n_workers) as pool:
        worker_args = [(pin_dir, b, coll_0, coll_1, coll_ops) for b in buckets if b]
        for part in pool.map(_worker_run, worker_args):
            for tid, (a0, a1, btail, ops_clean) in part.items():
                ti = tid_to_i.get(tid)
                if ti is None:
                    continue
                profiled.add(tid)
                for s in a0:
                    b0_map[s].append(ti)
                for s in a1:
                    b1_map[s].append(ti)
                for s in btail:
                    btail_map[s].append(ti)
                for s, vals in ops_clean.items():
                    key = json.dumps(vals, separators=(",", ":"))
                    op_groups[s][key].append(ti)

    # Sort all index lists for determinism
    all_b_sites = sorted(b0_map.keys() | b1_map.keys())
    branches_payload = {
        "n_tests": len(profiled),
        "sites": {s: [sorted(b0_map.get(s, ())), sorted(b1_map.get(s, ()))] for s in all_b_sites},
        "tail_both": {s: sorted(v) for s, v in sorted(btail_map.items())},
    }
    b_bytes = gzip.compress(json.dumps(branches_payload, separators=(",", ":")).encode(), compresslevel=9)
    (maps_dir / "branches.json.gz").write_bytes(b_bytes)

    operands_payload = {
        "n_tests": len(profiled),
        "sites": {
            s: [[sorted(tidxs), json.loads(vk)] for vk, tidxs in sorted(vmap.items())]
            for s, vmap in sorted(op_groups.items())
        },
    }
    o_bytes = gzip.compress(json.dumps(operands_payload, separators=(",", ":")).encode(), compresslevel=9)
    (maps_dir / "operands.json.gz").write_bytes(o_bytes)

    print(
        f"Built branches.json.gz ({len(b_bytes):,}B, {len(all_b_sites)} sites, {len(profiled)} tests) and "
        f"operands.json.gz ({len(o_bytes):,}B, {len(op_groups)} sites, {len(profiled)} tests) "
        f"in {time.monotonic() - t0:.1f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
