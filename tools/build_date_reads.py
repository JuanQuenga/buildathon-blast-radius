"""Profile inputs to date-unit lookups during collection and individual tests."""
import json
from pathlib import Path
import subprocess
import sys

from build_keyword_reads import profile

if __name__ == "__main__":
    result = {}
    collection = profile(["--collect-only", "tests"], "date")
    for test in json.loads(Path(sys.argv[1]).read_text())["tests"]:
        if test["seconds"] < 0.5:
            continue
        try:
            result[test["id"]] = sorted(collection | profile([test["id"]], "date"))
        except (ValueError, subprocess.TimeoutExpired) as exc:
            print(exc, file=sys.stderr)
    print(json.dumps(result, sort_keys=True))
