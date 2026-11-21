"""Verify literal changes in the optimizer's rule argument dictionary."""
import ast
import hashlib
import json
from pathlib import Path
import pickle


def literal_change(before, after):
    old, new = ast.parse(before), ast.parse(after)
    def mapping(tree):
        fn = next(f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == "optimize")
        return next(n.value for n in fn.body if isinstance(n, ast.Assign) and
                    len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and
                    n.targets[0].id == "possible_kwargs" and isinstance(n.value, ast.Dict))
    try:
        a, b = mapping(old), mapping(new)
    except StopIteration:
        return None
    for j, (x, y) in enumerate(zip(a.values, b.values)):
        if (not isinstance(x, ast.Constant) or not isinstance(y, ast.Constant)
                or type(x.value) is not bool or type(y.value) is not bool or x.value == y.value):
            continue
        key = a.keys[j]
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            continue
        b.values[j] = x
        if ast.dump(old) == ast.dump(new):
            return [key.value, x.value, y.value]
        b.values[j] = y
    return None


def replay(change):
    import inspect
    from sqlglot.optimizer.optimizer import RULES
    from rule_corpus import fingerprint, record_at
    from staged import check_optimized
    from phase import mark

    key, old, new = change
    # The pin's sole SQLGlot caller passes this one override explicitly.
    # Its value takes precedence over the dictionary literal.
    if key == "leave_tables_isolated":
        raise ValueError("explicit executor argument")
    queries = json.loads((Path(__file__).parent / "maps/rule-queries.json").read_text())
    by_query = {}
    for j, q in enumerate(queries):
        by_query.setdefault(q, []).append(j)
    if len(by_query) != 57:
        raise ValueError("incomplete executed queries")
    consumes = [key in inspect.getfullargspec(rule).args for rule in RULES]
    for q, chain in by_query.items():
        if len(chain) != len(RULES):
            raise ValueError("incomplete rule chain")
        result = None
        schema = None
        schema_changed = False
        for j, rule, affected in zip(chain, RULES, consumes):
            if result is None and not affected:
                continue
            mark("rules")
            module, name, inputs, after, expected, same, kwargs_digest = record_at(j)
            if (rule.__module__, rule.__name__) != (module, name):
                raise ValueError("rule order changed")
            pinned, kwargs = pickle.loads(inputs)
            expr = pinned if result is None else result
            if "schema" in kwargs:
                if schema is None:
                    schema = kwargs["schema"]
                else:
                    kwargs["schema"] = schema
            if affected:
                if kwargs.get(key) is not old:
                    raise ValueError("unexpected rule argument")
                kwargs[key] = new
            result = rule(expr, **kwargs)
            if affected:
                kwargs[key] = old
            state_matches = hashlib.sha256(pickle.dumps(kwargs)).digest() == kwargs_digest
            if "schema" in kwargs:
                schema_changed = not state_matches
            elif not state_matches:
                raise ValueError("rule side effects changed")
            output = fingerprint(result)
            if (not schema_changed and output == expected and (result is expr) == same and
                    (output if same else fingerprint(expr)) == after):
                result = None
                schema = None
        if result is not None:
            check_optimized(q, result)


if __name__ == "__main__":
    import sys
    sys.path[:0] = [str(Path.cwd()), str(Path(__file__).parent)]
    replay(json.loads(sys.argv[2]))
