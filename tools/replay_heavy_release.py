"""Replay local partitions for the host-only heavy monotonicity release check.

Use the same revision, generated directory and batch size for all images.
All registered diffs run, including those without an audited outcome. Frozen
matched cases run first in their historical batches. Additional cases then
run in separate batches. Only catalog and patch bytes enter the container.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from measure_heavy import FLAGS, ROOT, WRAPPER, score_lib


def answers(directory, revision):
    proc = subprocess.run(["git", "show", f"{revision}:{directory}/answers.json"],
                          cwd=ROOT, capture_output=True, check=True)
    return json.loads(proc.stdout)["commits"]


def cohorts(partition, generated, revision):
    if partition in {"generated", "stress"}:
        yield partition, [(p, None) for p in sorted(generated.glob("*.patch"))]
        return
    seen = set()
    groups = defaultdict(list)
    for name in ("sample", "dev", "exotic", "revert", "holdout", "transfer"):
        directory = Path("eval/cases") / name
        meta = json.loads((ROOT / directory / "meta.json").read_text())
        matched = answers(directory, "8cba26f")
        labels = answers(directory, revision)
        for patch in sorted((ROOT / directory / "commits").glob("*.patch")):
            key = patch.stem
            if meta[key]["split"] == "dev":
                seen.add(patch.read_bytes())
            if meta[key]["split"] != partition:
                continue
            record = labels.get(key)
            failures = (set(record["failing"]) if record is not None
                        and not record.get("basis") else None)
            old = key in matched and not matched[key].get("basis")
            groups[old, name].append((patch, failures))
    for (matched, name), cases in groups.items():
        if partition == "holdout":
            cases = [(p, f) for p, f in cases if p.read_bytes() not in seen]
        yield ("matched" if matched else "extra") + "/" + name, cases


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("image")
    ap.add_argument("catalog", type=Path)
    ap.add_argument("generated", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--partition", choices=("dev", "holdout", "generated", "stress"), required=True)
    ap.add_argument("--extra-only", action="store_true",
                    help="Reuse already captured matched and generated batches")
    args = ap.parse_args()
    catalog = args.catalog.resolve()
    tests = json.loads(catalog.read_text())["tests"]
    dur = {r["number"]: round(r["seconds"] * 1000) for r in tests}
    totals = defaultdict(Counter)
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / (args.partition + ".plans")).open("xb") as plans, (
            args.out / (args.partition + ".trace")).open("xb") as trace:
        for cohort, cases in cohorts(args.partition, args.generated, args.labels):
            if args.extra_only and not cohort.startswith("extra/"):
                continue
            batch_size = 60 if args.partition == "stress" else 40
            for offset in range(0, len(cases), batch_size):
                rows = cases[offset:offset + batch_size]
                with tempfile.TemporaryDirectory() as tmp:
                    batch = Path(tmp)
                    batch.chmod(0o755)
                    for patch, _ in rows:
                        shutil.copyfile(patch, batch / patch.name)
                    proc = subprocess.run(["docker", "run", *FLAGS, "-v",
                        f"{catalog}:/data/tests.json:ro", "-v", f"{batch}:/data/commits:ro",
                        args.image, "python", "-c", WRAPPER], capture_output=True,
                        timeout=270 if args.partition == "stress" else 185, check=True)
                result = score_lib.evaluate(proc.stdout, tests, [p.stem for p, _ in rows])
                if not result["ok"]:
                    raise ValueError(result)
                plans.write(proc.stdout)
                plans.flush()
                trace.write(proc.stderr)
                trace.flush()
                stats = totals[cohort]
                stats["count"] += len(rows)
                stats["cpu"] += float(proc.stderr.splitlines()[-1].split()[1])
                for patch, failures in rows:
                    if failures is not None:
                        workers = result["plans"][patch.stem]
                        stats["labelled"] += 1
                        stats["misses"] += bool(failures - set().union(*workers))
                        stats["labelled_sim_ms"] += score_lib.commit_cost_ms(workers, dur)
    print(json.dumps(totals, indent=2))


if __name__ == "__main__":
    main()
