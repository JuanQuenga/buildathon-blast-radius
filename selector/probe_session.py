"""Share one imported overlay across isolated checks on a method-local patch."""
import contextlib
import importlib
import json
import math
import os
import signal
import multiprocessing
import time

from . import impact, warm_replay
from .runtime_paths import set_memory_limit


def serve(conn, work, paths):
    from . import heavy
    os.setsid()
    set_memory_limit(1800)
    with open(os.devnull, "r+") as sink:
        for fd in (0, 1, 2):
            os.dup2(sink.fileno(), fd)
    try:
        warm_replay.enter_overlay(work)
        modules = [impact._module_name(p) for p in paths.split(",")]
        for module in modules:
            loaded = importlib.import_module(module)
            if not loaded.__file__.startswith(work + "/"):
                return
        warm_replay.ready = True
        warm_replay.active_overlay = work
        while True:
            mode, args, deadline, cpu_limit = json.loads(conn.recv_bytes(65536))
            heavy.replay.phase = mode
            result = heavy.replay(mode, args, work, deadline, paths, cpu_limit)
            conn.send_bytes(json.dumps([result, heavy.replay.phase, heavy.cpu()]).encode())
    except (EOFError, BrokenPipeError):
        pass
    finally:
        conn.close()


class PatchSession:
    def __init__(self, work, paths):
        ctx = multiprocessing.get_context("fork")
        self.conn, child = ctx.Pipe()
        self.process = ctx.Process(target=serve, args=(child, work, paths))
        self.process.start()
        child.close()
        self.charged = 0.0

    def check(self, mode, args, deadline, cpu_limit):
        self.conn.send_bytes(json.dumps([mode, args, deadline, cpu_limit]).encode())
        if not self.conn.poll(max(0, deadline + 0.06 - time.monotonic())):
            self.close()
            return "timeout", mode
        result, phase, charged = json.loads(self.conn.recv_bytes(256))
        if (result not in {"pass", "failed", "timeout"} or not isinstance(phase, str)
                or not math.isfinite(charged) or charged < self.charged):
            raise ValueError("invalid probe receipt")
        self.charged = charged
        return result, phase

    def close(self):
        self.conn.close()
        # Explicitly stop the server. Its duplicate pipe endpoint can keep
        # recv alive, and a stalled patched import may never enter the loop.
        if self.process.is_alive():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.kill()
        self.process.join()
        self.charged = 0.0
