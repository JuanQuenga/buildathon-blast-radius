"""Runtime verification: run candidate tests against the patched source.

Used only to *drop* candidates that were seen to pass and to *confirm*
failures.  Anything ambiguous (patch does not apply, import error, skip,
timeout, crash, missing result) leaves the test "unverified", and the caller
keeps it scheduled.

Mechanics (all inside the 64 MB /tmp, nothing written to the read-only image):
  1. ``apply_patch``: pure-Python strict apply of the unified diff to the
     pristine checkout (exact positions and context, else PatchError).
  2. ``build_overlay``: a symlink farm under /tmp mirroring the checkout, where
     only directories on the path of a changed file are real directories.
     Module ``__file__`` paths stay inside the overlay, so fixture reads
     (``tests/helpers.FIXTURES_DIR``) see patched fixture files too.
  3. ``run_tests``: fork a child (the parent has already imported the heavy,
     unpatched third-party modules such as duckdb and pandas), chdir and
     put the overlay first on sys.path, import the test modules fresh, run
     the selected unittest methods in catalog order, and report per-test
     outcomes over a pipe.  The parent kills the child at a hard deadline.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time

from .patch import FileDiff, PatchError, parse

PRISTINE = os.environ.get("BLAST_SQLGLOT", "/app/sqlglot")

PASS, FAIL, SKIP = "pass", "fail", "skip"


# --------------------------------------------------------------------------
# 1. Patch application


def _apply_file(fd: FileDiff, root: str) -> str | None:
    """Return new file text, or None if the file is deleted."""
    if fd.is_binary:
        raise PatchError("binary patch")
    if fd.is_deleted:
        return None
    if fd.is_new:
        old_lines: list[str] = []
        old_nl = True
    else:
        with open(os.path.join(root, fd.old_path), encoding="utf-8", newline="") as f:
            text = f.read()
        old_nl = text.endswith("\n")
        old_lines = text.split("\n")
        if old_nl:
            old_lines.pop()
    out: list[str] = []
    pos = 0  # index into old_lines (0-based)
    new_nl = old_nl
    for h in fd.hunks:
        start = h.old_start - 1 if h.old_len > 0 else h.old_start
        if start < pos or start > len(old_lines):
            raise PatchError("hunk out of order / range")
        out.extend(old_lines[pos:start])
        pos = start
        prev = None
        for line in h.lines:
            c = line[:1]
            if c == "\\":
                # "\ No newline at end of file" applies to the previous line
                if prev == "-" or prev == " ":
                    if pos == len(old_lines) and old_nl:
                        raise PatchError("newline marker mismatch")
                if prev == "+" or prev == " ":
                    new_nl = False
                continue
            body = line[1:]
            if c == " " or c == "-":
                if pos >= len(old_lines) or old_lines[pos] != body:
                    raise PatchError(f"context mismatch in {fd.old_path} at {pos + 1}")
                pos += 1
                if c == " ":
                    out.append(body)
            elif c == "+":
                out.append(body)
                new_nl = True
            prev = c
    out.extend(old_lines[pos:])
    if pos < len(old_lines):
        new_nl = old_nl
    return "\n".join(out) + ("\n" if new_nl and out else "")


def apply_patch(text: str, root: str = PRISTINE) -> dict[str, str | None]:
    """Map relative path -> new text (None = deleted)."""
    changes: dict[str, str | None] = {}
    for fd in parse(text):
        if fd.mode_change and not fd.hunks and not fd.is_rename:
            continue
        if fd.is_rename and fd.old_path and fd.old_path != fd.new_path:
            src = os.path.join(root, fd.old_path)
            if not fd.hunks:
                with open(src, encoding="utf-8", newline="") as f:
                    changes[fd.new_path] = f.read()
            else:
                changes[fd.new_path] = _apply_file(fd, root)
            changes[fd.old_path] = None
            continue
        path = fd.path
        if not path or ".." in path.split("/") or path.startswith("/"):
            raise PatchError(f"bad path {path!r}")
        changes[path] = _apply_file(fd, root)
    return changes


# --------------------------------------------------------------------------
# 2. Overlay


def build_overlay(dest: str, changes: dict[str, str | None], root: str = PRISTINE) -> None:
    """Symlink farm of ``root`` at ``dest`` with ``changes`` materialised."""
    expand: set[str] = set()
    for p in changes:
        parts = p.split("/")
        for i in range(1, len(parts)):
            expand.add("/".join(parts[:i]))

    def mirror(rel: str) -> None:
        src_dir = os.path.join(root, rel) if rel else root
        dst_dir = os.path.join(dest, rel) if rel else dest
        os.makedirs(dst_dir, exist_ok=True)
        try:
            names = os.listdir(src_dir)
        except FileNotFoundError:
            names = []
        for name in names:
            r = f"{rel}/{name}" if rel else name
            if r in changes or name == "__pycache__" and _has_changed_py(rel, changes):
                continue
            if r in expand:
                mirror(r)
            else:
                os.symlink(os.path.join(src_dir, name), os.path.join(dst_dir, name))
        for d in expand:
            if os.path.dirname(d) == rel and not os.path.exists(os.path.join(dest, d)):
                mirror(d)  # directory created by the patch

    mirror("")
    for p, txt in changes.items():
        if txt is None:
            continue
        full = os.path.join(dest, p)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(txt)


def _has_changed_py(rel: str, changes) -> bool:
    # Drop the precompiled __pycache__ of a directory holding a changed .py so
    # a stale .pyc can never shadow the patched source.
    for p in changes:
        if p.endswith(".py") and os.path.dirname(p) == rel:
            return True
    return False


# --------------------------------------------------------------------------
# 3. Running tests in a forked child


def _module_of(nid: str) -> tuple[str, str, str]:
    path, cls, meth = nid.split("::")
    return path[:-3].replace("/", "."), cls, meth


class _InlinePool:
    """Drop-in for ProcessPoolExecutor that runs calls inline but keeps the
    process boundary's pickle round-trip of the callable, arguments and
    result (the part sqlglot's tests exercise, e.g. Expression pickling).
    Spawning real workers would cost ~1 s CPU each (fresh interpreter
    importing duckdb/pandas), once per host CPU."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None,
                 initargs=(), **kw):
        import pickle
        self._p = pickle
        if initializer is not None:
            fn, args = pickle.loads(pickle.dumps((initializer, tuple(initargs))))
            fn(*args)

    def submit(self, fn, /, *args, **kwargs):
        from concurrent.futures import Future
        p = self._p
        f = Future()
        try:
            fn2, a2, k2 = p.loads(p.dumps((fn, args, kwargs)))
            f.set_result(p.loads(p.dumps(fn2(*a2, **k2))))
        except BaseException as e:  # noqa: BLE001
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


