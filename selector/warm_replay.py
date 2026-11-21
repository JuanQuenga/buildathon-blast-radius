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


def run(script, group, args, overlay, deadline, modules, receipt):
    pid = os.fork()
    if pid == 0:
        status = 1
        try:
            os.setsid()
            limit = 1800 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            with open(os.devnull, "r+") as devnull:
                for fd in (0, 1, 2):
                    os.dup2(devnull.fileno(), fd)
            os.chdir(overlay)
            sys.path.insert(0, os.getcwd())
            sys.path.append(str(script.parent))
            os.environ["BLAST_CHANGED_MODULE"] = modules
            sys.argv = [str(script), group, *args]
            runpy.run_path(str(script), run_name="__main__")
            for name in filter(None, modules.split(",")):
                importlib.import_module(name)
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
