#!/usr/bin/env python3
"""Revert the production hunk of upstream fix commits onto the pin.

  eval/gen/reverts.py --set revert --max 120 [--skip 0] [--max-lines 40]

Walks first-parent "fix..." commits before the pin (newest first), takes
``git diff C C^ -- sqlglot/`` (production code only, tests untouched), and keeps
it when it applies cleanly to the pin.  The regression tests usually still
exist on the pin, so these are realistic, subtle, single-feature breakages.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from build import add_case  # noqa: E402
from patchmaker import GEN, ensure_gen, reset  # noqa: E402
from paths import CASES, PIN  # noqa: E402


def git(*a, input=None, check=True):
    p = subprocess.run(["git", *a], cwd=GEN, input=input, capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(p.stderr)
    return p


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="revert")
    ap.add_argument("--max", type=int, default=120)
    ap.add_argument("--skip", type=int, default=0, help="skip the newest N fix commits")
    ap.add_argument("--every", type=int, default=1, help="take every k-th candidate")
    ap.add_argument("--max-lines", type=int, default=40)
    a = ap.parse_args(argv)
    ensure_gen()
    log = git("log", "--first-parent", "--format=%H %s", PIN).stdout.splitlines()
    fixes = [l.split(" ", 1) for l in log[1:] if l.split(" ", 1)[1].lower().startswith("fix")]
    fixes = fixes[a.skip::a.every]
    set_dir = CASES / a.set
    meta_path = set_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    seen = {p.read_text() for p in (set_dir / "commits").glob("*.patch")} if (set_dir / "commits").exists() else set()
    made = 0
    for sha, subj in fixes:
        if made >= a.max:
            break
        d = git("diff", sha, f"{sha}^", "--", "sqlglot/", check=False).stdout
        if not d.strip():
            continue
        changed = sum(1 for l in d.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---")))
        if changed > a.max_lines:
            continue
        reset()
        if git("apply", "--check", "-", input=d, check=False).returncode:
            continue
        git("apply", "-", input=d)
        files = [l for l in git("diff", "--name-only").stdout.splitlines()]
        contents = [("write", f, (GEN / f).read_text()) for f in files]
        reset()
        cid = add_case(set_dir, meta, contents, f"revert {sha[:10]} {subj[:80]}", "revert", "revert-fix", seen)
        if cid:
            meta[cid]["upstream"] = sha
            made += 1
    meta_path.write_text(json.dumps(dict(sorted(meta.items())), indent=1) + "\n")
    print(f"{a.set}: +{made} revert cases ({len(meta)} total)", file=sys.stderr)


if __name__ == "__main__":
    main()
