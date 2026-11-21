"""Compare host-side plan receipts on identical batches, including free riders.

Pass each partition's two JSONL files in the same invocation. Each file
must contain exactly one complete plan per commit in that partition.
"""
import argparse
import json
from pathlib import Path

from measure_heavy import score_lib
from replay_heavy_release import cohorts


def receipts(path, tests):
    raw = path.read_bytes()
    commits = [json.loads(line)["commit"] for line in raw.splitlines()]
    if not commits or len(commits) != len(set(commits)):
        raise ValueError(f"empty or duplicate receipts in {path}")
    result = score_lib.evaluate(raw, tests, commits)
    if not result["ok"]:
        raise ValueError(f"invalid plans in {path}: {result}")
    return {key: set().union(*workers) for key, workers in result["plans"].items()}


def compare(old, new, targets):
    if old.keys() != new.keys():
        raise ValueError("candidate and baseline cover different patches")
    return [(key, targets[number]) for key in sorted(old)
            for number in sorted((new[key] - old[key]) & targets.keys())]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--generated-patches", required=True, type=Path)
    parser.add_argument("--labels", required=True, help="frozen git revision of the label registry")
    parser.add_argument("--matched-only", action="store_true")
    for part in ("tuning", "holdout", "generated"):
        parser.add_argument("--historical-" + part, type=Path, action="append", default=[],
                            help="successful probe trace from a prior run of the baseline")
        parser.add_argument("--" + part, type=Path, nargs=2, required=True,
                            metavar=("BASELINE", "CANDIDATE"))
    args = parser.parse_args()
    tests = json.loads(args.catalog.read_text())["tests"]
    nodes = {"tests/test_executor.py::TestExecutor::test_execute_tpcds": "ds",
             "tests/test_generator.py::TestGenerator::test_generator_memory": "generator",
             "tests/test_parser.py::TestParser::test_max_nodes": "parser"}
    by_group = {nodes[r["id"]]: r["number"] for r in tests if r["id"] in nodes}
    targets = {row["number"]: row["id"].split("::")[-1] for row in tests
               if row["id"] in nodes}
    if len(targets) != len(nodes):
        raise ValueError("missing or ambiguous heavy test IDs")
    lost = 0
    for part in ("tuning", "holdout", "generated"):
        old, new = (receipts(path, tests) for path in getattr(args, part))
        partition = "dev" if part == "tuning" else part
        expected = {p.stem for cohort, cases in cohorts(partition, args.generated_patches, args.labels)
                    if not args.matched_only or not cohort.startswith("extra/")
                    for p, _ in cases}
        if not expected or old.keys() != expected:
            raise ValueError(f"incomplete {part} baseline ({len(old)} of {len(expected)} expected)")
        for trace in getattr(args, "historical_" + part):
            for line in trace.read_text().splitlines():
                fields = line.split()
                if len(fields) >= 7 and fields[0] == "heavy" and fields[3] == "pass":
                    key, group = fields[1:3]
                    if group in by_group and key in old:
                        old[key].discard(by_group[group])
        losses = compare(old, new, targets)
        lost += len(losses)
        print(part, len(old), "patches", len(losses), "regressions")
        for test in sorted(targets.values()):
            print(test, sum(node == test for _, node in losses))
        if part != "holdout":
            for key, node in losses:
                print(key, node)
    return bool(lost)


if __name__ == "__main__":
    raise SystemExit(main())
