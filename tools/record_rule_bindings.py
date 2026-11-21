"""Record arguments explicitly supplied to top-level optimizer calls on the pin.

Run inside a pinned image with a writable destination directory, outside the
image build. sitecustomize also instruments the test's spawned workers.
"""
import atexit
import json
import linecache
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types


def install():
    seen = {}
    root = "/app/sqlglot/"
    optimizer = root + "sqlglot/optimizer/optimizer.py"

    queries = 0

    def reflection(event, args):
        if (event not in {"object.__getattr__", "object.__setattr__"} or len(args) < 2
                or args[1] not in {"__defaults__", "__kwdefaults__"}
                or not isinstance(args[0], types.FunctionType)):
            return
        code = args[0].__code__
        if not code.co_filename.startswith(root + "sqlglot/optimizer/"):
            return
        frame = sys._getframe(1)
        while frame and frame.f_globals.get("__name__") == "inspect":
            frame = frame.f_back
        if (event == "object.__getattr__" and frame and frame.f_code.co_filename == optimizer
                and linecache.getline(optimizer, frame.f_lineno).strip()
                == "rule_params = inspect.getfullargspec(rule).args"):
            return
        seen[code.co_filename[len(root):] + ":" + code.co_qualname] = set()

    def entered(code, offset):
        nonlocal queries
        if code.co_filename == root + "tests/test_executor.py" and code.co_name == "mp_execute":
            queries += 1
            return
        if not code.co_filename.startswith(root + "sqlglot/optimizer/") or code.co_qualname != code.co_name:
            return sys.monitoring.DISABLE
        caller = sys._getframe(1).f_back
        key = code.co_filename[len(root):] + ":" + code.co_name
        explicit = set()
        if caller is not None and caller.f_code.co_filename == optimizer and caller.f_code.co_name == "optimize":
            rule = caller.f_locals.get("rule")
            if getattr(rule, "__code__", None) is code:
                kwargs = caller.f_locals["rule_kwargs"]
                if type(kwargs) is dict:
                    explicit.update(kwargs)
        if key in seen:
            seen[key].intersection_update(explicit)
        else:
            seen[key] = explicit
        if not explicit:
            return sys.monitoring.DISABLE

    def save():
        path = Path(os.environ["BLAST_BINDINGS_OUT"]) / f"{os.getpid()}.json"
        path.write_text(json.dumps([queries, {k: sorted(v) for k, v in seen.items()}]))

    sys.monitoring.use_tool_id(4, "optimizer argument bindings")
    sys.monitoring.register_callback(4, sys.monitoring.events.PY_START, entered)
    sys.monitoring.set_events(4, sys.monitoring.events.PY_START)
    sys.addaudithook(reflection)
    atexit.register(save)


def record(out):
    with tempfile.TemporaryDirectory() as scratch:
        folder = Path(scratch)
        (folder / "sitecustomize.py").write_bytes(Path(__file__).read_bytes())
        env = dict(os.environ, PYTHONPATH=f"{folder}:/app/sqlglot", BLAST_BINDINGS_OUT=scratch)
        subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
                        "tests/test_executor.py::TestExecutor::test_execute_tpcds"],
                       cwd="/app/sqlglot", env=env, check=True)
        observations = {}
        count = 0
        for path in folder.glob("*.json"):
            queries, bindings = json.loads(path.read_text())
            count += queries
            for fn, args in bindings.items():
                if fn in observations:
                    observations[fn].intersection_update(args)
                else:
                    observations[fn] = set(args)
        confirmed = {k: sorted(v) for k, v in observations.items() if v}
        if not confirmed or count != 99:
            raise RuntimeError("missing optimizer observations")
        out.write_text(json.dumps(confirmed, sort_keys=True) + "\n")


if __name__ == "sitecustomize":
    install()
elif __name__ == "__main__":
    record(Path(sys.argv[1]))
