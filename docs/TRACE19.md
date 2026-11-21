# Heavy-test execution traces at the SQLGlot pin

Candidate 18 already uses per-test executed lines. Its offline map combines forked and sequential runs, import and collection attribution, fixture reads, and class-family usage. The selector applies the patch to the pinned sources and examines old and new syntax trees for statement spans, insertions, and changes to bindings. Its original worker tracing substitutes an inline executor that preserves the pickle round trip. The follow-up below checks that substitution against real spawned workers. The [track rules](https://www.joinbuilders.org/buildathon/tracks/blast-radius) permit pinned coverage maps. All patch labels and scoring stayed outside the image.

## Independent recording

Each target ran alone under pytest in the release image with hash seeds 0 and 137. The hook starts at interpreter startup and propagates to spawned Python workers. All five targets reproduced the existing executable line set exactly in both runs. Some empty package initializers emitted a synthetic line 0, which has no source statement.

| Heavy test | Pinned executable lines | Python processes recorded per run |
|---|---:|---:|
| Execute TPC-DS | 23,301 | 3 |
| Execute TPC-H | 14,563 | 3 |
| Optimized TPC-H | 16,890 | 1 |
| Generator memory | 23,689 | 1 |
| Parser max nodes | 12,308 | 1 |

To reproduce one recording, run the following from the repository on a host with Docker and the candidate-18 image. The output contains source-relative line numbers and code entry, source read, and inspection observations. A hash seed is a repeatability check. It cannot establish determinism for every possible runner state.

```sh
python tools/record_heavy_lines.py IMAGE tests/test_executor.py::TestExecutor::test_execute_tpcds 137 tpcds.json.gz
```

## Marginal effect on candidate 18

The replay considered each selected heavy test and each heavy probe that C18 had already cleared. It required same-path existing package Python files, no directly touched recorded line, and admissible changed statement regions outside coverage. Memory-sensitive behavior was excluded. The simulated cost below removes qualifying tests from the measured C18 workers and deletes empty workers. These are counterfactual costs, with C18's other assignments held fixed.

| Partition | Patches | Extra scheduled heavy tests removable | C18 simulated s | After line check, s | Known false drops |
|---|---:|---:|---:|---:|---:|
| Official sample, included in tuning | 30 | 0 | 95.064 | 95.064 | 0 |
| Tuning | 161 | 1 | 1,302.610 | 1,302.610 | 0 |
| Holdout | 197 | 0 | 1,483.558 | 1,483.558 | 0 |
| Additional labelled | 169 | 2 | 1,917.507 | 1,917.507 | 0 |
| Generated | 104 | 1 | 2,660.839 | 2,660.839 | 0 |
| Fixture edits | 36 | 0 | 449.576 | 449.576 | 0 |

All four additional omissions were TPC-H tests sharing workers with other tests. The generated optimized TPC-H case was checked directly against the real patched test. Of 236 TPC-DS decisions, just one qualified, and C18 already cleared that selection with a 0.223-second probe. Comparing deleted line numbers alone would also clear known failures, including generator-memory regressions. The code and allocation footprint can matter even if a changed function never runs.

Of 44 matched TPC-DS cost-screen skips, 42 changed at least one reached line. The other two edited a dialect class namespace and a module-level expression containing a generator. Neither passed the statement guard. All 17 batch-budget skips touched reached lines. Both patches in the two-probe CPU reconstruction also touched reached optimizer lines. The official D2 receipt identifies an unattempted selection, but does not disclose its patch or its admission reason.

## Nested compiled expressions and shared state

A finer prototype compared compiled parent instructions, constants, exception and line tables, and binding layouts, then checked whether a changed nested code object was ever entered or inspected. It found one additional paid TPC-DS selection on the matched holdout. Patch d4ax changes an identity comparison inside a JSONPath generator expression. Removing all three executor selections together recovers 5.785 simulated seconds. The TPC-DS oracle passes even when the failing JSONPath test runs immediately before it. Two full-suite default-order traces, including prior tests and real spawn workers, also show that the expression had never run at the executor checkpoints. It had run by the later parser checkpoint.

That result supports an omission for this particular observation. A per-target trace alone cannot certify it for arbitrary shared test state. In a two-test control, the first test fills a cache from a generator expression; the second simply reads the cached value. Changing an arithmetic constant inside the generator makes the second test fail after the first test, even though its own trace is identical on both versions. Both isolated runs pass. Requiring the changed body to be unreachable from every possible predecessor rules out the observed JSONPath opportunity: the pinned JSONPath test reaches that body. A sound extension would need evidence of isolation or a bound on state that previous tests can carry forward.

There is no candidate-19 release from this experiment. The existing line-map rule leaves every paid TPC-DS selection in place. The more aggressive nested-expression rule failed the shared-state control. Neither the organiser's timing feedback nor these local traces establish how competing entries select tests.
