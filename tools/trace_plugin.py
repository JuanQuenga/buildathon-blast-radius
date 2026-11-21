"""pytest plugin: per-test dynamic dependency tracing (offline, profiling only).

Usage (one pytest process per test file, see tools/build_maps.sh):

    TRACE_OUT=/path/out TRACE_ROOT=/path/to/sqlglot-checkout \
        python -m pytest -p tools.trace_plugin -p no:cacheprovider tests/test_x.py

Every test item runs in its own forked child, so lazily-built caches, lazy
dialect imports and ``setUpClass`` work are attributed to *each* test that
depends on them rather than only to the first test of the process.  The
parent records what collection (importing the test module) executes.

Per context (``collect`` or a test node id) we record:
  * ``lines``: {relpath: [line, ...]} executed lines in sqlglot/ and tests/
    (sys.monitoring LINE events, each location disabled after its first hit,
    re-armed per test with ``restart_events``),
  * ``opens``: files opened under the checkout (audit hook; import machinery
    excluded) -> fixture dependencies, source inspection,
  * ``listdirs``: directories listed/globbed under the checkout,
  * ``classes``: "module.Qualname" of sqlglot classes that defining an
    ``__init__`` had an instance constructed (type(self), so subclasses are
    recorded precisely),
  * ``outcome``: passed/failed per phase (sanity check of the clean pin).
"""

from __future__ import annotations

import builtins
import json
import os
import sys
import time

import pytest

ROOT = os.path.realpath(os.environ.get("TRACE_ROOT", os.getcwd())) + os.sep
OUT = os.environ.get("TRACE_OUT", "trace-out")
TOOL = sys.monitoring.PROFILER_ID
_mon = sys.monitoring

_cur_lines: dict = {}
_cur_opens: set = set()
_cur_dirs: set = set()
_cur_classes: dict = {}
_file_ok: dict = {}


def _rel(path):
    try:
        p = os.path.realpath(os.fspath(path))
    except Exception:
        return None
    if p.startswith(ROOT):
        return p[len(ROOT):]
    return None


def _line_cb(code, line):
    fn = code.co_filename
    ok = _file_ok.get(fn)
    if ok is None:
        r = _rel(fn)
        ok = r if (r is not None and (r.startswith("sqlglot/") or r.startswith("tests/"))) else False
        _file_ok[fn] = ok
    if ok:
        s = _cur_lines.get(ok)
        if s is None:
            s = _cur_lines[ok] = set()
        s.add(line)
    return _mon.DISABLE


def _from_import_system():
    f = sys._getframe(2)
    depth = 0
    while f is not None and depth < 14:
        mod = f.f_globals.get("__name__", "")
        if mod.startswith("importlib") or mod in ("_frozen_importlib_external", "pkgutil"):
            return True
        f = f.f_back
        depth += 1
    return False


def _audit(event, args):
    if event == "open":
        path = args[0]
        if isinstance(path, int) or path is None:
            return
        r = _rel(path)
        if r is None or "__pycache__" in r:
            return
        if _from_import_system():
            return
        _cur_opens.add(r)
    elif event in ("os.listdir", "os.scandir", "glob.glob"):
        path = args[0] if args else "."
        if path is None:
            path = "."
        if isinstance(path, int):
            return
        r = _rel(path)
        if r is not None and not _from_import_system():
            _cur_dirs.add(r)


_orig_build_class = builtins.__build_class__


def _wrap_init(cls):
    init = cls.__dict__.get("__init__")
    if init is None or not callable(init) or getattr(init, "_traced", False):
        return
    import functools

    @functools.wraps(init)
    def __init__(self, *a, **k):
        t = type(self)
        _cur_classes[id(t)] = t
        return init(self, *a, **k)

    __init__._traced = True
    try:
        cls.__init__ = __init__
    except Exception:
        pass


def _wrap_meta_getattribute(meta):
    """Record class-object attribute reads (e.g. Dialect classes used without
    instantiation) for sqlglot metaclasses."""
    orig = meta.__getattribute__

    def __getattribute__(cls, name):
        _cur_classes[id(cls)] = cls  # id: sqlglot metaclasses override __hash__
        return orig(cls, name)

    try:
        meta.__getattribute__ = __getattribute__
    except Exception:
        pass


