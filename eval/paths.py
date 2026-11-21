"""Locations used by the evaluation tools (override with environment variables).

Nothing under eval/ is used by the selector at runtime or copied into the image.
"""

from __future__ import annotations

import os
from pathlib import Path

EVAL = Path(__file__).resolve().parent
REPO = EVAL.parent
_SCRATCH = Path("/home/juan/.t3/scratch/2026-11-20-you-are-the-coordinator-for-8d9ce8ed/blast-radius")

# Bulky outputs (oracle cache, pytest logs, harness run logs) live outside the repo.
WORKBENCH = Path(os.environ.get("BR_WORKBENCH", _SCRATCH / "work-bench"))
# A private clone of sqlglot at the pin (own .git). Copied per case, never modified.
BASE = Path(os.environ.get("BR_BASE", WORKBENCH / "base"))
# Python 3.13 venv with the starter's requirements.lock installed.
PYTHON = Path(os.environ.get("BR_PYTHON", WORKBENCH / "venv" / "bin" / "python"))
STARTER = Path(os.environ.get("BR_STARTER", _SCRATCH / "starter" / "sample-kit"))
CATALOG = Path(os.environ.get("BR_CATALOG", STARTER / "sample" / "tests.json"))
CASES = EVAL / "cases"
CACHE = WORKBENCH / "oracle-cache"
PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
