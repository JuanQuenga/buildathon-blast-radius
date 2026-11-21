"""Turn file edits against the pin into git-style unified diffs (like the organisers')."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paths import PIN, WORKBENCH  # noqa: E402

GEN = WORKBENCH / "gen"  # private clone at the pin used only to render diffs


def _git(*args, input=None, check=True) -> str:
    p = subprocess.run(["git", "-c", "core.abbrev=7", *args], cwd=GEN, input=input,
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {args}: {p.stderr}")
    return p.stdout


def ensure_gen() -> None:
    if not (GEN / ".git").exists():
        subprocess.run(["git", "clone", "-q", str(WORKBENCH / "base"), str(GEN)], check=True)
    _git("checkout", "-q", "--detach", PIN)
    reset()


def reset() -> None:
    _git("reset", "-q", "--hard", PIN)
    _git("clean", "-fdq")


def read(path: str) -> str:
    return (GEN / path).read_text(encoding="utf-8")


def make_patch(changes: list[tuple]) -> str:
    """changes: ("write", path, text) | ("delete", path) | ("rename", old, new[, text])."""
    reset()
    try:
        for ch in changes:
            op = ch[0]
            if op == "write":
                p = GEN / ch[1]
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(ch[2], encoding="utf-8")
            elif op == "delete":
                (GEN / ch[1]).unlink()
            elif op == "rename":
                src, dst = GEN / ch[1], GEN / ch[2]
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
                if len(ch) > 3 and ch[3] is not None:
                    dst.write_text(ch[3], encoding="utf-8")
            else:
                raise ValueError(op)
        _git("add", "-A")
        diff = _git("diff", "--cached", "-M", "--no-color", "--no-ext-diff")
    finally:
        reset()
    return diff


def case_id(seed_text: str) -> str:
    """4-char opaque id like the official ones (base36, from a hash)."""
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    h = int(hashlib.sha256(seed_text.encode()).hexdigest(), 16)
    s = ""
    for _ in range(4):
        h, r = divmod(h, 36)
        s += alphabet[r]
    return s
