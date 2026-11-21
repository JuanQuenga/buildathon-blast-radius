# Final entry

**Candidate 14 (`da0ed24`) remains the provisional final entry.** On official practice, candidates 13 and 14 both recorded 150.503 simulated seconds, about 24 CPU seconds and zero misses. Candidate 14 also covered all failures on the 36 local fixture mutations at lower worker cost. Its stress replay still has deadline-sensitive differences. All tags prefixed `diagnostic-` are practice experiments with intentional omissions. [Release measurements](CANDIDATE14.md) · [Practice diagnostics](PRACTICE-DIAGNOSTICS.md)

The full-data CPU experiment failed the C13 holdout heavy-drop gate. It remains a research tag. [CPU experiment and release checks](ORDERED-CPU.md)

## Candidate-12 sixty-patch scenarios

These are expected simulated plus CPU seconds conditional on zero misses. The 270-second limit applies to the selector's real wall time. The simulation can exceed 270.

| Mix | Candidate 12 | Candidate 5 | Additional per-patch miss probability that erases C12's lead |
|---|---:|---:|---:|
| Resample the 30 official sample patches at their observed frequencies | 192.7 | 232.0 | 0.218 percentage points |
| 48 sample-like plus 12 drawn from measured non-sample kinds | 278.6 | 324.5 | 0.255 percentage points |

The first row doubles the observed sample totals. For the second, C12 used 423.748 simulated + 11.584 CPU seconds across 42 held-out non-sample-kind cases. C5 used 483.418 + 2.930. These included 21 reverts, 17 test edits and four formatting, metadata or encoding cases. The mean of this proxy supplies the cost of the twelve unfamiliar draws. Genuinely new kinds could cost much more.

For an unconditional expectation, add 18,000 times the entry's per-patch miss probability to each score. The observed point estimate of C12's **additional** miss rate is zero on 197 paired holdouts. At that estimate, the table also gives the empirical expected scores. A half-event sensitivity allowance gives 0.253% per patch, or 45.5 extra seconds per final, if charged entirely to C12. That allowance would reverse the sample-only choice and nearly tie the 20% proxy scenario. It is an explicit risk assumption. The one-sided independent-sampling 95% upper bound for zero events in 197 observations is 1.51%. Related mutations, tuning on earlier aggregates and an unknown final mix limit both estimates. If all incremental misses occur on the 12 unfamiliar patches, their conditional break-even risk is about 1.28 percentage points.

A fixed 60-patch batch combined all 30 sample patches, 18 mechanical holdouts of sample kinds and 12 of other kinds. Candidate 12 scored 401.446 simulated seconds, zero misses and 26.673–30.036 CPU seconds across five runs. The plans had identical SHA-256 `bc7cc1504ccb3c1e934a009ba79418c7cc0d61f193a62dbd77ff031d3915c74f`. Wall time ranged from 27.41 to 30.91 seconds. Candidate 5 scored 507.053 simulated + 2.320 CPU seconds and zero misses on the same inputs.

## Mechanisms shared with candidate-13

| Mechanism | Evidence required to remove work, and fallback |
|---|---|
| Static Python impact | Apply the diff and compile it against the pin. Match AST scopes, covered statements, bindings, class families and imports. Protocol, dynamic export, unknown path, collection, configuration or analysis hazards broaden the plan. Runtime readers of source and unknown catalog tests stay scheduled. |
| Fixtures, moves and pytest | Include traced readers and directory enumerators. Treat packaging config as pytest config when applicable. Discovery-only changes and disabling a demonstrably absent plugin can request collection. Reconcile every catalog node, lock missing nodes and failed-file tests against all subsequent probes, and preserve the broad plan on unexplained errors. Autouse fixtures and runtime plugin effects can require the full catalog. |
| Pin value profiles | Version tuples, operand identities, branch outcomes, tokenizer keys, date units, expression validation and SHOW dispatch use pinned, observed consumers. Fresh-builder copy removal requires an unaliased table, scalar operands and the original methods. Keep explicit dictionary derivations, observable fields and unmatched AST edits. A companion comment must be AST-identical. A companion fixture still contributes its own readers. |
| TPC-DS | Check setup/teardown, optimizer scope, dispatch, fixtures and execution-tail coverage. All 99 queries parse at the pin and 57 execute. Replay reached rule inputs or queries. Changed plans and execution-stage edits require ordered result comparison. A changed reference SQL requires the actual DuckDB assertion. Failed imports, invalid receipts, incomplete checks, timeouts or uneconomic continuations keep the test. Warm replay checks the actual package identity, including initializers. Isolated replay still assumes compatible process state. |
| Parser and generator | Use small parser assertions only when the large case misses the edit. Scaled checks retain guards for thresholds, limits, loops and reached full-size parser code. Verify generator registry identity after reset and check the affected dialects or the whole allocation test. Allocations, caches or order effects outside the recorded paths remain risks. |
| Small worker checks | Run the actual unittest methods. Clear the worker only on complete success. After a failure retain every test without a positive receipt, including the failing test. Errors, skips, unexpected successes, setup failures and missing receipts preserve the candidates. |
| Scheduling | Price the complete replacement worker plan. Fill unused worker capacity with plausible tests then free riders. A failed verifier or supervisor preserves a valid static plan. Reservations and query prices use fixed pin work estimates. |

## Repeatability and limits

Candidate 12 limits each first-stage mode to twelve probes. Candidate 13 removes this limit and the probability cutoff on heavy-test checks. It prices recoverable schedule time against pin work, with a fixed allowance and a separate emergency CPU ceiling. Catalog order, patch order, hash seed, query IDs and search-node limits are fixed. A timeout debits its fixed CPU allowance. The published image uses one Python worker for spawned test pools and a two-CPU Docker quota. CPU time and the ranking score can vary even when the JSONL plans are identical.

Wall-dependent emergency exits remain. Static selection has a five-CPU-second patch limit, a 60-CPU-second batch limit, a 20-second blocked-I/O watchdog per patch and a 90-second static wall ceiling. The verifier has per-child CPU timers, a memory cap and a supervisor measured from selector startup, 120 wall seconds for a 40-patch batch or 180 for 60. An expired supervisor discards uncommitted probe results and returns the static plans. The default path emits only after validation. Severe host stalls, resource exhaustion, threshold-adjacent probes or changes to process state could therefore change a plan. Repeated plans on checked batches establish repeatability on those inputs; universal identity across official reruns is unproven.

Candidate 10 failed the official image build. Candidates 12 and 13 use committed pin recordings, a locked environment and a clean-build gate on four CPUs with 8 GiB and no cache. The C13 runtime-source checkout passed the clean build gate at 9.41 MB. Only the coordinator submits.
