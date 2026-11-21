"""Expose the last verifier stage even if a child crashes or times out."""
import os


def mark(name):
    fd = os.environ.get("BLAST_PHASE_FD")
    if fd is not None:
        os.pwrite(int(fd), name.encode().ljust(16), 0)
