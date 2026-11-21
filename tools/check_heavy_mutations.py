"""Compare probe decisions on generated patches with independent pytest labels."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from measure_heavy import FLAGS

# The probe container receives patches alone. Full-suite labels are read by
# this outer process after it has received the verdicts.
CHECK = """
import json,sys,tempfile,time
from pathlib import Path
from selector import heavy,verify
if "--prewarm" in sys.argv:
    import resource
    limit=1800*1024**2
    resource.setrlimit(resource.RLIMIT_AS,(limit,limit))
    assert heavy.warm_replay.prepare()
for path in sorted(Path("/patches").glob("*.patch")):
    patch=path.read_text()
    outcomes={}
    for group in sorted(set(heavy.TARGETS.values())):
        start=heavy.cpu()
        result="hazard"
        mode=None
        try:
            edit=heavy.stage_edit(patch,group)
            if edit is not None:
                mode,source,changes,cases=edit
                with tempfile.TemporaryDirectory() as overlay:
                    verify.build_overlay(overlay,changes)
                    args=[json.dumps(cases)] if mode in {"partial","slice"} else []
                    result=heavy.replay(mode,args,overlay,time.monotonic()+30,source)
        except Exception as exc:
            result=type(exc).__name__
        outcomes[group]=[mode,result,round(heavy.cpu()-start,4)]
    print(json.dumps([path.stem,outcomes]),flush=True)
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("directory", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument(
        "--wait-count",
        type=int,
        default=0,
        help="Continue as oracle labels arrive, up to this many labelled patches.",
    )
    ap.add_argument("--prewarm", action="store_true",
                    help="Exercise the forked verifier when the image supports it.")
    args = ap.parse_args()
    identity = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.image], text=True
    ).strip()
    report = (
        json.loads(args.output.read_text())
        if args.output.exists()
        else {"image": identity, "decisions": {}}
    )
    if report["image"] != identity:
        raise ValueError("cached probe decisions use a different image")
    if report.get("prewarm", False) != args.prewarm and report["decisions"]:
        raise ValueError("cached probe decisions use a different prewarm setting")
    report["prewarm"] = args.prewarm
    while True:
        for p in sorted(args.directory.glob("*.json")):
            if p.stem in report["decisions"]:
                continue
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                root.chmod(0o755)
                shutil.copyfile(p.with_suffix(".patch"), root / (p.stem + ".patch"))
                cmd = [
                    "docker",
                    "run",
                    *FLAGS,
                    "-v",
                    f"{root}:/patches:ro",
                    identity,
                    "python",
                    "-c",
                    CHECK,
                    *(["--prewarm"] if args.prewarm else []),
                ]
                res = subprocess.run(
                    cmd, capture_output=True, text=True, check=True, timeout=100
                )
                key, outcomes = json.loads(res.stdout)
                report["decisions"][key] = outcomes
            counts = defaultdict(Counter)
            for key, outcomes in report["decisions"].items():
                labels = json.loads((args.directory / (key + ".json")).read_text())
                for group, (mode, result, cpu) in outcomes.items():
                    suffix = {"ds": "test_execute_tpcds", "parser": "test_max_nodes",
                              "generator": "test_generator_memory"}[group]
                    truth = next((v for k, v in labels.items() if k.endswith("::" + suffix)),
                                 "unlabelled")
                    counts[group][
                        truth + ("_drop" if result == "pass" else "_keep")
                    ] += 1
                    counts[group]["cpu"] += cpu
            report["matrix"] = counts
            args.output.write_text(json.dumps(report, indent=2))
        if len(report["decisions"]) >= args.wait_count:
            return
        time.sleep(10)


if __name__ == "__main__":
    main()
