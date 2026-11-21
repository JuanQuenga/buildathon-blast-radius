"""Score the 36 generated fixture patches against fresh pinned pytest outcomes."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

from fixture_regressions import generate
from measure_heavy import FLAGS, score_lib
from selector.impact import apply_hunks, parse_patch

REPORT = """
import json
from pathlib import Path
failed = set()
def pytest_runtest_logreport(report):
    if report.failed:
        failed.add(report.nodeid)
def pytest_sessionfinish(session, exitstatus):
    Path('/oracle/failed.json').write_text(json.dumps(sorted(failed)))
"""
TESTS = ["tests/test_executor.py::TestExecutor::" + name for name in
         ("test_execute_tpcds", "test_execute_tpch", "test_optimized_tpch")]
TESTS += ["tests/test_optimizer.py::TestOptimizer::" + name for name in ("test_tpcds", "test_tpch")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("pin", type=Path)
    parser.add_argument("catalog", type=Path)
    args = parser.parse_args()
    catalog = args.catalog.resolve()
    tests = json.loads(catalog.read_text())["tests"]
    ids = {r["number"]: r["id"] for r in tests}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        patches = root / "patches"
        generate(args.pin, patches)
        files = sorted(patches.glob("*.patch"))
        plan = subprocess.check_output(["docker", "run", *FLAGS, "-v", f"{patches}:/data/commits:ro",
                                        "-v", f"{catalog}:/data/tests.json:ro", args.image], timeout=190)
        result = score_lib.evaluate(plan, tests, [p.stem for p in files])
        if not result["ok"]:
            raise ValueError(result)
        oracle = root / "oracle"
        oracle.mkdir()
        oracle.chmod(0o777)
        (oracle / "fixture_report.py").write_text(REPORT)
        for patch in files:
            fp, = parse_patch(patch.read_text())
            changed, _ = apply_hunks((args.pin / fp.path).read_text(), fp.hunks)
            source = root / "changed.sql"
            source.write_text(changed)
            receipt = oracle / "failed.json"
            receipt.unlink(missing_ok=True)
            proc = subprocess.run(["docker", "run", *FLAGS, "-v", f"{source}:/app/sqlglot/{fp.path}:ro",
                "-v", f"{oracle}:/oracle", "-e", "PYTHONPATH=/oracle:/app/sqlglot", "-w", "/app/sqlglot",
                args.image, "python", "-m", "pytest", "-p", "no:cacheprovider", "-p", "fixture_report",
                "-q", *TESTS], capture_output=True, timeout=180)
            if proc.returncode not in (0, 1) or not receipt.is_file():
                raise RuntimeError(f"incomplete pytest run for {patch.stem}")
            failures = set(json.loads(receipt.read_text()))
            scheduled = {ids[n] for worker in result["plans"][patch.stem] for n in worker}
            if bool(failures) != bool(proc.returncode) or failures - scheduled:
                raise ValueError((patch.stem, failures, scheduled))
        print(f"{len(files)} fixture regressions passed")


if __name__ == "__main__":
    main()
