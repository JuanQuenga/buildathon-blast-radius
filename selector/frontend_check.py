"""Check pinned frontend fingerprints without opening tables or running SQL."""
import json
import os
from pathlib import Path
import pickle
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(1, str(Path(__file__).parent))
from staged import digest


def check(indices):
    from sqlglot import parse_one, transpile
    from sqlglot.optimizer.optimizer import optimize
    import ast
    from sqlglot.executor import ensure_schema
    tree = ast.parse(Path("tests/helpers.py").read_text())
    schema = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "TPCDS_SCHEMA" for t in n.targets))
    schema = ensure_schema(schema, dialect=None)

    inputs = json.loads(Path("/app/pin-cache/ds-inputs.json").read_text())
    expected = json.loads(Path("/app/staged.json").read_text())["ds"]
    for index in indices:
        sql, row = inputs[index], expected[index]
        expr = pickle.loads(pickle.dumps(parse_one(sql)))
        if digest(expr.dump()) != row[2]:
            raise ValueError("parsed or serialized expression changed")
        if len(row) > 5:
            if digest(transpile(sql, write="duckdb")) != row[4]:
                raise ValueError("reference SQL changed")
            optimized = optimize(expr, schema, leave_tables_isolated=True, dialect=None)
            if digest(optimized.dump()) != row[5]:
                raise ValueError("optimized expression changed")


if __name__ == "__main__":
    check(json.loads(sys.argv[2]))
