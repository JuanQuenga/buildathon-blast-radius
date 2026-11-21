"""Record option names potentially read while constructing pinned generators."""
import builtins
import dis
import json
from pathlib import Path
import sys

sys.path.insert(0, "/app/sqlglot")
names = set()


def entered(code, offset):
    if code.co_filename.startswith("/app/sqlglot/"):
        for instruction in dis.get_instructions(code):
            if instruction.opname in {"LOAD_ATTR", "LOAD_SUPER_ATTR", "LOAD_GLOBAL", "LOAD_NAME", "LOAD_CONST"}:
                arg = instruction.argval
                if type(arg) is str and arg.isidentifier() and arg.isupper():
                    names.add(arg)
    return sys.monitoring.DISABLE


original = builtins.getattr
def lookup(obj, key, *fallback):
    if type(key) is str and key.isupper():
        names.add(key)
    return original(obj, key, *fallback)


sys.monitoring.use_tool_id(4, "generator option consumers")
sys.monitoring.register_callback(4, sys.monitoring.events.PY_START, entered)
sys.monitoring.set_events(4, sys.monitoring.events.PY_START)
builtins.getattr = lookup
from tests.test_generator import TestGenerator
TestGenerator("test_generator_memory").debug()
builtins.getattr = original
sys.monitoring.set_events(4, 0)
Path(sys.argv[1]).write_text(json.dumps(sorted(names)))
