"""Matched Docker measurement. Answers stay in the scoring process on the host."""

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
import score_lib  # noqa: E402

FLAGS = """--rm --platform linux/amd64 --network none --cpus 2 --memory 4096m
--memory-swap 4096m --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777
--cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp""".split()
WRAPPER = """import os,sys
pid=os.posix_spawnp("python", ["python","-m","selector.main"], os.environ)
_,status,usage=os.wait4(pid,0)
print("cpu", usage.ru_utime+usage.ru_stime, file=sys.stderr)
sys.exit(os.waitstatus_to_exitcode(status))
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline")
    ap.add_argument("image")
    ap.add_argument("catalog", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--labels", default="8cba26f")
    ap.add_argument("--partition", choices=("dev", "holdout"), required=True)
    args = ap.parse_args()
    catalog = args.catalog.resolve()
    tests = json.loads(catalog.read_text())["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    targets = {
        t["number"]: t["id"].split("::")[-1]
        for t in tests
        if t["id"]
        in {
            "tests/test_parser.py::TestParser::test_max_nodes",
            "tests/test_executor.py::TestExecutor::test_execute_tpcds",
            "tests/test_generator.py::TestGenerator::test_generator_memory",
        }
    }
    groups = defaultdict(list)
    seen = set()
    for name in ("sample", "dev", "exotic", "revert", "holdout", "transfer"):
        directory = Path("eval/cases") / name
        meta = json.loads((ROOT / directory / "meta.json").read_text())
        result = subprocess.run(
            ["git", "show", f"{args.labels}:{directory}/answers.json"],
            cwd=ROOT,
            capture_output=True,
        )
        answers = json.loads(result.stdout)["commits"] if result.returncode == 0 else {}
        for path in sorted((ROOT / directory / "commits").glob("*.patch")):
            split = meta[path.stem]["split"]
            if split == "dev":
                seen.add(path.read_bytes())
            if (
                split == args.partition
                and path.stem in answers
                and not answers[path.stem].get("basis")
            ):
                groups[name].append((path, set(answers[path.stem]["failing"])))
    totals = defaultdict(Counter)
    ledger = []
    for name, cases in groups.items():
        if args.partition == "holdout":
            cases = [c for c in cases if c[0].read_bytes() not in seen]
        for start in range(0, len(cases), 40):
            batch = cases[start : start + 40]
            with tempfile.TemporaryDirectory() as d:
                os.chmod(d, 0o755)
                for path, _ in batch:
                    shutil.copyfile(path, Path(d) / path.name)
                selected = defaultdict(dict)
                for tag, img in (("old", args.baseline), ("new", args.image)):
                    cmd = [
                        "docker",
                        "run",
                        *FLAGS,
                        "-v",
                        f"{d}:/data/commits:ro",
                        "-v",
                        f"{catalog}:/data/tests.json:ro",
                        img,
                        "python",
                        "-c",
                        WRAPPER,
                    ]
                    r = subprocess.run(
                        cmd, capture_output=True, timeout=180, check=True
                    )
                    plans = score_lib.evaluate(
                        r.stdout, tests, [p.stem for p, _ in batch]
                    )
                    assert plans["ok"], plans
                    stats = Counter(
                        patches=len(batch),
                        cpu=float(r.stderr.decode().splitlines()[-1].split()[1]),
                    )
                    for path, labels in batch:
                        plan = plans["plans"][path.stem]
                        sel = set().union(*plan)
                        stats["miss"] += bool(labels - sel)
                        stats["sim_ms"] += score_lib.commit_cost_ms(plan, dur)
                        for num, group in targets.items():
                            selected[path.stem, num][tag] = num in sel
                            if tag == "new" and (
                                num in sel or selected[path.stem, num]["old"]
                            ):
                                outcome = (
                                    ("fail" if num in labels else "pass")
                                    + "_"
                                    + ("keep" if num in sel else "drop")
                                )
                                stats[group + "_" + outcome] += 1
                                if args.partition == "dev":
                                    trace = [
                                        line
                                        for line in r.stderr.decode().splitlines()
                                        if line.startswith("heavy " + path.stem + " ")
                                    ]
                                    ledger.append(
                                        [name, path.stem, group, outcome, trace]
                                    )
                    for row in [args.partition] + (
                        ["sample"] if name == "sample" else []
                    ):
                        totals[row, tag].update(stats)
            args.output.write_text(
                json.dumps(
                    {
                        "totals": {"/".join(k): v for k, v in totals.items()},
                        "ledger": ledger,
                    },
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