def _install_inline_pool() -> None:
    import concurrent.futures as cf
    import concurrent.futures.process as cfp
    cf.ProcessPoolExecutor = _InlinePool
    cfp.ProcessPoolExecutor = _InlinePool


_DEBUG = os.environ.get("BLAST_VERIFY_DEBUG")


def _debug(test, err) -> None:
    if not _DEBUG:
        return
    import traceback
    with open(_DEBUG, "a") as f:
        f.write(f"== {getattr(test, '_nid', test)}\n")
        f.write("".join(traceback.format_exception(*err))[-3000:])


def _child(overlay: str, nids: list[str], per_test_s: float, wfd: int) -> None:
    import importlib
    import unittest

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.chdir(overlay)
    sys.path.insert(0, overlay)
    sys.dont_write_bytecode = True
    os.environ["SKIP_INTEGRATION"] = "0"
    for m in [m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot.")
              or m == "tests" or m.startswith("tests.")]:
        del sys.modules[m]
    importlib.invalidate_caches()
    # Tests use ProcessPoolExecutor(spawn), which re-imports __main__ in the
    # spawned interpreter.  Give it an inert __main__ so nothing of ours re-runs.
    import types
    sys.modules["__main__"] = types.ModuleType("__main__")
    _install_inline_pool()

    out: dict[str, str] = {}
    w = os.fdopen(wfd, "w")

    def emit(nid, st):
        out[nid] = st
        w.write(json.dumps([nid, st]) + "\n")
        w.flush()

    class _TO(BaseException):
        pass

    def on_alarm(signum, frame):
        raise _TO()

    signal.signal(signal.SIGALRM, on_alarm)

    class R(unittest.TestResult):
        """Per-test outcome; a per-test timer starts in startTest."""

        def __init__(self):
            super().__init__()
            self.bad: set[str] = set()
            self.skipped_ids: set[str] = set()
            self.done: set[str] = set()
            self.stray_error = False

        def startTest(self, test):
            super().startTest(test)
            signal.setitimer(signal.ITIMER_REAL, per_test_s)

        def stopTest(self, test):
            signal.setitimer(signal.ITIMER_REAL, 0)
            super().stopTest(test)
            nid = getattr(test, "_nid", None)
            if nid:
                self.done.add(nid)

        def _mark(self, test):
            nid = getattr(test, "_nid", None)
            if nid:
                self.bad.add(nid)
            else:  # setUpClass / module fixture error
                self.stray_error = True

        def addError(self, test, err):
            super().addError(test, err)
            self._mark(test)
            _debug(test, err)

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._mark(test)
            _debug(test, err)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self._mark(test)
                _debug(test, err)

        def addUnexpectedSuccess(self, test):
            self._mark(test)

        def addSkip(self, test, reason):
            nid = getattr(test, "_nid", None)
            if nid:
                self.skipped_ids.add(nid)
            else:
                self.stray_error = True  # whole class skipped: unverified

    groups: dict[str, list[tuple[str, str, str]]] = {}
    for nid in nids:
        mod, cls, meth = _module_of(nid)
        groups.setdefault(mod, []).append((nid, cls, meth))

    for mod, items in groups.items():
        try:
            m = importlib.import_module(mod)
        except BaseException:
            _debug(mod, sys.exc_info())
            for nid, _, _ in items:
                emit(nid, FAIL)
            continue
        tests = []
        for nid, cls, meth in items:
            try:
                t = getattr(m, cls)(meth)
                t._nid = nid
                tests.append(t)
            except BaseException:
                emit(nid, FAIL)  # method vanished / class broken
        r = R()
        try:
            unittest.TestSuite(tests).run(r)
        except BaseException:
            pass  # leftovers stay unverified
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        for t in tests:
            nid = t._nid
            if nid in r.bad:
                emit(nid, FAIL)
            elif r.stray_error or nid not in r.done:
                continue  # unverified
            elif nid in r.skipped_ids:
                emit(nid, SKIP)
            else:
                emit(nid, PASS)
    w.close()


