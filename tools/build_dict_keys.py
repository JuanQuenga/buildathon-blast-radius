"""Build selector/maps/dict-keys.json.gz: exact per-test lookup & bulk-read map
for SQLGlot's core dispatch dictionaries on the pristine pin.

For each of the 1,620 catalog tests, records every key looked up (both hits and
misses) and every bulk read (iter/keys/values/items/len/copy/unpack/merge) on:
  - Generator.TRANSFORMS (and _DISPATCH_CACHE)
  - Generator.TYPE_MAPPING
  - Generator.PROPERTIES_LOCATION
  - Parser.FUNCTIONS
  - Parser.FUNCTION_PARSERS
  - Parser.STATEMENT_PARSERS
  - Parser.PROPERTY_PARSERS
  - Parser.UNARY_PARSERS
  - Dialect / sqlglot.typing.*.EXPRESSION_METADATA
"""
from __future__ import annotations

import concurrent.futures as cf
import concurrent.futures.process as cfp
import gzip
import importlib
import json
import os
import pickle
import pkgutil
import sys
import time
import unittest
from collections import defaultdict
from pathlib import Path

TABLES = (
    "TRANSFORMS",
    "TYPE_MAPPING",
    "PROPERTIES_LOCATION",
    "FUNCTIONS",
    "FUNCTION_PARSERS",
    "STATEMENT_PARSERS",
    "PROPERTY_PARSERS",
    "UNARY_PARSERS",
    "EXPRESSION_METADATA",
)


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


