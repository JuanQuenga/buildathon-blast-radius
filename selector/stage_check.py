"""Check cheap query fingerprints before importing the executor."""
import json
import os
from pathlib import Path
import pickle
import sys

sys.path.insert(0, str(Path(__file__).parent))
import staged
from phase import mark


def main():
    group = sys.argv[1]
    if group == "imported":
        return
    if group == "slice" and not os.environ.get("BLAST_CHANGED_MODULE", "").startswith("sqlglot.optimizer."):
        # A parser or serialization exception is conclusive. Changed values
        # continue to ordered-result verification below.
        from sqlglot import parse_one
        inputs = json.loads(Path("/app/pin-cache/ds-inputs.json").read_text())
        mark("parse")
        for i in json.loads(sys.argv[2])["active"][:6]:
            pickle.loads(pickle.dumps(parse_one(inputs[i])))
    staged.main()


if __name__ == "__main__":
    main()
