"""Fork a fresh overlay interpreter from a clean, pre-sqlglot parent.

Invariant: the parent NEVER imports ``sqlglot`` or ``tests`` (except inside an
isolated PatchSession worker dedicated to one overlay). That guarantees every
forked probe starts with zero stale ``sqlglot`` modules, ~1-2 ms fork latency,
and shared copy-on-write pages for stdlib and (lazily loaded) third-party libs.
"""
import contextlib
import importlib
import os
import runpy
import signal
import sys
import time

from .runtime_paths import SQLGLOT_DIR, pin_affinity, set_memory_limit

ready = False
heavy_ready = False
pytest_ready = False
active_overlay = None


def prepare_base():
    """Warm lightweight stdlib modules without touching sqlglot or heavy C libs."""
    global ready
    if ready:
        return True
    if any(name.split(".")[0] in {"sqlglot", "tests"} for name in sys.modules):
        return False
    pin_affinity(2)
    for mod in ("unittest", "pickle", "hashlib", "gc", "tracemalloc", "concurrent.futures"):
        with contextlib.suppress(ImportError):
            importlib.import_module(mod)
    ready = True
    return True


def prepare_pytest():
    """Lazily pre-import pytest (shared by forked collection probes)."""
    global pytest_ready
    if pytest_ready:
        return True
    if not prepare_base():
        return False
    try:
        importlib.import_module("pytest")
        pytest_ready = not any(name.split(".")[0] in {"sqlglot", "tests"} for name in sys.modules)
    except (ImportError, OSError, MemoryError):
        pytest_ready = False
    return pytest_ready


def prepare_data_libs():
    """Lazily pre-import numpy/pandas/duckdb WITHOUT pytest or sqlglot."""
    global ready, heavy_ready
    if heavy_ready:
        return True
    if any(name.split(".")[0] in {"sqlglot", "tests"} for name in sys.modules):
        return False
    try:
        pin_affinity(2)
        for module in ("numpy", "pandas", "pandas.testing", "duckdb"):
            importlib.import_module(module)
        clean = not any(name.split(".")[0] in {"sqlglot", "tests"} for name in sys.modules)
        heavy_ready = clean
        ready = clean
    except (ImportError, OSError, MemoryError):
        heavy_ready = False
    return heavy_ready


def prepare():
    """Lazily pre-import duckdb/pandas/numpy/pytest when full executor setup runs."""
    return prepare_data_libs() and prepare_pytest()


def prepare_core():
    """Compatibility alias: warm lightweight base WITHOUT importing sqlglot."""
    return prepare_base()


def compatible(modules):
    if active_overlay:
        return all(
            n not in sys.modules or getattr(sys.modules[n], "__file__", "").startswith(active_overlay + "/")
            for n in filter(None, modules.split(","))
        )
    return not any(name.split(".")[0] in {"sqlglot", "tests"} for name in sys.modules)


def enter_overlay(overlay):
    os.chdir(overlay)
    sys.path.insert(0, os.getcwd())
    sqlglot_prefix = str(SQLGLOT_DIR) + "/"
    for name, module in sys.modules.copy().items():
        if name == "sqlglot" or name.startswith("sqlglot."):
            paths = getattr(module, "__path__", None)
            if paths is not None:
                module.__path__ = [p.replace(sqlglot_prefix, overlay + "/", 1) for p in paths]


def run(script, group, args, overlay, deadline, modules, receipt, phase=None, cpu_limit=None):
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        status = 1
        try:
            if active_overlay is None:
                os.setsid()
            set_memory_limit(1800)
            with open(os.devnull, "r+") as devnull:
                for fd in (0, 1, 2):
                    os.dup2(devnull.fileno(), fd)
            enter_overlay(overlay)
            sys.path.append(str(script.parent))
            os.environ["BLAST_CHANGED_MODULE"] = modules
            if phase is not None:
                os.environ["BLAST_PHASE_FD"] = str(phase.fileno())
            if cpu_limit is not None:
                # The static selector installs an exception-raising SIGPROF handler.
                # A patched parser can catch that exception as an ordinary parse
                # error. Let waitpid report the CPU limit as a signal instead.
                os.environ["BLAST_PROBE_CPU"] = str(cpu_limit)
                signal.signal(signal.SIGPROF, signal.SIG_DFL)
                signal.setitimer(signal.ITIMER_PROF, cpu_limit)
            sys.argv = [str(script), group, *args]
            runpy.run_path(str(script), run_name="__main__")
            for name in filter(None, modules.split(",")):
                loaded = importlib.import_module(name)
                if not os.path.abspath(loaded.__file__).startswith(overlay + "/"):
                    raise RuntimeError("patched module origin")
            os.write(receipt.fileno(), b"OK")
            status = 0
        except BaseException:
            pass
        os._exit(status)
    try:
        while time.monotonic() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                receipt.seek(0)
                if os.waitstatus_to_exitcode(status) == -signal.SIGPROF:
                    return "timeout"
                return "pass" if status == 0 and receipt.read(3) == b"OK" else "failed"
            time.sleep(0.005)
        return "timeout"
    finally:
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            done = pid
        if not done:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
