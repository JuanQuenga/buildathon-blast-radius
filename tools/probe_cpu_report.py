"""Summarize host-side selector receipt traces, allocating each attempt once."""
import argparse
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median


def summarize(paths, group=None):
    by_patch = Counter()
    by_stage = defaultdict(list)
    groups = Counter()
    total = supervisor = queue = 0.0
    for path in paths:
        for line in path.read_text().splitlines():
            row = line.split()
            if row[:1] == ["cpu"]:
                total += float(row[1])
            elif row[:1] == ["probe-cpu"]:
                supervisor += float(row[1])
            elif row[:1] == ["probe-queue"]:
                queue += float(row[2])
            elif row[:1] == ["heavy"] and len(row) >= 8:
                _, key, name, receipt, mode, seconds, _, stage, *_ = row
                seconds = float(seconds)
                groups[name] += seconds
                if group is None or name == group:
                    by_patch[key] += seconds
                    by_stage[stage, receipt].append(seconds)
    print(f"total {total:.3f} supervisor {supervisor:.3f} queue {queue:.3f}")
    print("group CPU", {name: round(s, 3) for name, s in groups.items()})
    print("per-patch CPU buckets (attempted patches only)")
    for lo, hi in ((0, 1), (1, 3), (3, 5), (5, 10), (10, float("inf"))):
        print(lo, hi, sum(lo < s <= hi for s in by_patch.values()))
    print("terminal stage | receipt | attempts | total CPU | median | max | over 3")
    for (stage, receipt), costs in sorted(by_stage.items()):
        print(f"{stage} | {receipt} | {len(costs)} | {sum(costs):.3f} | "
              f"{median(costs):.3f} | {max(costs):.3f} | {sum(s > 3 for s in costs)}")
    return by_patch


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", nargs="+", type=Path)
    parser.add_argument("--group")
    parser.add_argument("--show-patches", action="store_true")
    args = parser.parse_args()
    rows = summarize(args.trace, args.group)
    if args.show_patches:
        for key, seconds in rows.most_common():
            if seconds > 3:
                print(key, round(seconds, 3))
