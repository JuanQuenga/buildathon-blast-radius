"""Exercise the observable-plan shortcut and its fallback in the release image."""
import json
from pathlib import Path
import tempfile
import time

from selector import heavy, verify

path = "sqlglot/optimizer/optimizer.py"
old = (Path(verify.PRISTINE) / path).read_text()
point = "    return optimized\n"
assert old.count(point) == 1
cases = [
    ('    optimized.meta["line"] = 999999\n', "pass", "plan"),
    ("    optimized = exp.select(exp.Literal.number(123))\n", "failed", "execute"),
]
for edit, expected, phase in cases:
    with tempfile.TemporaryDirectory() as overlay:
        verify.build_overlay(overlay, {path: old.replace(point, edit + point)})
        selection = {"active": [42], "worker": [42], "execute": []}
        result = heavy.replay("slice", [json.dumps(selection)], overlay, time.monotonic() + 20, path)
        print(result, heavy.replay.phase)
        assert (result, heavy.replay.phase) == (expected, phase)
