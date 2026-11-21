# Runtime verification: findings

Question: the scorer charges container CPU 1:1 but only charges *simulated*
seconds for scheduled tests. Can the selector run candidate tests itself
against the patched source, drop the ones it saw pass, and emit `[]` for patches
proven clean?

**Short answer: not with our impact model. It is built (`selector/verify.py`)
and off by default (`BLAST_VERIFY=0`).** It roughly breaks even on CPU at best,
and isolated test runs reproduce the labeler's pytest session imperfectly, so
dropping a test on an isolated pass can cause a 300 s miss.

All numbers: this machine, Python 3.13.18, `requirements.lock` installed with
uv, `SKIP_INTEGRATION=0`, pinned sqlglot `eb11aec4`.

## 1. What a test costs to run here vs. the catalog

Full suite with pytest (junit timings), compared to the catalog's `seconds`:

| | catalog s | local s |
|---|---|---|
| whole suite (1620 tests) | 103.0 | 114.7 wall (user CPU 181 s: duckdb threads) |
| `test_max_nodes` | 28.8 | 22.3 |
| `test_execute_tpcds` | 21.5 | 38.7 |
| `test_generator_memory` | 6.7 | 7.6 |
| tests > 50 ms (median ratio) | | 0.73× |
| 1527 tests < 50 ms | 8.9 | 5.9 |

Running a test costs about as much CPU as it would cost in simulated seconds.
There is no arbitrage on the test itself. The only possible gains are the
**2 s per-worker setup** and the makespan of a plan that becomes empty or smaller.

## 2. Fixed overhead of verifying a patch

| step | CPU |
|---|---|
| fresh `import sqlglot` | 0.046 s |
| import a dialect test module | ~0.04 s |
| import `tests/test_optimizer.py` cold (duckdb + pandas) | 0.8–1.5 s; parent pre-imports them once (~0.9–1.1 s), after which forked children skip that cost |
| fork + overlay + sqlglot + 1 cheap dialect test, end to end | ~0.15 s |
| pytest collection check (import all 60 test modules) | 0.22–0.40 s |
| optimizer fixture tests | 1.0–1.6× catalog seconds |

## 3. Mechanics (implemented, tested)

- `apply_patch`: pure-Python strict apply. Exact positions and context, else
  `PatchError` (no verification). It produced byte-identical results to
  `git apply` on **878/878** bench patches and refused the one patch git also refuses.
- `build_overlay`: a symlink farm in `/tmp` (≈ kilobytes) where only directories
  on a changed path are real. Module `__file__` stays inside the overlay, so
  fixture reads (`tests/helpers.FIXTURES_DIR`) see patched `.sql` files. The
  `__pycache__` of a directory with a changed `.py` is dropped so stale bytecode
  can never shadow the patch.
- A forked child (stdout/stderr redirected to /dev/null, own process group,
  hard deadline, SIGKILL of the group) imports the test modules fresh and runs
  the selected unittest methods in catalog order with per-test SIGALRM timeouts.
  Failures, errors, failed subtests and unexpected successes count as FAIL.
  Skips, class-fixture errors, timeouts and missing reports count as unverified.
- The tests use `ProcessPoolExecutor(mp_context=spawn)`. Spawning re-imports
  `__main__` and starts one interpreter per host CPU (16 here, ~1 s CPU each).
  The child swaps in an inline executor that keeps the **pickle round-trip** of
  function, arguments and result. That round-trip is what the serde patch
  (`3utz`, 37 failures) breaks, and it is still detected.
- Patches touching `conftest.py`, pytest/packaging config, or anything outside
  `sqlglot/` and `tests/` are refused: unittest would not see their effect on pytest.

