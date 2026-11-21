from pathlib import Path
base=Path(__file__).resolve().parent/'src/selector'
p=base/'fixture_fields.py';s=p.read_text()
s=s.replace('def tpc_consumers(new, entry):','def tpc_consumers(new, entry, name, pin_root):')
s=s.replace('    return set(entry["raw"]) | set(entry["reads"])','''    # The comparison cache is shared by both suites and keyed only by SQL.
    # A new cross-fixture key could return a DataFrame from the other connection.
    other, = TPC_FIXTURES - {name}
    other_rows = load_fields((pin_root / other).read_text(), paired=True)
    if {sql for _, sql, _ in rows}.intersection(sql for _, sql, _ in other_rows):
        raise ValueError("shared reference SQL cache key")
    return set(entry["raw"]) | set(entry["reads"])''')
s=s.replace('def project(fp, readers, tests, apply_hunks):','def project(fp, readers, tests, apply_hunks, *, single_file=False):')
s=s.replace('if name not in TPC_FIXTURES:', 'if name not in TPC_FIXTURES or not single_file:')
s=s.replace('keep = tpc_consumers(new, entry)', 'keep = tpc_consumers(new, entry, name, pin.parents[2])')
# /app/sqlglot/tests/fixtures/optimizer/tpc-h/tpc-h.sql -> parents[2] = fixtures
p.write_text(s)
p=base/'impact.py';s=p.read_text().replace('project(fp, b, self.idx["tests"], apply_hunks)', 'project(fp, b, self.idx["tests"], apply_hunks, single_file=self.single_file)')
s=s.replace('self.python_body(fp, oldp, old_src, new_src, placed, identity_edit)','self.python_body(oldp, old_src, new_src, placed, identity_edit)').replace('def python_body(self, fp, oldp','def python_body(self, oldp')
p.write_text(s)
p=base/'fixture_query.py';s=p.read_text()
s=s.replace('import json','import functools')
a=s.index('def main():')
s=s[:a]+'''FIXTURE = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"


@functools.cache
def pinned_rows():
    with Path("/app/pin-cache/query-fixtures.pkl").open("rb") as stream:
        return pickle.load(stream)


def changed_inputs(rows):
    baseline = pinned_rows()
    if len(rows) != len(baseline) or len(rows) != 99:
        raise ValueError("fixture structure changed")
    changed = []
    for (meta, sql, _), (prior_meta, source, _) in zip(rows, baseline):
        if meta != prior_meta or (sql != source and meta.get("execute")):
            raise ValueError("full result comparison required")
        if sql != source:
            changed.append(sql)
    return changed


def admissible(source):
    from .fixture_fields import load_fields
    rows = []
    for directives, sql, expected in load_fields(source, paired=True):
        meta = {}
        for directive in directives:
            key, value = directive.split(":", 1)
            meta[key.lstrip("#").strip()] = value.strip()
        rows.append((meta, sql, expected))
    try:
        return bool(changed_inputs(rows))
    except ValueError:
        return False


def main():
    sys.path.insert(0, os.getcwd())
    from sqlglot import parse_one
    from tests import helpers
    if Path(helpers.FIXTURES_DIR) != Path.cwd() / "tests/fixtures":
        raise ValueError("fixture loader points outside the patched overlay")
    rows = list(helpers.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    for sql in changed_inputs(rows):
        # The original parent parses every string and transfers the expression.
        # mp_execute returns None before reading it when execute is absent.
        pickle.loads(pickle.dumps(parse_one(sql)))


if __name__ == "__main__":
    main()
'''
p.write_text(s)
p=base/'heavy.py';s=p.read_text().replace('        return "fixture-query", "", verify.apply_patch(text), []','''        from .fixture_query import admissible
        changes = verify.apply_patch(text)
        if admissible(changes[files[0].path]):
            return "fixture-query", "", changes, []
        return None''')
s=s.replace('if group != "fixture-query" and (group != "catalog"', 'if (group != "fixture-query" or "tests" not in sys.modules) and (group != "catalog"')
s=s.replace('if probe.mode == "bundle" and session is None:', 'if probe.mode in {"bundle", "fixture-query"} and session is None:')
p.write_text(s)
