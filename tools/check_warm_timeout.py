"""Exercise a forked verifier under the static selector's real CPU handler."""
from pathlib import Path
import signal
import tempfile
import time

from selector import main, warm_replay


def check():
    previous = signal.signal(signal.SIGPROF, main._alarm)
    try:
        with tempfile.TemporaryDirectory() as overlay, tempfile.TemporaryFile() as receipt:
            script = Path(overlay) / "catch_parse_error.py"
            script.write_text("try:\n    while True:\n        pass\nexcept Exception:\n    pass\n")
            outcome = warm_replay.run(script, "test", [], overlay, time.monotonic() + 3,
                                      "", receipt, cpu_limit=0.1)
            if outcome != "timeout" or receipt.read():
                raise AssertionError(f"expected timeout without a receipt, got {outcome}")
    finally:
        signal.signal(signal.SIGPROF, previous)


if __name__ == "__main__":
    check()
