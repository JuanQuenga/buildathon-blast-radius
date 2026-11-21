"""Measure a frozen selector tree. Run again as the independent label queue advances."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random
import statistics
import subprocess
import tempfile

import run
import score_lib as score
from paths import CASES, CATALOG, EVAL, PYTHON, REPO, WORKBENCH

SETS = ("sample", "dev", "exotic", "revert", "holdout", "transfer")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=REPO)


def missed_catalog_ids(node_ids, numbers):
    if len(node_ids) <= 12:
        return ", ".join(f"`{name}`" for name in node_ids)
    runs = []
    for number in sorted(numbers[name] for name in node_ids):
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    ranges = ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in runs)
    return f"{len(node_ids)} numbered tests in the supplied catalog ({ranges})"


def measure_batch(name, batch, tmp, cache, catalog, tests):
    key = hashlib.sha256(catalog)
    for c in batch:
        key.update(c['id'].encode() + b'\0' + c['patch'])
    cached = cache / f"{name}-{key.hexdigest()[:16]}.json"
    if not cached.exists():
        data = run.make_batch_dir(tmp / cached.stem, batch, catalog)
        r = run.run_selector([str(PYTHON), "-m", "selector.main"], tmp, data,
                             180, "10,11", {})
        v = score.evaluate(r["stdout"], tests, [c["id"] for c in batch])
        if not v["ok"] or r["exit"] or r["timeout"] or r["stdout_over"]:
            raise RuntimeError(f"Rejected {name} batch {cached.stem}: {v.get('code')}, exit {r['exit']}")
        logs = r["stderr"].decode(errors="replace").splitlines()
        cached.write_text(json.dumps(dict(plans=v["plans"], cpu=r["cpu_us"], log=logs)))
    return json.loads(cached.read_text())


def measure(ref, labels_ref=None):
    sha = git("rev-parse", "--verify", ref + "^{commit}").decode().strip()
    cache = WORKBENCH / "candidate-runs" / sha
    cache.mkdir(parents=True, exist_ok=True)
    catalog = CATALOG.read_bytes()
    tests = json.loads(catalog)["tests"]
    durations = {t["number"]: score.duration_ms(t["seconds"]) for t in tests}
    names = {t["number"]: t["id"] for t in tests}
    numbers = {name: n for n, name in names.items()}
    full = score.best_schedule(list(durations), durations)[0]
    label_commit = git("rev-parse", "--verify", labels_ref + "^{commit}").decode().strip() if labels_ref else None
    records, missing, duplicates = [], defaultdict(int), defaultdict(int)
    seen = set()
    sets = []
    for name in SETS:
        directory = CASES / name
        meta = json.loads((directory / "meta.json").read_text())
        answers = directory / "answers.json"
        if label_commit:
            blob = subprocess.run(["git", "show", f"{label_commit}:eval/cases/{name}/answers.json"],
                                  cwd=REPO, capture_output=True)
            labels = json.loads(blob.stdout)["commits"] if blob.returncode == 0 else {}
        else:
            labels = json.loads(answers.read_text())["commits"] if answers.exists() else {}
        details = WORKBENCH / "oracle-details" / (name + ".json")
        audit = json.loads(details.read_text()) if not label_commit and details.exists() else {}
        for path in sorted((directory / "commits").glob("*.patch")):
            cid = path.stem
            case = dict(meta[cid], id=cid, patch=path.read_bytes(), set=name)
            result = audit.get(cid, {})
            valid = (result.get("status") == "ok" and result.get("rc") in (0, 1, 5))
            if label_commit:
                valid = cid in labels and not labels[cid].get("basis")
            if name == "sample" or valid:
                case["answer"] = labels.get(cid)
                case["absent"] = ({names[t] for t in (labels.get(cid) or {}).get("missing", [])}
                                  if label_commit else set(result.get("missing") or []))
            sets.append(case)
            if case["split"] == "dev":
                seen.add(case["patch"])
    with tempfile.TemporaryDirectory(dir=cache) as tmp:
        tmp = Path(tmp)
        archive = git("archive", sha, "selector")
        subprocess.run(["tar", "-xf", "-", "-C", tmp], input=archive, check=True)
        for name in SETS:
            pool = [c for c in sets if c["set"] == name]
            random.Random(1729).shuffle(pool)
            for start in range(0, len(pool), 40):
                batch = pool[start:start + 40]
                m = measure_batch(name, batch, tmp, cache, catalog, tests)
                for c in batch:
                    if c["split"] == "holdout" and c["patch"] in seen:
                        duplicates[name] += 1
                        continue
                    if not c.get("answer"):
                        missing[name] += 1
                        continue
                    ws = m["plans"][c["id"]]
                    sel = {t for w in ws for t in w}
                    failures = c["answer"]["failing"]
                    missed = set(failures) - sel
                    log = " ".join(l.strip() for l in m["log"] if l.strip().startswith(c['id'] + ': '))
                    missed_ids = sorted(names[n] for n in missed)
                    records.append(dict(c, missed=missed_ids,
                                        missed_reported=bool(set(missed_ids) - c["absent"]),
                                        clean=not failures, selected=len(sel), reason=log,
                                        sim=score.commit_cost_ms(ws, durations)/1000 + 300*bool(missed),
                                        cpu=m["cpu"]/1e6/len(batch), full=len(sel)==len(tests),
                                        lower=score.lower_bound(failures,durations)/1000))
        isolated_cpu = defaultdict(float)
        if label_commit:
            # Charge process startup separately to each replay group. These CPU
            # measurements are independent of the mixed batches used for score.
            groups = defaultdict(list)
            for case in records:
                groups[case["set"]].append(case)
                if case["split"] == "dev":
                    groups[f"{case['set']} / {case['kind']} (dev)"].append(case)
            for label, group in groups.items():
                for offset in range(0, len(group), 40):
                    part = group[offset:offset + 40]
                    replay = measure_batch(group[0]["set"], part, tmp, cache, catalog, tests)
                    isolated_cpu[label] += replay["cpu"] / 1e6
    output = [f"# Selector {sha[:12]}", "",
              "Host run of the frozen selector tree in shuffled batches of up to 40. CPU is measured per process batch. Interim tables allocate batch CPU evenly to labelled patches. Full counts plans selecting the entire catalog. Simulated time includes miss penalties. The bound relaxes worker packing. The full-suite reference uses four workers.",
              "", "Incomplete or unaudited patches excluded by set: " + str(dict(missing)) + ". Byte-identical development patches excluded from held-out results: " + str(dict(duplicates)) + ".",
              "", "| Set / kind | N | Clean | Miss | Sim s | CPU s | Full | Bound s | Full suite s |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    if label_commit:
        output.insert(2, f"Label revision: `{label_commit}`.\n")
    if label_commit:
        output[3:3] = ["CPU in this table comes from replaying exactly the patches in each row. Each group pays its own startup cost, so subgroup CPU totals can differ from their parent. Simulated scores and bootstrap CPU use the original shuffled batches.", ""]
    def row(label, rows):
        cpu = isolated_cpu[label] if label_commit else sum(r["cpu"] for r in rows)
        output.append(f"| {label} | {len(rows)} | {sum(r['clean'] for r in rows)} | {sum(bool(r['missed']) for r in rows)} | {sum(r['sim'] for r in rows):.3f} | {cpu:.3f} | {sum(r['full'] for r in rows)} | {sum(r['lower'] for r in rows):.3f} | {len(rows)*full/1000:.3f} |")
    for name in SETS:
        rows = [r for r in records if r['set']==name]
        row(name, rows)
        groups = defaultdict(list)
        for r in rows:
            if r['split'] == 'dev':
                groups[r['kind']].append(r)
        for kind, rs in sorted(groups.items()):
            row(f"{name} / {kind} (dev)", rs)
    ambiguous = [r for r in records if r["absent"]]
    rescued = sum(bool(r["missed"]) and not r["missed_reported"] for r in ambiguous)
    output += ["", f"{len(ambiguous)} labelled patches have catalog IDs with no pytest report. "
               f"Treating those IDs as out of scope removes {rescued} missed-patch penalties "
               f"({300*rescued} simulated seconds) across the measured sets. "
               "The group schedules and CPU stay the same under either convention."]
    output += ["", "## Batch scenarios", "",
               "Each reference uses the official 16/8/3/3 weights. Held-out mutation scenarios require all four original kinds to have labels. For unseen draws, choose uniformly among represented non-sample kinds on held-out cases, then uniformly within kind. CPU uses batch allocations. Quantiles describe these resamplings only. Repeated draws can include the same patch.",
               "", "| N | Unseen share | P10 s | Median s | P90 s | Mean misses |", "|---:|---:|---:|---:|---:|---:|"]
    official = [r for r in records if r['set']=='sample']
    unseen = defaultdict(list)
    for r in records:
        if r['split']=='holdout' and r['kind'] not in run.PRACTICE_KINDS:
            unseen[r['kind']].append(r)
    references = {"Official sample": official, "Held-out mutations": [
        r for r in records if r["split"] == "holdout" and r["kind"] in run.PRACTICE_KINDS
    ]}
    output[-2:] = ["| Reference | N | Unseen share | P10 s | Median s | P90 s | Mean misses |",
                   "|---|---:|---:|---:|---:|---:|---:|"]
    for label, reference in references.items():
        groups = [[r for r in reference if r["kind"] == k] for k in run.PRACTICE_KINDS]
        if not all(groups):
            continue
        for n in (40, 60):
            for share in (0, .2):
                if share and not unseen:
                    continue
                rng = random.Random(32768)
                totals, misses = [], []
                extras = list(unseen.values())
                for _ in range(4000):
                    kinds = rng.choices(groups, weights=list(run.PRACTICE_KINDS.values()), k=n)
                    draws = [rng.choice(rng.choice(extras) if rng.random() < share else group)
                             for group in kinds]
                    totals.append(sum(r["sim"] + r["cpu"] for r in draws))
                    misses.append(sum(bool(r["missed"]) for r in draws))
                totals.sort()
                output.append(f"| {label} | {n} | {share:.0%} | {totals[400]:.1f} | {totals[2000]:.1f} | {totals[3600]:.1f} | {statistics.mean(misses):.2f} |")
    output += ["", "## Development misses", ""]
    for r in records:
        if r['missed'] and r['split']=='dev':
            output.append(f"- {r['set']}/{r['id']} ({r['kind']}). {r.get('desc','')} Selector trace: {r['reason']}. Omitted catalog IDs: " + missed_catalog_ids(r["missed"], numbers))
    if not any(r['missed'] and r['split']=='dev' for r in records):
        output.append("No missed development patches among the labelled cases.")
    dest = EVAL / "REPORTS" / (sha + ".md")
    dest.parent.mkdir(exist_ok=True)
    dest.write_text("\n".join(output)+"\n")
    print(dest)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ref")
    ap.add_argument("--labels-ref", help="use a frozen evaluation commit containing audited labels")
    args = ap.parse_args()
    measure(args.ref, args.labels_ref)
