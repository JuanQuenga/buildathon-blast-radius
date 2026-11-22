"""Verify literal changes in the optimizer's rule argument dictionary."""
import ast
import json
from pathlib import Path


def overridden_defaults(path, before, after):
    """Accept only boolean defaults explicitly supplied on every recorded call."""
    bindings = json.loads((Path(__file__).parent / "maps/rule-bindings.json").read_text())
    if not any(key.startswith(path + ":") for key in bindings):
        return False
    old, new = ast.parse(before), ast.parse(after)
    if len(old.body) != len(new.body):
        return False
    changed = False
    for a, b in zip(old.body, new.body):
        if not isinstance(a, ast.FunctionDef) or not isinstance(b, ast.FunctionDef):
            continue
        names = bindings.get(path + ":" + a.name)
        if not names or a.name != b.name or a.decorator_list or b.decorator_list:
            continue
        if not a.args.defaults or len(a.args.defaults) != len(b.args.defaults):
            continue
        args = (a.args.posonlyargs + a.args.args)[-len(a.args.defaults):]
        for i, (arg, x, y) in enumerate(zip(args, a.args.defaults, b.args.defaults)):
            if (arg.arg in names and isinstance(x, ast.Constant) and isinstance(y, ast.Constant)
                    and type(x.value) is bool and type(y.value) is bool and x.value != y.value):
                b.args.defaults[i] = x
                changed = True
    return changed and ast.dump(old) == ast.dump(new)


