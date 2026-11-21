# eval/ — held-out benchmark for the Blast Radius selector

> **Contest rule.** Everything in `eval/` is *measurement only*. Benchmark
> answers (`eval/cases/*/answers.json`), case metadata and anything derived from
> them must never be read by the selector, baked into maps, or copied into the
> Docker image. The Dockerfile copies only `selector/`. Use these cases to test
> whether source-derived selection generalises, not to build a patch → tests
> table (explicitly forbidden by the track rules).

## Layout

| Path | What |
| --- | --- |
| `oracle.py` | Ground-truth labeler: apply a patch to a fresh reflink copy of the pin, run the whole suite with pytest, emit the official label. Parallel, cached by patch hash. |
| `oracle_plugin/br_oracle_plugin.py` | pytest plugin streaming every report (incl. pytest-9 subtest failures and collection errors) to JSONL. |
| `run.py` | Harness: runs a selector on case batches like the scorer, validates with the exact rules, scores with the exact formula, prints misses and breakdowns, bootstraps 40/60-patch batches. |
| `score_lib.py` | Exact validator + score (cross-checked against the starter `score.py` with `run.py --selftest`). |
| `baselines.py` | `starter`, `full4`, `none`, `oracle` reference selectors. |
| `gen/` | Case generators: `build.py` (mechanical mutators in the organisers' style), `exotic.py` (kinds absent from the sample), `reverts.py` (upstream fix reverts). |
| `cases/<set>/` | `commits/*.patch`, `answers.json` (official format + `kind`), `meta.json` (`kind`, `family`, `op`, `area`, `files`, `desc`, `split`). |
| `FINDINGS.md` | Facts about the pin/test-suite that matter for selection. |

Bulky things live **outside the repo** in `$BR_WORKBENCH`
(default `blast-radius/work-bench/`): the oracle cache, per-case pytest logs
(`oracle-logs/<key>.log`), per-test failure reasons (`oracle-details/<set>.json`),
harness run dirs (`runs/`), the Python 3.13 venv, and private clones of the pin.

## Case sets

| Set | Source | Split |
| --- | --- | --- |
| `sample` | the official 30 (official labels) | dev |
| `dev` | `build.py --seed 1` mechanical mutations | dev |
| `holdout` | `build.py --seed 2` — **frozen, do not tune on it** | holdout |
| `exotic` | `build.py --exotic --seed 101` | 75% dev / 25% holdout (`meta.split`) |
| `revert` | `reverts.py --every 2` (production hunk of upstream `fix` commits reverted on the pin) | 75% dev / 25% holdout |

Kinds: `code`, `fixture`, `comment`, `multi` (the four sample kinds), plus
`test` (test-file edits: expectations, import errors, renamed/skipped tests,
helpers), `revert`, and the exotic kinds `newfile`, `delete`, `rename`, `nonpy`,
`export`, `syntax`, `importcycle`, `conftest`.  See the counts in `FINDINGS.md`.

The holdout is everything with `split == "holdout"`: the whole `holdout` set
plus the hashed quarter of `exotic` and `revert`. Look at holdout scores only
when comparing finished selectors, and never look at which holdout cases miss
while tuning.

## Setup (once)

```sh
WB=blast-radius/work-bench
uv venv -p 3.13 $WB/venv && uv pip install -p $WB/venv/bin/python -r blast-radius/starter/sample-kit/requirements.lock
git clone blast-radius/sqlglot $WB/base && git -C $WB/base checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
```

Env overrides: `BR_WORKBENCH`, `BR_BASE`, `BR_PYTHON`, `BR_STARTER`, `BR_CATALOG`,
`BR_ORACLE_CORES` (taskset range for oracle pytest runs, default `10-15`),
`BR_RUN_CORES` (2 cores for the selector, default `8,9`), `BR_TEST_TIMEOUT` (per-test
pytest-timeout, default 300 s; raised from 180 because the shared machine hit load 58).

## Running

```sh
# score our selector (python -m selector.main from the repo root, BLAST_DATA=<batch dir>)
python3 eval/run.py --cases sample                       # official 30
python3 eval/run.py --cases sample,dev,exotic,revert --split dev
python3 eval/run.py --cases dev,exotic,revert --split dev --sample 40 --mix practice   # practice-like distribution
python3 eval/run.py --cases dev,exotic,revert --split dev --sample 60 --mix final --limit-s 270
python3 eval/run.py --cases holdout,exotic,revert --split holdout   # final check only

# any other selector / checkout
python3 eval/run.py --cases dev --cwd /path/to/other/checkout
python3 eval/run.py --cases sample --selector "$WB/venv/bin/python eval/baselines.py full4" --selftest

# label new cases
python3 eval/gen/build.py --set mynew --seed 7 --code 50 --fixture 10
python3 eval/oracle.py label-dir eval/cases/mynew -j 6
python3 eval/oracle.py label some.patch          # one-off, prints failing tests + reasons
```

`run.py` runs the selector per batch (default 40 patches) with a fresh read-only
data dir, `HOME`/`TMPDIR` in a fresh temp dir, `BLAST_SQLGLOT` pointing at a
read-only copy of the pin, pinned to 2 cores with `taskset`, killed at
`--limit-s`, stdout capped at 2 MiB. CPU is user+sys from `wait4` rusage (whole
reaped process tree). It does **not** reproduce Docker's read-only root FS, the
64 MB `/tmp` limit, the 4 GB memory limit or the 256-process limit; test those
with `make sample` in Docker. Per-patch results feed `--sample N --reps R`
bootstraps (`--mix practice` draws kinds in the sample's 16/8/3/3 proportions,
`--mix final` adds ~20% from the non-sample kinds).

## Oracle semantics

Command (per case, own copy of the pin, cwd = copy, `PYTHONPATH=<copy>`,
`SKIP_INTEGRATION=0`, Python 3.13 + `requirements.lock`):
`python -m pytest tests -p no:cacheprovider -p br_oracle_plugin -q --timeout=180`.

A catalog test is labelled failing when any of its reports failed (setup, call,
teardown, or any subtest — pytest 9 reports the parent `PASSED` next to
`SUBFAILED`), when its module/class failed to collect (every catalog test in
that file), or when it never reported at all (`missing`: crash, deleted or
renamed test). The last rule is our conservative guess; the official handling
of renamed/deleted catalog tests is unpublished — `oracle-details/<set>.json`
lists such tests under `missing` so they can be excluded.

## Comparing frozen selector commits

Run `python3 eval/candidate_report.py candidate-1` to measure the tagged selector tree and write its report under `eval/REPORTS/`. The script snapshots labels at startup and reuses measured plans for the same tree, catalog and batch. Rerun it after a labelling checkpoint. Reports disclose the number of unavailable labels and exclude exact copies of development patches from held-out measurements. The four fresh transfer cases are held out. Archived selector output and timings stay in the workbench. This host runner assumes the image starts Python with the selector module and default settings.

For a test rename or deletion, primary evaluation uses the fixed catalog and counts a missing ID as failing. `run.py --ignore-missing` gives the alternate convention when oracle details are available. For a root conftest import error before pytest has collected any tests, the label file marks the all-catalog interpretation as inferred. Candidate reports omit that case. Whole-job timeouts, crashes and interrupted collection require a successful rerun. The per-test timeout currently defaults to 300 seconds.

Selector comparisons on holdout may disclose only aggregates. The development-only miss traces in the reports include the selector's logged decision and the omitted tests. Per-patch CPU is unavailable, so subgroup CPU is a share of the measured batch total.

Use `--labels-ref COMMIT` to compare candidates against the same committed, audited label snapshot. These reports replay each row's patches as a group to measure its local CPU time, including process startup. The main simulation and bootstraps continue to use the shuffled batches. The committed labels may also record the numbers of missing catalog tests for the alternate-convention calculation.
