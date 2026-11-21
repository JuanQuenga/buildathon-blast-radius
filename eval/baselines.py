#!/usr/bin/env python3
"""Reference selectors for exercising the harness (reads $BLAST_DATA like ours).

  eval/baselines.py starter   # the starter kit: everything on 3 workers (LPT by seconds)
  eval/baselines.py full4     # everything, best LPT over k (k=4 -> 36.817 s/patch)
  eval/baselines.py none      # select nothing
  eval/baselines.py oracle    # CHEATS: reads labels from $BR_ANSWERS (harness self-test only)
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import score_lib as S  # noqa: E402

DATA = Path(os.environ.get("BLAST_DATA", "/data"))


def main():
    mode = sys.argv[1]
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dur = {t["number"]: S.duration_ms(t["seconds"]) for t in tests}
    ids = sorted(p.stem for p in (DATA / "commits").glob("*.patch"))
    if mode == "starter":
        ws = [[] for _ in range(3)]
        loads = [0.0] * 3
        for t in sorted(tests, key=lambda t: t["seconds"], reverse=True):
            i = loads.index(min(loads))
            ws[i].append(t["number"])
            loads[i] += t["seconds"]
        plans = {c: ws for c in ids}
    elif mode == "full4":
        _, ws = S.best_schedule([t["number"] for t in tests], dur)
        plans = {c: ws for c in ids}
    elif mode == "none":
        plans = {c: [] for c in ids}
    elif mode == "oracle":
        ans = {}
        for f in os.environ["BR_ANSWERS"].split(","):
            ans.update(json.loads(Path(f).read_text())["commits"])
        plans = {c: S.best_schedule(ans[c]["failing"], dur)[1] for c in ids}
    else:
        sys.exit(f"unknown mode {mode}")
    for c in ids:
        print(json.dumps({"commit": c, "workers": plans[c]}, separators=(",", ":")))


if __name__ == "__main__":
    main()
