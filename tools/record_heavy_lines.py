"""Record cold imports and real spawned workers on the pinned image, offline.

Run from the host with IMAGE NODEID SEED OUTPUT. Each run writes one gzip file.
"""

import gzip
import json
from pathlib import Path
import subprocess
import sys
from measure_heavy import FLAGS

RUN = """
from collections import defaultdict
import os
from pathlib import Path
import subprocess
import sys,json
env=dict(os.environ, PYTHONPATH="/trace:/app/sqlglot", BLAST_RECORD_ROOT="/app/sqlglot")
cmd=[sys.executable,"-m","pytest","-p","no:cacheprovider","-q",sys.argv[1]]
run=subprocess.run(cmd, env=env,stdout=sys.stderr)
run.check_returncode()
files=sorted(Path("/tmp").glob("heavy-lines-*"))
hits=defaultdict(set)
observed=defaultdict(set)
for f in files:
    for line in f.read_text().splitlines():
        if line.startswith("@"):
            kind,path,n,name=line.split("\t",3)
            observed[kind[1:]].add((path,int(n),name))
            continue
        path,n=line.split("\t")
        hits[path].add(int(n))
if not hits or not any(p.startswith("tests/") for p in hits):
    raise ValueError("missing test coverage")
print(json.dumps({"processes":len(files),"observed":{k:sorted(v) for k,v in observed.items()},"hits":{p:sorted(ns) for p,ns in sorted(hits.items())}}))
"""


def main():
    image, node, seed, output = sys.argv[1:]
    tracer = Path(__file__).resolve().parent / "heavy_line_trace"
    run = subprocess.run(
        [
            "docker",
            "run",
            *FLAGS,
            "-w",
            "/app/sqlglot",
            "-e",
            f"PYTHONHASHSEED={seed}",
            "-v",
            f"{tracer}:/trace:ro",
            image,
            "python",
            "-c",
            RUN,
            node,
        ],
        capture_output=True,
        timeout=360,
    )
    sys.stderr.buffer.write(run.stderr)
    run.check_returncode()
    record = json.loads(run.stdout)
    print(
        node,
        seed,
        record["processes"],
        sum(map(len, record["hits"].values())),
        flush=True,
    )
    Path(output).write_bytes(
        gzip.compress(json.dumps(record, separators=(",", ":")).encode(), mtime=0)
    )


if __name__ == "__main__":
    main()
