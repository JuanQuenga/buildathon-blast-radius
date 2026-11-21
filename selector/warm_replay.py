"""Fork a fresh overlay interpreter from the verifier's third-party imports."""
import contextlib
import importlib
import os
import resource
import runpy
import signal
import sys
import time

ready = False
active_overlay = None


def prepare():
    global ready
    if ready:
        return True
    try:
        os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
        for module in ("numpy", "pandas", "duckdb", "pytest"):
            importlib.import_module(module)
        ready = not any(name.split(".")[0] in {"sqlglot", "tests"} for name in sys.modules)
    except (ImportError, OSError, MemoryError):
        ready = False
    return ready


def prepare_core():
    """Warm the package root while keeping lazy dialect modules unloaded."""
    global ready
    if "sqlglot" not in sys.modules:
        sys.path.insert(0, "/app/sqlglot")
        try:
            importlib.import_module("sqlglot")
        finally:
            sys.path.pop(0)
    ready = True


def compatible(modules):
    if active_overlay:
        return all(n not in sys.modules or getattr(sys.modules[n], "__file__", "").startswith(active_overlay + "/")
                   for n in filter(None, modules.split(",")))
    return all(not any(name == changed or name.startswith(changed + ".")
                       for name in sys.modules)
               for changed in filter(None, modules.split(",")))


def enter_overlay(overlay):
    os.chdir(overlay)
    sys.path.insert(0, os.getcwd())
    for name, module in sys.modules.copy().items():
        if name == "sqlglot" or name.startswith("sqlglot."):
            paths = getattr(module, "__path__", None)
            if paths is not None:
                module.__path__ = [p.replace("/app/sqlglot/", overlay + "/", 1)
                                   for p in paths]


def run(script, group, args, overlay, deadline, modules, receipt, phase=None, cpu_limit=None):
    pid = os.fork()
    if pid == 0:
        status = 1
        try:
            if active_overlay is None:
                os.setsid()
            limit = 1800 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
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
            time.sleep(0.01)
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
