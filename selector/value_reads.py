"""Recognize single value edits whose only pinned consumers cannot observe them."""
import ast
import functools
import json
from pathlib import Path


@functools.cache
def profile():
    keys, fields = json.loads((Path(__file__).parent / "maps/validation-reads.json").read_text())
    return set(keys), fields


def bool_switch(a, b):
    return (isinstance(a, ast.Constant) and isinstance(b, ast.Constant)
            and type(a.value) is bool and type(b.value) is bool and a.value != b.value)


def same_file_with_value(old, new, values, i, original):
    saved = values[i]
    try:
        values[i] = original
        return ast.dump(old) == ast.dump(new)
    finally:
        values[i] = saved


def inert(path, old, new, mro):
    expression = path.startswith("sqlglot/expressions/")
    parser = path in {f"sqlglot/parsers/{name}.py" for name in ("mysql", "singlestore", "duckdb", "snowflake")}
    if not expression and not parser:
        return False
    keys, missing = profile()
    for cls, other in zip(old.body, new.body):
        if not isinstance(cls, ast.ClassDef) or not isinstance(other, ast.ClassDef):
            continue
        # These pinned classes feed other dictionaries through explicit class
        # references. Their derived required sets need separate evidence.
        if cls.name in {"Concat", "Grant", "Cast", "Expr", "Expression"}:
            continue
        for a, b in zip(cls.body, other.body):
            if not isinstance(a, ast.Assign) or not isinstance(b, ast.Assign):
                continue
            if len(a.targets) != 1 or not isinstance(a.targets[0], ast.Name):
                continue
            name = a.targets[0].id
            if name != ("arg_types" if expression else "SHOW_PARSERS"):
                continue
            x, y = a.value, b.value
            if not isinstance(x, ast.Dict) or not isinstance(y, ast.Dict) or len(x.keys) != len(y.keys):
                continue
            for i, (key, v, w) in enumerate(zip(x.keys, x.values, y.values)):
                if not isinstance(key, ast.Constant) or type(key.value) is not str:
                    continue
                if expression:
                    cls_id = path[:-3].replace("/", ".") + "." + cls.name
                    if ("sqlglot.expressions.core.Expr" in mro.get(cls_id, ())
                            and bool_switch(v, w) and key.value not in missing.get(cls_id, ())):
                        return same_file_with_value(old, new, y.values, i, v)
                elif (key.value not in keys and isinstance(v, ast.Call) and isinstance(w, ast.Call)
                      and isinstance(v.func, ast.Name) and v.func.id == "_show_parser"
                      and len(v.keywords) == len(w.keywords)):
                    for kw, replacement in zip(v.keywords, w.keywords):
                        if kw.arg and kw.arg == replacement.arg and bool_switch(kw.value, replacement.value):
                            saved = replacement.value
                            try:
                                replacement.value = kw.value
                                return ast.dump(old) == ast.dump(new)
                            finally:
                                replacement.value = saved
    return False
