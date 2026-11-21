"""Project a well-formed fixture edit onto fields observed on the pristine pin."""
import base64
import functools
import gzip
import hashlib
import json
import os
from pathlib import Path
import zlib


@functools.cache
def profile():
    return json.loads((Path(__file__).parent / "maps/fixture-fields.json").read_text())


def load_fields(text, paired):
    lines = [line for line in text.splitlines() if line and not line.startswith("--")]
    if paired:
        statements = "\n".join(lines).split(";")
        rows = []
        for i in range(0, len(statements) - 1, 2):
            source = statements[i].strip().split("\n")
            meta = []
            while source and source[0].startswith("#"):
                meta.append(source.pop(0))
            if not source:
                raise ValueError("missing SQL")
            rows.append((meta, "\n".join(source), statements[i+1].strip()))
        return rows
    rows, meta = [], []
    for line in lines:
        if line.startswith("#"):
            meta.append(line)
        else:
            rows.append((meta, line, ""))
            meta = []
    return rows


def binary_result(lines, old):
    # git binary patches use the Git/base85 alphabet, zlib, and either raw
    # target bytes or a pack-format delta against the pristine blob.
    kind, size = lines[0].split()
    size = int(size)
    limit = 4_000_000
    if kind not in {"literal", "delta"} or not 0 <= size <= limit:
        raise ValueError("unsupported binary payload")
    data = bytearray()
    for line in lines[1:]:
        if not line:
            break
        n = ord(line[0]) - (64 if "A" <= line[0] <= "Z" else 70)
        if not 1 <= n <= 52 or len(line) != 1 + 5 * ((n + 3) // 4):
            raise ValueError("invalid binary line")
        data.extend(base64.b85decode(line[1:])[:n])
        if len(data) > limit:
            raise ValueError("oversize binary patch")
    stream = zlib.decompressobj()
    raw = stream.decompress(data, size + 1)
    if len(raw) != size or not stream.eof or stream.unused_data:
        raise ValueError("incomplete binary patch")
    if kind == "literal":
        return raw
    ops = iter(raw)
    def count():
        value = 0
        for shift in range(0, 35, 7):
            x = next(ops)
            value |= (x & 127) << shift
            if x < 128:
                return value
        raise ValueError("invalid delta size")
    if count() != len(old):
        raise ValueError("wrong delta base")
    target = count()
    if target > limit:
        raise ValueError("oversize delta target")
    out = bytearray()
    for op in ops:
        if op & 128:
            off = sum(next(ops) << (8*i) for i in range(4) if op & (1 << i))
            length = sum(next(ops) << (8*i) for i in range(3) if op & (16 << i)) or 65536
            if off + length > len(old):
                raise ValueError("copy outside base")
            out.extend(old[off:off+length])
        elif op:
            for _ in range(op):
                out.append(next(ops))
        else:
            raise ValueError("invalid delta command")
        if len(out) > target:
            raise ValueError("oversize delta output")
    if len(out) != target:
        raise ValueError("truncated delta")
    return bytes(out)


def checked_table(raw, changed):
    import io
    def rows(data):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            txt = stream.read(4_000_001)
        if len(txt) > 4_000_000 or b'"' in txt or b'\r' in txt or b'\0' in txt:
            raise ValueError("unknown CSV layout")
        return [line.split("|") for line in txt.decode().splitlines()]
    old, new = rows(raw), rows(changed)
    # The Python fixture loader infers converters from the first data row.
    # Each later field must already parse in its column on the pin.
    if len(new) < 2 or old[:2] != new[:2]:
        return False
    width = len(old[0])
    if any(len(r) != width for r in old + new):
        return False
    columns = [set(col) for col in zip(*old[1:])]
    return all(all(x in col for x, col in zip(r, columns)) for r in new[2:])


def project(fp, readers, tests, apply_hunks):
    if fp.status != "M" or fp.old_path != fp.new_path or not fp.path.startswith("tests/fixtures/"):
        return readers
    name = fp.path[len("tests/fixtures/"):]
    try:
        entry = profile().get(name)
        if entry is None:
            return readers
        pin = Path(os.environ.get("BLAST_SQLGLOT", "/app/sqlglot")) / fp.path
        raw = pin.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            return readers
        if "table" in entry:
            if not fp.payload or not checked_table(raw, binary_result(fp.payload, raw)):
                return readers
            keep = set(entry["table"])
        else:
            if fp.binary or not fp.hunks:
                return readers
            old = raw.decode()
            new, _ = apply_hunks(old, fp.hunks)
            # Edits touching separators, meta directives or comment lines are
            # structurally ambiguous even if the total pair count is unchanged.
            for h in fp.hunks:
                for tag, line in h.lines:
                    if tag != " " and (";" in line or line.lstrip().startswith(("#", "--"))):
                        return readers
            before, after = (load_fields(s, entry["paired"]) for s in (old,new))
            if len(before) != len(after) or len(before) != entry["size"]:
                return readers
            keep = set(entry["raw"])
            for i, (a,b) in enumerate(zip(before,after)):
                if a[0] != b[0]:
                    return readers
                mask = (2 if a[1] != b[1] else 0) | (4 if a[2] != b[2] else 0)
                if mask:
                    keep.update(n for n, masks in entry["reads"].items() if masks[i] & mask)
        return readers & sum(1 << i for i,n in enumerate(tests) if n in keep)
    except (ValueError, OSError, EOFError, IndexError, KeyError, RuntimeError, StopIteration, zlib.error):
        return readers
