"""Repeat timeout-affected patches serially after host load and swapping settle."""
import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
import time

import oracle


def swap_pages():
    counters = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
    return int(counters["pswpin"]) + int(counters["pswpout"])


def wait_for_quiet():
    host_cpus = os.cpu_count() or 1
    while True:
        before = swap_pages()
        time.sleep(30)
        pressure = Path("/proc/pressure/memory").read_text().splitlines()[1].split()[1]
        swap_rate = (swap_pages() - before) * os.sysconf("SC_PAGE_SIZE") / 30
        if os.getloadavg()[0] <= host_cpus and float(pressure.split("=")[1]) < 0.5 and swap_rate < 1024**2:
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, default=oracle.EVAL / "cases")
    args = parser.parse_args()
    tests, skipped = oracle.load_catalog()
    quarantine = oracle.WORKBENCH / "timeout-quarantine"
    pending = {p.stem for p in quarantine.glob("*.json")}
    for p in oracle.CACHE.glob("*.json"):
        x = json.loads(p.read_text())
        if (x.get("status") == "case_timeout" or x.get("timeout_first_run") or x.get("timeouts")
                or any("Timeout (>" in r for r in x.get("reasons", {}).values())):
            pending.add(p.stem)
    # Older plugins retained only the first reason per parent. A timeout in
    # a later subtest can still be visible in the original session log.
    logs = oracle.WORKBENCH / "oracle-logs"
    for log in logs.glob("*.log"):
        if (oracle.CACHE / (log.stem + ".json")).is_file():
            with log.open("rb") as stream:
                if any(b"from pytest-timeout" in line and b"Timeout (>" in line for line in stream):
                    pending.add(log.stem)
    touched = defaultdict(list)
    results = {}
    for p in sorted(args.cases_root.glob("*/commits/*.patch")):
        patch = p.read_bytes()
        key = oracle.patch_key(patch)
        if key not in pending:
            continue
        touched[p.parents[1]].append(p.stem)
        if key in results:
            continue
        wait_for_quiet()
        measured = oracle.label(patch, tests, skipped, use_cache=False)
        if measured["status"] != "ok" or measured["rc"] not in (0, 1, 5):
            raise RuntimeError(f"Quiet rerun incomplete: {measured['status']}")
        results[key] = {"failing": measured["failing"],
                        "timeout_reproduced": measured.get("timeout_reproduced", [])}
    for directory, ids in sorted(touched.items()):
        if directory.name != "sample":
            subprocess.run([sys.executable, str(oracle.EVAL / "oracle.py"), "label-dir",
                            str(directory), "-j", "1", "--only", ",".join(ids)], check=True)
    (oracle.WORKBENCH / "quiet-timeout-rechecks.json").write_text(json.dumps(results))
    print(f"Completed {len(results)} quiet-timeout rechecks", flush=True)


if __name__ == "__main__":
    main()
