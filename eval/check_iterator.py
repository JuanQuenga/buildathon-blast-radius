"""Recheck the dev patch that stops TableIter from advancing (2zr6).

Run `ordered` for both full sessions, then `isolated clean` and `isolated patched`
under identical cgroups. `certify` writes an explicitly qualified oracle cache
entry only when all 60 isolated outcomes agree with both full suites.
"""
import argparse
import difflib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tempfile
import unittest

import oracle
from paths import BASE, CACHE, CASES, CATALOG, PYTHON, WORKBENCH

DATA = WORKBENCH / "deferred-dev"
PATCH = CASES / "dev/commits/2zr6.patch"
SOURCE = "sqlglot/executor/table.py"
ORIGINAL = "        self.index += 1\n"
STALLED = "        self.index += 0\n"


def ordered():
    tests, skipped = oracle.load_catalog()
    clean = (BASE / SOURCE).read_text()
    assert clean.count(ORIGINAL) == 1
    for steps in (2048, 16384):
        target = DATA / f"guard-{steps}.json"
        if target.is_file():
            continue
        hits = DATA / f"guard-hits-{steps}.txt"
        hits.unlink(missing_ok=True)
        guard = STALLED + f"""        if self.index < len(self.table):
            self._br_steps = getattr(self, "_br_steps", 0) + 1
            if self._br_steps > {steps}:
                import os
                with open({str(hits)!r}, "a") as br_log:
                    br_log.write(os.environ.get("PYTEST_CURRENT_TEST", "") + "\\n")
                raise RuntimeError("Benchmark watchdog: iterator returned the same row repeatedly")
"""
        patched = clean.replace(ORIGINAL, guard)
        diff = difflib.unified_diff(clean.splitlines(True), patched.splitlines(True),
                                    fromfile=f"a/{SOURCE}", tofile=f"b/{SOURCE}")
        result = oracle.label("".join(diff).encode(), tests, skipped, use_cache=False)
        if result["status"] != "ok" or not result["ended"] or result["missing"]:
            raise RuntimeError("Incomplete ordered watchdog session")
        target.write_text(json.dumps(result))