def run_tests(overlay: str, nids: list[str], deadline_s: float,
              per_test_s: float = 10.0) -> dict[str, str]:
    """Run ``nids`` against ``overlay`` in a forked child.  Returns nid ->
    pass/fail/skip for tests that reported; missing = unverified."""
    sys.stdout.flush()
    sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        try:
            os.close(r)
            os.setpgid(0, 0)
            _child(overlay, nids, per_test_s, w)
        finally:
            os._exit(0)
    os.close(w)
    try:
        os.setpgid(pid, pid)
    except OSError:
        pass
    res: dict[str, str] = {}
    buf = b""
    t_end = time.monotonic() + deadline_s
    import select as _sel
    killed = False
    while True:
        left = t_end - time.monotonic()
        if left <= 0:
            killed = True
            break
        rl, _, _ = _sel.select([r], [], [], left)
        if not rl:
            continue
        chunk = os.read(r, 65536)
        if not chunk:
            break
        buf += chunk
    os.close(r)
    if killed:
        for target in (-pid, pid):
            try:
                os.kill(target, signal.SIGKILL)
            except OSError:
                pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    # reap any grandchildren the tests left behind in our process group
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    lines = buf.decode("utf-8", "replace").split("\n")
    if killed and lines:
        lines = lines[:-1]  # possibly truncated
    for line in lines:
        if not line:
            continue
        try:
            nid, st = json.loads(line)
            res[nid] = st
        except Exception:
            pass
    return res


def verify(patch_text: str, nids: list[str], work: str, deadline_s: float,
           per_test_s: float = 10.0, root: str = PRISTINE) -> dict[str, str]:
    """Apply + overlay + run.  Raises PatchError when the patch does not apply."""
    changes = apply_patch(patch_text, root)
    shutil.rmtree(work, ignore_errors=True)
    try:
        build_overlay(work, changes, root)
        return run_tests(work, nids, deadline_s, per_test_s)
    finally:
        shutil.rmtree(work, ignore_errors=True)
