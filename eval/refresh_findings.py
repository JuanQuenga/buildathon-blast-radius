"""Refresh the measured portion of FINDINGS.md using development labels only."""
from collections import Counter
import json
from pathlib import Path

from paths import CASES, CATALOG, EVAL, WORKBENCH

MARKER = "## Development measurements"


def main():
    catalog = json.loads(CATALOG.read_text())["tests"]
    expensive = [t for t in catalog if t["seconds"] > 5]
    lines = [MARKER, "", "Counts cover complete pytest sessions or the official sample. Each row uses only the development partition. The percentage is the share of labelled patches with zero numbered failures.", "",
             "| Set | Labelled / planned | Clean | Clean share |", "|---|---:|---:|---:|"]
    hits = {t["number"]: Counter() for t in expensive}
    operations = {t["number"]: Counter() for t in expensive}
    text_edits = Counter()
    for d in sorted(CASES.iterdir()):
        meta = json.loads((d / "meta.json").read_text())
        dev = {cid: m for cid, m in meta.items() if m["split"] == "dev"}
        if not dev:
            continue
        labels = d / "answers.json"
        labels = json.loads(labels.read_text())["commits"] if labels.exists() else {}
        audit_path = WORKBENCH / "oracle-details" / f"{d.name}.json"
        audit = json.loads(audit_path.read_text()) if audit_path.exists() else {}
        count = clean = 0
        for cid, m in dev.items():
            a, r = labels.get(cid), audit.get(cid, {})
            if a is None or a.get("basis") or (d.name != "sample" and (r.get("status") != "ok" or r.get("rc") not in (0, 1, 5))):
                continue
            count += 1
            clean += not a["failing"]
            if a["failing"] and m["kind"] == "comment":
                text_edits[m.get("op", "unknown")] += 1
            for test in expensive:
                if test["number"] in a["failing"]:
                    reason = r.get("reasons", {}).get(test["id"], "official")
                    phase = "timeout" if "Timeout (>" in reason else reason.split(":", 1)[0]
                    hits[test["number"]][phase] += 1
                    operations[test["number"]][m.get("op", m["kind"])] += 1
        share = f"{clean/count:.1%}" if count else "—"
        lines.append(f"| {d.name} | {count}/{len(dev)} | {clean} | {share} |")
    lines += ["", "The expensive tests failed in the following labelled development patches. Phases come from pytest reports. Missing means the catalog ID never reported.", ""]
    for t in expensive:
        counts = hits[t["number"]]
        phases = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "no observed failures"
        edits = ", ".join(f"{k} ({v})" for k, v in operations[t["number"]].most_common())
        lines.append(f"- `{t['id']}` ({t['seconds']:.3f} s) — {phases}.")
        if edits:
            lines.append(f"  Observed patch operations include {edits}.")
    if text_edits:
        lines.extend(["", "Development comment edits with a numbered failure, by generator operation — " + ", ".join(f"{k} {v}" for k, v in sorted(text_edits.items())) + "."])
    stable = []
    for p in sorted(WORKBENCH.glob("stability-*.json")):
        rows = json.loads(p.read_text())
        for name in sorted({r["set"] for r in rows}):
            subset = [r for r in rows if r["set"] == name]
            stable.append(f"{name} {sum(r['match'] for r in subset)}/{len(subset)}")
    if stable:
        lines.extend(["", "Independent uncached label repeats with identical failure sets — " + ", ".join(stable) + "."])
    file = EVAL / "FINDINGS.md"
    file.write_text(file.read_text().partition("\n" + MARKER)[0].rstrip() + "\n\n" + "\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
