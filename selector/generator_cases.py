"""Replay the complete pinned allocation test after checking cold and reset lookups."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))


def main():
    from tests.test_generator import Dialect, Dialects, TestGenerator

    classes = {d: type(Dialect.get_or_raise(d)) for d in Dialects}
    Dialect._classes.clear()
    for dialect in Dialects:
        if type(Dialect.get_or_raise(dialect)) is not classes[dialect]:
            raise ValueError("dialect registry changed after reset")
    if len(sys.argv) > 2 and sys.argv[1] == "dialects":
        import tests.test_generator as tests
        selected = json.loads(sys.argv[2])
        cases = [d for d in Dialects if d.value in selected]
        if len(cases) != len(selected):
            raise ValueError("unknown dialect")
        tests.Dialects = cases
    TestGenerator("test_generator_memory").debug()


if __name__ == "__main__":
    main()
