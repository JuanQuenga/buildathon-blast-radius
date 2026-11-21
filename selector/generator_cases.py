"""Replay the complete pinned allocation test after checking cold and reset lookups."""
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
    TestGenerator("test_generator_memory").debug()


if __name__ == "__main__":
    main()
