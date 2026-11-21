"""Pin-only method footprints for the parse corpus and generator test."""
import collections
import gzip
import json
import os
from pathlib import Path
import pickle
import sys

sys.path.insert(0, str(Path.cwd()))


def record():
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:1])
    rows = {}
    current = collections.defaultdict(set)
    prefix = str(Path.cwd()) + "/"
    def entered(code, offset):
        filename = code.co_filename
        if filename.startswith(prefix + "sqlglot/"):
            current[filename[len(prefix):]].update(n for _, _, n in code.co_lines() if n)
        return sys.monitoring.DISABLE
    def checkpoint(key):
        rows[key] = {p: sorted(lines) for p, lines in current.items()}
        current.clear()
        sys.monitoring.restart_events()
    sys.monitoring.use_tool_id(3, "pin call footprint")
    sys.monitoring.register_callback(3, sys.monitoring.events.PY_START, entered)
    sys.monitoring.set_events(3, sys.monitoring.events.PY_START)
    if sys.argv[-1] == "parser":
        from parse_corpus import DATA, fingerprint
        with gzip.open(DATA, "rb") as stream:
            corpus = pickle.load(stream)
        checkpoint("corpus-setup")
        for i, (key, expected) in enumerate(corpus.items()):
            name, (dialect, *args), kwargs = pickle.loads(key)
            try:
                result = fingerprint(getattr(dialect, name)(*args, **kwargs))
            except Exception as exc:
                result = (type(exc).__module__, type(exc).__qualname__, str(exc))
            if result != expected:
                raise ValueError("pin replay changed")
            checkpoint("parse:" + str(i))
        assert len(corpus) >= 99
    else:
        import tests.test_generator as tests
        dialects = list(tests.Dialects)
        classes = {d: type(tests.Dialect.get_or_raise(d)) for d in dialects}
        tests.Dialect._classes.clear()
        for d in dialects:
            assert classes[d] is type(tests.Dialect.get_or_raise(d))
        checkpoint("generator-setup")
        for d in dialects:
            tests.Dialects = (d,)
            tests.TestGenerator("test_generator_memory").debug()
            checkpoint("gen:" + d.value)
        assert len(dialects) > 1
    sys.monitoring.set_events(3, 0)
    # Keep parser method footprints only for the parse-boundary proof.
    for k, row in rows.items():
        if k.startswith("parse:"):
            rows[k] = {p: v for p, v in row.items() if p == "sqlglot/parser.py" or p.startswith("sqlglot/parsers/")}
    with gzip.open("/app/profile-" + sys.argv[-1] + ".gz", "wt") as stream:
        json.dump(rows, stream, separators=(",", ":"))


if __name__ == "__main__":
    record()
