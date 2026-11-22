"""Runtime directory and OS portability helpers (Linux Docker + macOS dev)."""
from __future__ import annotations

import os
import resource
import sys
from pathlib import Path

APP_DIR = Path(os.environ.get("BLAST_APP_DIR", "/app"))
DATA_DIR = Path(os.environ.get("BLAST_DATA", "/data"))
SQLGLOT_DIR = Path(os.environ.get("BLAST_SQLGLOT", str(APP_DIR / "sqlglot")))


def set_memory_limit(mb: int = 1800) -> None:
    """Install virtual-memory cap on Linux (Docker container). No-op on macOS."""
    if sys.platform == "linux":
        cap = mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))


def pin_affinity(cpus: int = 2) -> None:
    """Pin CPU affinity on Linux when available."""
    if hasattr(os, "sched_setaffinity") and hasattr(os, "sched_getaffinity"):
        os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:cpus])
