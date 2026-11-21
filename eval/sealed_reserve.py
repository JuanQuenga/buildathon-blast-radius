"""Measure frozen selectors on the encrypted reserve and publish aggregate results."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

import score_lib as score
from paths import CATALOG, EVAL, WORKBENCH

PRIVATE = WORKBENCH / "reserve-private"
PUBLISHED = EVAL / "sealed"


def inputs():
    manifest = json.loads((PUBLISHED / "manifest.json").read_text())
    archive = PRIVATE / "inputs.tar"
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["plaintext_tar_sha256"]:
            raise ValueError("Reserve archive differs from the committed freeze")
    with tarfile.open(archive) as frozen:
        meta = json.load(frozen.extractfile("cases/meta.json"))
        cases = []
        for cid in sorted(meta):
            patch = frozen.extractfile(f"cases/commits/{cid}.patch").read()
            if patch != (PRIVATE / "cases/commits" / f"{cid}.patch").read_bytes():
                raise ValueError("Oracle input differs from the encrypted reserve")
            cases.append(dict(id=cid, patch=patch))
    if len(cases) != manifest["patches"]:
        raise ValueError("Unexpected reserve size")
    return cases


def plans(ref, cases, catalog, tests):
    import candidate_report

    sha = candidate_report.git("rev-parse", "--verify", ref + "^{commit}").decode().strip()
    cache = PRIVATE / "runs" / sha
    cache.mkdir(exist_ok=True, parents=True)
    with tempfile.TemporaryDirectory(dir=cache) as tmp:
        tree = Path(tmp)
        candidate_report.prepare_tree(sha, tree, cache)
        measured = candidate_report.measure_batch("reserve", cases, tree, cache, catalog, tests, isolated_host=True)
    return sha, measured


def aggregate(cases, result, answers, durations):
    sim = missed = full = lower = feasible = clean = 0
    for c in cases:
        cid = c["id"]
        workers = result["plans"][cid]
        selected = {t for worker in workers for t in worker}
        failing = answers[cid]["failing"]
        miss = not selected.issuperset(failing)
        sim += score.commit_cost_ms(workers, durations) + score.MISS_MS * miss
        missed += miss
        full += len(selected) == len(durations)
        clean += not failing
        lo, hi = score.oracle_bounds(failing, durations)
        lower += lo
        feasible += hi
    return dict(patches=len(cases), clean=clean, misses=missed, simulation_ms=sim,
                cpu_us=result["cpu"], full_suite_plans=full, oracle_bound_ms=lower,
                oracle_feasible_ms=feasible,
                full_suite_baseline_ms=len(cases) * score.best_schedule(list(durations), durations)[0])


def seal_answers():
    target = PUBLISHED / "answers.json.gpg"
    target.parent.mkdir(exist_ok=True)
    gpg = ["gpg", "--homedir", str(PRIVATE / "gpg"), "--batch", "--pinentry-mode", "loopback",
           "--passphrase-file", str(PRIVATE / "seal.key")]
    comp = PRIVATE / "cases/answers.json"
    if target.exists():
        old = subprocess.check_output([*gpg, "--decrypt", str(target)], stderr=subprocess.DEVNULL)
        if old == comp.read_bytes():
            return
    with tempfile.TemporaryDirectory(dir=PUBLISHED) as td:
        tmp = Path(td) / "answers.gpg"
        subprocess.run([*gpg, "--cipher-algo", "AES256", "--symmetric", "--output", str(tmp), str(comp)], check=True)
        verify = subprocess.check_output([*gpg, "--decrypt", str(tmp)], stderr=subprocess.DEVNULL)
        if verify != comp.read_bytes():
            raise ValueError("Encrypted answer verification failed")
        os.replace(tmp, target)


def report_lines(sha):
    published = PUBLISHED / "scores.json"
    row = json.loads(published.read_text()).get(sha) if published.is_file() else None
    if row is None:
        return []
    return ["", "## Encrypted reserve", "",
            "The evaluator froze the 24 patches after candidate-12. Every selector receives the same single batch. Only aggregates leave the private measurement directory.", "",
            "| Patches | Clean | Misses | Simulated s | Local CPU s | Full suite plans | Oracle bound s | Feasible oracle s | Full suite s |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            f"| {row['patches']} | {row['clean']} | {row['misses']} | {row['simulation_ms']/1000:.3f} | {row['cpu_us']/1e6:.3f} | {row['full_suite_plans']} | {row['oracle_bound_ms']/1000:.3f} | {row['oracle_feasible_ms']/1000:.3f} | {row['full_suite_baseline_ms']/1000:.3f} |"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("refs", nargs="+")
    parser.add_argument("--plans-only", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    cases = inputs()
    catalog = CATALOG.read_bytes()
    tests = json.loads(catalog)["tests"]
    durations = {t["number"]: score.duration_ms(t["seconds"]) for t in tests}
    published = PUBLISHED / "scores.json"
    scores = json.loads(published.read_text()) if published.exists() else {}
    if not args.plans_only:
        answers = json.loads((PRIVATE / "cases/answers.json").read_text())["commits"]
        if set(answers) != {c["id"] for c in cases} or any(x.get("basis") for x in answers.values()):
            raise ValueError("Reserve labels are incomplete or qualified")
        repeat = json.loads((PRIVATE / "stability.json").read_text())
        if len(repeat) != 2 or not all(repeat.values()):
            raise ValueError("Reserve stability check incomplete")
    for ref in args.refs:
        sha, result = plans(ref, cases, catalog, tests)
        if not args.plans_only:
            scores[sha] = aggregate(cases, result, answers, durations)
    if not args.plans_only:
        seal_answers()
        temp = published.with_suffix(".tmp")
        temp.write_text(json.dumps(scores, sort_keys=True, indent=2) + "\n")
        temp.replace(published)


if __name__ == "__main__":
    main()