def _worker_run(args):
    pin_dir, items = args  # items: list[(idx, node_id)]
    sys.path.insert(0, pin_dir)
    os.environ["SKIP_INTEGRATION"] = "0"
    _install_inline_pool()

    import sqlglot
    import sqlglot.dialects as dialects_pkg
    import sqlglot.generator as gen_mod
    import sqlglot.parser as parser_mod
    import sqlglot.typing as typing_pkg
    from sqlglot import exp
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.tokens import TokenType

    # Pre-import every dialect, parser, generator, typing submodule BEFORE wrapping
    # so class-creation dict unpacks do not pollute runtime observations.
    for pkg in (dialects_pkg, typing_pkg):
        for _, modname, _ in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
            try:
                importlib.import_module(modname)
            except Exception:
                pass
    for d in list(Dialect.classes.values()):
        gen_mod._DISPATCH_CACHE[d.generator_class] = gen_mod._build_dispatch(d.generator_class)
    gen_mod._DISPATCH_CACHE[gen_mod.Generator] = gen_mod._build_dispatch(gen_mod.Generator)

    active = [False]
    cur_hits: set[tuple[str, str]] = set()
    cur_bulk: set[str] = set()

    def encode_key(k):
        if isinstance(k, str):
            return "S:" + k
        if isinstance(k, type) and issubclass(k, exp.Expr):
            return "E:" + k.__name__
        if isinstance(k, exp.DType):
            return "D:" + k.name
        if isinstance(k, TokenType):
            return "T:" + k.name
        return None

    class TrackedDict(dict):
        __slots__ = ("_table",)

        def __init__(self, data, table):
            super().__init__(data)
            self._table = table

        def _rec(self, k):
            if active[0]:
                ek = encode_key(k)
                if ek is None:
                    cur_bulk.add(self._table)
                else:
                    cur_hits.add((self._table, ek))

        def _blk(self):
            if active[0]:
                cur_bulk.add(self._table)

        def __getitem__(self, k):
            self._rec(k)
            return super().__getitem__(k)

        def get(self, k, default=None):
            self._rec(k)
            return super().get(k, default)

        def __contains__(self, k):
            self._rec(k)
            return super().__contains__(k)

        def __iter__(self):
            self._blk()
            return super().__iter__()

        def keys(self):
            self._blk()
            return super().keys()

        def values(self):
            self._blk()
            return super().values()

        def items(self):
            self._blk()
            return super().items()

        def __len__(self):
            self._blk()
            return super().__len__()

        def copy(self):
            self._blk()
            return TrackedDict(super().copy(), self._table)

        def __or__(self, other):
            self._blk()
            return super().__or__(other)

        def __ror__(self, other):
            self._blk()
            return super().__ror__(other)

        def __repr__(self):
            self._blk()
            return super().__repr__()

        def __eq__(self, other):
            self._blk()
            return super().__eq__(other)

    def all_subclasses(cls):
        out = {cls}
        stack = [cls]
        while stack:
            c = stack.pop()
            for sub in c.__subclasses__():
                if sub not in out:
                    out.add(sub)
                    stack.append(sub)
        return out

    # Wrap Generator subclasses + _DISPATCH_CACHE + _build_dispatch
    for gcls in all_subclasses(gen_mod.Generator):
        for attr in ("TRANSFORMS", "TYPE_MAPPING", "PROPERTIES_LOCATION"):
            val = gcls.__dict__.get(attr)
            if isinstance(val, dict) and not isinstance(val, TrackedDict):
                setattr(gcls, attr, TrackedDict(val, attr))
        if gcls in gen_mod._DISPATCH_CACHE:
            gen_mod._DISPATCH_CACHE[gcls] = TrackedDict(gen_mod._DISPATCH_CACHE[gcls], "TRANSFORMS")

    orig_build_dispatch = gen_mod._build_dispatch

    def wrapped_build_dispatch(cls):
        was = active[0]
        active[0] = False
        try:
            d = orig_build_dispatch(cls)
        finally:
            active[0] = was
        return TrackedDict(d, "TRANSFORMS")

    gen_mod._build_dispatch = wrapped_build_dispatch

    # Wrap Parser subclasses
    for pcls in all_subclasses(parser_mod.Parser):
        for attr in ("FUNCTIONS", "FUNCTION_PARSERS", "STATEMENT_PARSERS", "PROPERTY_PARSERS", "UNARY_PARSERS"):
            val = pcls.__dict__.get(attr)
            if isinstance(val, dict) and not isinstance(val, TrackedDict):
                setattr(pcls, attr, TrackedDict(val, attr))

    # Wrap Dialect & typing EXPRESSION_METADATA
    for dcls in all_subclasses(Dialect):
        val = dcls.__dict__.get("EXPRESSION_METADATA")
        if isinstance(val, dict) and not isinstance(val, TrackedDict):
            setattr(dcls, "EXPRESSION_METADATA", TrackedDict(val, "EXPRESSION_METADATA"))
    for name, mod in list(sys.modules.items()):
        if name == "sqlglot.typing" or name.startswith("sqlglot.typing."):
            val = getattr(mod, "EXPRESSION_METADATA", None)
            if isinstance(val, dict) and not isinstance(val, TrackedDict):
                setattr(mod, "EXPRESSION_METADATA", TrackedDict(val, "EXPRESSION_METADATA"))

    # Group tests by (file, class) so setUpClass runs once per class (and track
    # setUpClass observations into every test of that class!).
    by_cls = defaultdict(list)
    for idx, nid in items:
        parts = nid.split("::")
        if len(parts) != 3:
            by_cls[("__invalid__", "")].append((idx, nid))
        else:
            by_cls[(parts[0], parts[1])].append((idx, nid))

    out_hits = []
    out_bulk = []
    for (fpath, cname), group in by_cls.items():
        if fpath == "__invalid__":
            for idx, _ in group:
                out_bulk.append((idx, list(TABLES)))
            continue
        try:
            mod = importlib.import_module(fpath[:-3].replace("/", "."))
            cls = getattr(mod, cname)
        except Exception:
            for idx, _ in group:
                out_bulk.append((idx, list(TABLES)))
            continue

        # Run setUpClass once and credit its observations to all tests in cls.
        cur_hits.clear()
        cur_bulk.clear()
        active[0] = True
        setup_ok = True
        try:
            cls.setUpClass()
        except Exception:
            setup_ok = False
        finally:
            active[0] = False
        setup_hits = set(cur_hits)
        setup_bulk = set(cur_bulk)

        try:
            for idx, nid in group:
                method = nid.split("::")[2]
                if not setup_ok:
                    out_bulk.append((idx, list(TABLES)))
                    continue
                cur_hits.clear()
                cur_bulk.clear()
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
                if not ok:
                    out_bulk.append((idx, list(TABLES)))
                else:
                    merged_bulk = setup_bulk | cur_bulk
                    merged_hits = setup_hits | cur_hits
                    if merged_bulk:
                        out_bulk.append((idx, sorted(merged_bulk)))
                    # Only emit hits for tables not already bulked for this test.
                    kept_hits = [(t, k) for (t, k) in merged_hits if t not in merged_bulk]
                    if kept_hits:
                        out_hits.append((idx, kept_hits))
        finally:
            try:
                cls.tearDownClass()
            except Exception:
                pass
    return out_hits, out_bulk


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
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("selector/maps/dict-keys.json.gz")

    # Align test order with selector/maps/index.pkl.z so indices match impact._index()["tests"]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from selector import impact
    tests = impact._index()["tests"]
    dur_by_id = {t["id"]: float(t["seconds"]) for t in json.loads(cat_path.read_text())["tests"]}

    # Partition by (file, class) balanced by duration across 6 workers
    by_cls = defaultdict(list)
    for idx, tid in enumerate(tests):
        parts = tid.split("::")
        key = (parts[0], parts[1]) if len(parts) == 3 else ("__other__", tid)
        by_cls[key].append((idx, tid))

    groups = sorted(
        by_cls.values(),
        key=lambda g: -sum(dur_by_id.get(tid, 0.01) for _, tid in g),
    )
    n_workers = 6
    buckets = [[] for _ in range(n_workers)]
    loads = [0.0] * n_workers
    for g in groups:
        b = min(range(n_workers), key=loads.__getitem__)
        buckets[b].extend(g)
        loads[b] += sum(dur_by_id.get(tid, 0.01) for _, tid in g)

    bulk_map = {t: set() for t in TABLES}
    key_map = {t: defaultdict(set) for t in TABLES}

    with cf.ProcessPoolExecutor(max_workers=n_workers) as pool:
        for out_hits, out_bulk in pool.map(_worker_run, [(pin_dir, b) for b in buckets if b]):
            for idx, tables in out_bulk:
                for t in tables:
                    bulk_map[t].add(idx)
            for idx, pairs in out_hits:
                for t, k in pairs:
                    key_map[t][k].add(idx)

    payload = {
        "bulk": {t: sorted(v) for t, v in bulk_map.items()},
        "keys": {t: {k: sorted(v) for k, v in sorted(d.items())} for t, d in key_map.items()},
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    out_path.write_bytes(gzip.compress(raw, compresslevel=9))
    print(f"Built {out_path} ({out_path.stat().st_size:,} bytes gz, {len(raw):,} bytes raw) in {time.monotonic()-t0:.1f}s")
    for t in TABLES:
        print(f"  {t:22s} | bulk={len(bulk_map[t]):4d} tests | distinct_keys={len(key_map[t]):4d}")


if __name__ == "__main__":
    main()