def direct_worker(nodeid, output, growth_mib):
    # unittest methods can emit a real failure before pytest runs out of memory
    # trying to render it. Record that first failure before formatting any traceback.
    sys.path.insert(0, os.getcwd())
    from tests.test_executor import TestExecutor

    fd = os.open(output, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    reserve = bytearray(2 << 20)

    class Result(unittest.TestResult):
        def startTest(self, test):
            super().startTest(test)
            pages = int(Path("/proc/self/statm").read_text().split()[0])
            _, hard = resource.getrlimit(resource.RLIMIT_AS)
            limit = pages * os.sysconf("SC_PAGE_SIZE") + (growth_mib << 20)
            resource.setrlimit(resource.RLIMIT_AS, (limit, hard))

        def record(self, test, err):
            nonlocal reserve
            reserve = None
            evidence = dict(test=test.id(), outcome="failed", exception=err[0].__name__,
                            cause=type(err[1].__cause__).__name__)
            os.write(fd, json.dumps(evidence).encode())
            os._exit(1)

        addFailure = addError = record

        def addSubTest(self, test, subtest, err):
            if err:
                self.record(test, err)

    test = TestExecutor(nodeid.split("::")[-1])
    result = Result()
    unittest.TestSuite([test]).run(result)
    if result.testsRun != 1 or not result.wasSuccessful():
        raise RuntimeError("Missing test outcome")
    os.write(fd, json.dumps(dict(test=test.id(), outcome="passed")).encode())
    os.close(fd)


def executor_tests():
    return [t for t in json.loads(CATALOG.read_text())["tests"] if t["file"] == "tests/test_executor.py"]


def isolated(stage):
    folder = DATA / f"direct-{stage}"
    folder.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=oracle._tmpdir()) as td:
        work = Path(td) / "sqlglot"
        oracle._copy_base(work)
        if stage == "patched":
            ok, error = oracle._apply(PATCH.read_bytes(), work)
            if not ok:
                raise ValueError(error)
        env = dict(PATH=f"{PYTHON.parent}:/usr/bin:/bin", HOME=str(work), LANG="C.UTF-8",
                   SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0",
                   PYTHON_CPU_COUNT="1", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                   MKL_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1")
        for t in executor_tests():
            n = t["number"]
            # These three methods needed more address space in clean controls.
            growth = 2048 if n == 1106 else 1024 if n in (1102, 1119, 1123) else 256
            output = folder / f"{n}.json"
            if output.is_file():
                continue
            tmp = output.with_suffix(".tmp")
            cmd = [*oracle.TASKSET, str(PYTHON), str(Path(__file__).resolve()), "worker",
                   t["id"], str(tmp), str(growth)]
            with subprocess.Popen(cmd, cwd=work, env=env, start_new_session=True) as proc:
                try:
                    rc = proc.wait(timeout=305)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    raise RuntimeError(f"Unconfirmed isolated timeout on {n}")
            data = json.loads(tmp.read_text()) if tmp.is_file() and tmp.stat().st_size else {}
            if rc != {"passed": 0, "failed": 1}.get(data.get("outcome")):
                raise RuntimeError(f"Unconfirmed isolated process exit on {n}: {rc}")
            tmp.replace(output)


def certify():
    # Confirm the frozen patch is precisely the edit under observation.
    with tempfile.TemporaryDirectory(dir=oracle._tmpdir()) as td:
        work = Path(td)
        path = work / SOURCE
        path.parent.mkdir(parents=True)
        clean = (BASE / SOURCE).read_text()
        path.write_text(clean)
        ok, err = oracle._apply(PATCH.read_bytes(), work)
        if not ok or path.read_text() != clean.replace(ORIGINAL, STALLED):
            raise ValueError(f"Iterator patch differs from measured edit: {err}")
        if [p for p in work.rglob("*") if p.is_file()] != [path]:
            raise ValueError("Unexpected extra file in iterator patch")
    tests = executor_tests()
    results = []
    for steps in (2048, 16384):
        saved = json.loads((DATA / f"guard-{steps}.json").read_text())
        hit = {s.rsplit(" (", 1)[0] for s in (DATA / f"guard-hits-{steps}.txt").read_text().splitlines()}
        if (saved["status"] != "ok" or not saved["ended"] or saved["missing"] or
                not hit.issubset({t["id"] for t in tests})):
            raise RuntimeError("Invalid full-suite or unexpected watchdog scope")
        results.append(set(saved["failing"]))
    if results[0] != results[1]:
        raise RuntimeError("Ordered sessions differ")
    for t in tests:
        n = t["number"]
        for stage in ("clean", "patched"):
            row = json.loads((DATA / f"direct-{stage}/{n}.json").read_text())
            expected = "failed" if stage == "patched" and n in results[0] else "passed"
            if row["outcome"] != expected or row["test"] != t["id"].replace(".py::", ".").replace("/", ".").replace("::", "."):
                raise RuntimeError(f"Direct {stage} and ordered results differ for {n}")
    key = oracle.patch_key(PATCH.read_bytes())
    data = dict(status="iterator_crosschecked", key=key, failing=sorted(results[0]),
                missing=[], reasons={}, verification="Two ordered watchdog sessions and 60 paired executor tests")
    out = CACHE / f"{key}.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(out)
    print(f"Confirmed {len(results[0])} failures with both watchdogs and all paired controls")


if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ordered")
    commands.add_parser("certify")
    commands.add_parser("isolated").add_argument("stage", choices=("clean", "patched"))
    worker = commands.add_parser("worker")
    worker.add_argument("nodeid")
    worker.add_argument("output")
    worker.add_argument("growth_mib", type=int)
    args = parser.parse_args()
    if args.command == "ordered":
        ordered()
    elif args.command == "isolated":
        isolated(args.stage)
    elif args.command == "worker":
        direct_worker(args.nodeid, args.output, args.growth_mib)
    else:
        certify()
