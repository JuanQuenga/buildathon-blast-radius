"""Pin-only offline line recording. The parent and real spawn workers install this hook."""

import os
import sys

root = os.environ.get("BLAST_RECORD_ROOT")
if root:
    root = root.rstrip("/") + "/"
    tool = 4
    sys.monitoring.use_tool_id(tool, "pinned heavy lines")
    pid = os.getpid()
    record_dir = os.environ.get("BLAST_RECORD_DIR", "/tmp")
    want_lines = os.environ.get("BLAST_RECORD_MODE") != "entries"
    output = os.open(
        f"{record_dir}/heavy-lines-{pid}", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600
    )
    paths = {}
    seen = set()

    def enter(code, offset):
        filename = code.co_filename
        if filename.startswith(root):
            rel = filename[len(root) :]
            if rel.startswith(("sqlglot/", "tests/")):
                record("entered", filename, code.co_qualname, code.co_firstlineno)
                if not want_lines:
                    return sys.monitoring.DISABLE
                paths[code] = rel
                sys.monitoring.set_local_events(tool, code, sys.monitoring.events.LINE)
        return sys.monitoring.DISABLE

    def line(code, number):
        key = (paths[code], number)
        if key not in seen:
            seen.add(key)
            os.write(output, f"{key[0]}\t{number}\n".encode())
        return sys.monitoring.DISABLE

    # Monitor access to source bytes and Python function code objects. These
    # observations can make even a function that is never called relevant.
    audit_seen = set()
    inside_audit = False

    def record(kind, filename, qualname="", lineno=0):
        if isinstance(filename, str) and filename.startswith(root):
            rel = filename[len(root) :]
            item = (kind, rel, qualname, lineno)
            if rel.startswith(("sqlglot/", "tests/")) and item not in audit_seen:
                audit_seen.add(item)
                os.write(output, f"@{kind}\t{rel}\t{lineno}\t{qualname}\n".encode())

    def audit(event, args):
        global inside_audit
        if inside_audit:
            return
        if event == "object.__getattr__" and args[1] == "__code__":
            inside_audit = True
            try:
                c = args[0].__code__
                record("inspected", c.co_filename, c.co_qualname, c.co_firstlineno)
            finally:
                inside_audit = False
        elif event == "open":
            name = args[0]
            if isinstance(name, str) and name.endswith(".py"):
                record("source", name)

    sys.addaudithook(audit)

    sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, enter)
    sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, line)
    sys.monitoring.set_events(tool, sys.monitoring.events.PY_START)