def _build_class(func, name, *bases, **kw):
    cls = _orig_build_class(func, name, *bases, **kw)
    try:
        mod = getattr(func, "__module__", None) or ""
        if isinstance(cls, type) and mod.startswith("sqlglot"):
            _wrap_init(cls)
            if issubclass(cls, type):
                _wrap_meta_getattribute(cls)
    except Exception:
        pass
    return cls


def _reset():
    global _cur_lines, _cur_opens, _cur_dirs, _cur_classes
    _cur_lines = {}
    _cur_opens = set()
    _cur_dirs = set()
    _cur_classes = {}
    _mon.restart_events()


def _snapshot():
    mro = {}
    for c in list(_cur_classes.values()):
        mro[f"{c.__module__}.{c.__qualname__}"] = [
            f"{b.__module__}.{b.__qualname__}"
            for b in c.__mro__
            if (b.__module__ or "").startswith(("sqlglot", "tests"))
        ]
    return {
        "mro": mro,
        "lines": {k: sorted(v) for k, v in _cur_lines.items()},
        "opens": sorted(_cur_opens),
        "listdirs": sorted(_cur_dirs),
        "classes": sorted(f"{c.__module__}.{c.__qualname__}" for c in list(_cur_classes.values())),
    }


class _SyncExecutor:
    """In-process stand-in for ProcessPoolExecutor so that work the tests ship
    to spawned workers (optimizer fixtures, executor TPC-H/DS) is traced."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        import pickle

        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *a, **k):
        from concurrent.futures import Future

        import pickle

        f = Future()
        try:
            # a real spawn pool pickles arguments and results (Expr.__reduce__ ->
            # sqlglot.serde): reproduce that so it is traced too
            fn, a, k = pickle.loads(pickle.dumps((fn, a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def map(self, fn, *its, **kw):
        return [fn(*a) for a in zip(*its)]

    def shutdown(self, wait=True, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def pytest_unconfigure(config):
    try:
        _mon.set_events(TOOL, 0)
    except Exception:
        pass


def pytest_configure(config):
    os.makedirs(OUT, exist_ok=True)
    import concurrent.futures

    concurrent.futures.ProcessPoolExecutor = _SyncExecutor
    builtins.__build_class__ = _build_class
    sys.addaudithook(_audit)
    _mon.use_tool_id(TOOL, "blast-trace")
    _mon.register_callback(TOOL, _mon.events.LINE, _line_cb)
    _mon.set_events(TOOL, _mon.events.LINE)
    _reset()


def _safe(nodeid):
    return nodeid.replace("/", "__").replace("::", "--").replace("[", "(").replace("]", ")")


def pytest_collection_finish(session):
    data = _snapshot()
    data["context"] = "collect"
    data["args"] = list(session.config.args)
    data["items"] = [i.nodeid for i in session.items]
    name = "collect--" + _safe("+".join(session.config.args))[:150] + ".json"
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(data, f)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    from _pytest.runner import runtestprotocol

    path = os.path.join(OUT, "test--" + _safe(item.nodeid) + ".json")
    t0 = time.time()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            _reset()
            reports = runtestprotocol(item, nextitem=None, log=False)
            outcome = {r.when: r.outcome for r in reports}
            data = _snapshot()
            data["context"] = item.nodeid
            data["outcome"] = outcome
            cls = getattr(item, "cls", None)
            data["test_mro"] = [
                f"{b.__module__}.{b.__qualname__}" for b in (cls.__mro__ if cls else ())
                if (b.__module__ or "").startswith(("sqlglot", "tests"))
            ]
            with open(path + ".tmp", "w") as f:
                json.dump(data, f)
            os.replace(path + ".tmp", path)
        except BaseException as e:  # noqa
            try:
                sys.stderr.write(f"TRACE CHILD ERROR {item.nodeid}: {e!r}\n")
            except Exception:
                pass
            code = 3
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    if status != 0 or not os.path.exists(path):
        sys.stderr.write(f"TRACE FAILED {item.nodeid} status={status}\n")
    sys.stderr.write(f"traced {item.nodeid} {time.time() - t0:.2f}s\n")
    return True
