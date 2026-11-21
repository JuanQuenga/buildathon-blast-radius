"""Find an omitted pure string wrapper in an otherwise identical generator."""
import ast
import functools
import json
from pathlib import Path


@functools.cache
def receipts():
    return json.loads((Path(__file__).parent / "maps/string-calls.json").read_text())


def transparent_for(path, old, new):
    if path != "sqlglot/generator.py" or not isinstance(old, ast.FunctionDef):
        return ()
    key = path + ":Generator." + old.name
    tests = receipts().get(key, ())
    if not tests:
        return ()
    # At the pin this method has a single implementation, performs no writes,
    # and returns either the builtin str input or str.replace on that input.
    # Match the only delta by substituting the argument into the original tree.
    for node in ast.walk(old):
        for field, call in ast.iter_fields(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "_replace_line_breaks"
                    and isinstance(call.func.value, ast.Name) and call.func.value.id == "self"
                    and len(call.args) == 1 and isinstance(call.args[0], ast.Name) and not call.keywords):
                continue
            setattr(node, field, call.args[0])
            try:
                if ast.dump(old) == ast.dump(new):
                    return tests
            finally:
                setattr(node, field, call)
    return ()
