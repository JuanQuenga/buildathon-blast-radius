"""Generate field replacements and pair-boundary controls from the pinned workload."""
import argparse
import difflib
from pathlib import Path
import re


def generate(pin, out):
    out.mkdir(parents=True, exist_ok=True)
    for suite in ("tpc-ds", "tpc-h"):
        path = f"tests/fixtures/optimizer/{suite}/{suite}.sql"
        old = (pin / path).read_text()
        parts = old.split(";")
        for offset, name in enumerate(("dssr", "dsex") if suite == "tpc-ds" else ("hhsr", "hhex")):
            change = parts.copy()
            j = 12 + offset
            match = re.search(r'\b(?:FROM|from) +("?[A-Za-z_0-9]+"?)', change[j])
            assert match
            pos = match.end()
            change[j] = change[j][:pos] + "42 " + change[j][pos:]
            write_patch(out, name, path, old, ";".join(change))
        for case in range(16):
            index = (case // 2 * 5 + 1) % ((len(parts) - 1) // 2)
            position = 2 * index + case % 2
            chunk = parts[position]
            match = re.search(r"(?im)^[ \t]*(?:SELECT|FROM)\b", chunk)
            assert match
            token = (" 42 FROM" if case % 4 == 0 else " 42")
            if case >= 14:
                token = ";" + token
            change = parts.copy()
            change[position] = chunk[:match.end()] + token + chunk[match.end():]
            new = ";".join(change)
            write_patch(out, f"{suite}-{case:02}", path, old, new)


def write_patch(out, name, path, old, new):
    diff = difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                fromfile="a/" + path, tofile="b/" + path)
    (out / f"{name}.patch").write_text(f"diff --git a/{path} b/{path}\n" + "".join(diff))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pin", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    generate(args.pin, args.out)
