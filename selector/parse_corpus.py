"""Replay the unique public parse calls made during the pinned TPC-DS workload."""
import functools
import gzip
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys

DATA = Path("/app/parse-corpus.pkl.gz")


def fingerprint(expressions):
    values = [e.dump() if e is not None else None for e in expressions]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).digest()


def record():
    root = str(Path.cwd()) + "/"
    sys.path.insert(0, root)
    outside = {}
    corpus = {}
    tokens = {}
    depth = 0

    def entered(code, offset):
        path = code.co_filename.removeprefix(root)
        if path == "sqlglot/parser.py" or path.startswith("sqlglot/parsers/"):
            if depth:
                return
            if code.co_name != "<module>" and not code.co_flags & 1:
                return  # class suite, not method execution
            if code.co_name != "<module>":
                outside[code] = path

        return sys.monitoring.DISABLE

    sys.monitoring.use_tool_id(4, "parse boundary")
    sys.monitoring.register_callback(4, sys.monitoring.events.PY_START, entered)
    sys.monitoring.set_events(4, sys.monitoring.events.PY_START)
    from sqlglot.dialects.dialect import Dialect

    def wrap(name):
        original = getattr(Dialect, name)
        @functools.wraps(original)
        def observe(*args, **kwargs):
            nonlocal depth
            key = pickle.dumps((name, args, kwargs))
            depth += 1
            try:
                expressions = original(*args, **kwargs)
                value = fingerprint(expressions)
            except Exception as exc:
                value = (type(exc).__module__, type(exc).__qualname__, str(exc))
                if corpus.setdefault(key, value) != value:
                    raise ValueError("inconsistent parse exception") from exc
                raise
            finally:
                depth -= 1
            if corpus.setdefault(key, value) != value:
                raise ValueError("parse depends on previous calls")
            return expressions
        setattr(Dialect, name, observe)

    from sqlglot.tokens import Tokenizer
    tokenize = Tokenizer.tokenize

    def observe_tokens(tokenizer, sql):
        factory = "tokenizer" if type(tokenizer) is tokenizer.dialect.tokenizer_class else "jsonpath_tokenizer"
        assert type(tokenizer) is type(getattr(tokenizer.dialect, factory)())
        key = pickle.dumps((tokenizer.dialect, factory, sql))
        result = tokenize(tokenizer, sql)
        value = token_digest(result)
        if tokens.setdefault(key, value) != value:
            raise ValueError("tokenization depends on previous calls")
        return result

    Tokenizer.tokenize = observe_tokens
    wrap("parse")
    wrap("parse_into")
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    from tests import test_executor as tests
    test = tests.TestExecutor("test_execute_tpcds")
    test.setUpClass()
    tests.initializer(tests.TPCDS_SCHEMA, test.tpcds_tables)
    for meta, sql, _ in test.tpcds_sqls:
        expr = pickle.loads(pickle.dumps(tests.parse_one(sql)))
        table = tests.mp_execute(expr, meta)
        if table is not None:
            tests.transpile(sql, write="duckdb")
    test.tearDownClass()
    sys.monitoring.set_events(4, 0)
    lines = {}
    for code, path in outside.items():
        lines.setdefault(path, set()).update(n for _, _, n in code.co_lines() if n is not None)
    DATA.with_suffix(".json").write_text(json.dumps({p: sorted(v) for p, v in lines.items()}))
    if len(corpus) < 99:
        raise ValueError("incomplete parse corpus")
    with gzip.open("/app/token-corpus.pkl.gz", "wb") as stream:
        pickle.dump(tokens, stream)
    with gzip.open(DATA, "wb") as stream:
        pickle.dump(corpus, stream)


def token_digest(tokens):
    values = [(t.token_type.name, t.text, t.line, t.col, t.start, t.end, t.comments) for t in tokens]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).digest()


def replay_tokens():
    from sqlglot.dialects.dialect import Dialect, Dialects
    with gzip.open("/app/token-corpus.pkl.gz", "rb") as stream:
        corpus = pickle.load(stream)
    for key, expected in corpus.items():
        dialect, factory, sql = pickle.loads(key)
        if token_digest(getattr(dialect, factory)().tokenize(sql)) != expected:
            raise ValueError("token stream changed")
    for dialect in Dialects:
        Dialect.get_or_raise(dialect)


def replay():
    with gzip.open(DATA, "rb") as stream:
        corpus = pickle.load(stream)
    for key, expected in corpus.items():
        name, (dialect, *args), kwargs = pickle.loads(key)
        try:
            actual = fingerprint(getattr(dialect, name)(*args, **kwargs))
        except Exception as exc:
            actual = (type(exc).__module__, type(exc).__qualname__, str(exc))
        if actual != expected:
            raise ValueError("parse outcome changed")


if __name__ == "__main__":
    record()