Fidelity on the official sample: across all 30 patches (verified set = the
failing tests' files), **0 false passes, 0 false failures**, and every labelled
failure in the verified set was detected.

## 4. Where isolated verification is wrong (bench `exotic` set, our oracle labels)

1. **Collection errors.** With pytest's default behaviour, one test module that
   fails to import interrupts the session, and the oracle labels **all 1620 tests**
   failing. Examples: deleting `optimizer/helpers.py` or `dialects/mysql.py`,
   renaming `spark2.py`, a syntax error in `helper.py`, dropping an export from
   `sqlglot/__init__`. Running only the candidate modules misses this. Fixed: the
   child first imports every `tests/**/test_*.py` (0.22–0.40 s) and reports
   `collection_error`, and the entrypoint then goes full suite.
   `--dump-collect` lists the 160 source files imported at collection, so a patch
   touching none of them (and adding/removing no `.py`) skips the check.
2. **Order-dependent state (not fixable cheaply).** `7y2o` removes `"Doris"`
   from `DIALECTS`. In the full session `tests/test_dialect_entry_points.py`
   clears `Dialect._classes` in setUp/tearDown, and it runs before `test_parser.py`
   and `test_time.py`. Doris can then no longer be resolved, so those tests fail.
   In isolation they pass. Faithful reproduction would mean replaying the session
   prefix, which is essentially the whole suite. **So an isolated pass is strong
   evidence, not proof.** That alone makes dropping *required* tests risky: one
   miss (300 s) cancels about 150 verified-clean patches' worth of gains (≈2 s each).

## 5. End-to-end comparison (bench harness `eval/run.py`)

Impact model as of 2da8a53 (WIP snapshot), sample + 45 labelled exotic cases (75 patches):

| `BLAST_VERIFY` | simulated | CPU | total | misses |
|---|---|---|---|---|
| `0` (off) | 1072.600 | 12.7 | **1085.3** | 0 |
| `collect` | 1072.600 | 21.5 | 1094.1 | 0 |
| `insure` | 1072.600 | 21.5 | 1094.1 | 0 |
| `drop` | 1068.250 | 22.7 | 1090.9 | 0 |

The official 30 alone, with an earlier, looser gate in drop mode: 162.3 sim +
19.7 CPU = 182.0, against 175.2 with verification off. The model's required sets
are tight (often exactly the one failing test), so verification mostly
confirms failures (≈75% of verified patches had a failure) and pays CPU for
nothing. It won only on cheap dialect patches proven clean (fryt, gbav, ktz3,
2qn5: about +1.8 s each).

Why it can't win much: the best case per patch is the plan's simulated cost
(usually 2.0–2.6 s for small plans) minus about 0.15–0.5 s CPU. That needs
`P(clean | small plan) × ~2 s > CPU`, which holds only for patches whose
candidates are a few cheap dialect tests. Even then it is about 1–2 s per
proven-clean patch, set against the order-dependence risk in §4.2.

## 6. Modes left in the code

`BLAST_VERIFY` = `0` (default) | `collect` | `insure` | `drop`. Requires the
pristine checkout at `BLAST_SQLGLOT` (default `/app/sqlglot`), the test
dependencies (`requirements.lock`), and optionally `BLAST_COLLECT_MODULES`.
Without the checkout the pass is skipped. The static plan for every patch exists
before verification starts, verification only ever replaces a plan after a
clean result, and the pass stops starting jobs at `BLAST_VERIFY_END_S` (130 s).

- `collect`: pytest collection check for patches touching collection-time
  modules. A collection error gives the full suite. It also enables the
  `if_collects` hook: impact may return `{"full": True, "if_collects":
  {"required": ..., "plausible": ...}}` when it goes full *only* for fear of a
  collection error, and the narrower plan is used if the check passes.
- `insure`: plus cheap `plausible` tests (≤1 s CPU per patch). A failure outside
  `required` adds it, plus every unverified plausible test. It **never removes** a test.
- `drop`: plus removes required tests seen passing (gated on
  `P_CLEAN × saving > est CPU`). Not recommended (§4.2).

To enable any mode, the image needs, before `COPY selector/`:

```dockerfile
COPY requirements.lock /tmp/requirements.lock   # same versions as the labeler
RUN pip install --no-cache-dir -r /tmp/requirements.lock
RUN python - <<'EOF'
import io, tarfile, urllib.request
u = "https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz"
tarfile.open(fileobj=io.BytesIO(urllib.request.urlopen(u).read())).extractall("/app/src")
EOF
RUN mv /app/src/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /app/sqlglot \
 && python -m compileall -q /app/sqlglot/sqlglot /app/sqlglot/tests
ENV BLAST_VERIFY=collect
# after COPY selector/:
RUN SKIP_INTEGRATION=0 python -m selector.verify --dump-collect /app/sqlglot /app/collect_modules.json \
 && chmod -R a+rX /app
```

## 7. Recommendation

- Ship with verification **off**.
- `collect` is the only mode with a plausible net benefit on unseen
  change kinds, and only if impact opts into `if_collects` for patches where it
  goes full purely for collection-error risk. Otherwise it costs about
  0.3 s per checked patch (≈ +9 s per 75 patches here) as pure insurance against
  collection-error misses that impact already handles on every labelled case.
  Revisit if held-out results show impact missing collection breaks.
- `insure` only makes sense if impact starts emitting a non-empty `plausible`
  set of cheap tests.
- Do not use `drop`.
