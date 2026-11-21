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
    if group == "slice":
        active = json.loads(sys.argv[2])["active"]
        if active and not os.environ.get("BLAST_CHANGED_MODULE", "").startswith("sqlglot.optimizer."):
            mark("parse")
            from sqlglot import parse_one
            inputs = json.loads(Path("/app/pin-cache/ds-inputs.json").read_text())
            rows = json.loads(staged.BASE.read_text())["ds"]
            for i in active[:6]:
                expr = pickle.loads(pickle.dumps(parse_one(inputs[i])))
                if staged.digest(expr.dump()) != rows[i][2]:
                    raise ValueError("early AST mismatch")
    staged.main()


if __name__ == "__main__":
    main()
