# Candidate-4 measurement

The first official run of candidate-3 cost 177.103 simulated seconds and about 1.06 CPU seconds, with no misses. The 30-patch sample extrapolates to 154.151 simulated seconds per 40. The 22.952-second difference is about the cost of one unnecessary TPC-DS selection. A different mix of actual failures also fits the evidence. The sample bootstrap for 40 patches spans 106.7 to 207.0 seconds at P10–P90. The scorer withholds the hidden plans and labels, so the total alone cannot identify the cause.

## What the heavy tests reach

The parser budget test calls the default parser on a literal SELECT, 500 columns, 10,000 copies of a depth-100 equality expression, then a 100-column equality query and a final three-literal SELECT. It checks the exception type and text for two budgets and that a limit leaves the next parse usable.

The executor parses and sends all 99 TPC-DS fixture queries through a spawned process pool. Its worker executes 57 tagged queries with the supplied 24-table schema. Of the 99 parsed queries, 97 contain joins, 82 grouping, 52 subqueries and 15 windows. Among the 57 executed, the corresponding counts are 56, 50, 28 and zero. Both phases depend on parsing and serialization. Class setup also loads the TPC-H fixtures and CSVs. A change affecting an unexecuted query’s parser can still fail the test.

The offline tracer runs each complete test from the clean pin. It records outcomes of if/while conditions and short-circuit operands and exact primitive inputs at supported comparisons. Spawned Python processes use the same instrumentation. The builder requires an exit profile for each started process and a passing pytest run. Comparisons with unsupported operands or over 64 distinct values are discarded. The frozen map has 86 eligible comparison sites for TPC-DS and 15 for the parser.

At runtime the selector replays the original and patched operators for each recorded input where the operand expression is structurally identical. Every comparison must agree. Another rule checks if an unchanged prefix prevented evaluation of an added or changed operand, or if an omitted SQLGlot enum type guard was neutral on every reached path. Each rule affects only coverage attributed to the changed decision. Other edits and direct source reads can still require the same test. No incoming Python is executed. Type guards assume the pinned expression payload reader and DType check have no externally visible side effects. Execution order, environment and caches can produce values or arcs absent from an isolated clean run.

## Tuning ablation

The 110-patch tuning set includes the official 30. Every stage missed zero patches. Each row gives the incremental simulated saving with preceding changes enabled. Break-even is the maximum *increase* in miss probability that would consume that saving at 300 seconds per miss.

| Change | Score-changing tuning patches | Saved s | Saved s / 40 | Break-even across all patches | Conditional break-even on score-changing patches |
|---|---:|---:|---:|---:|---:|
| Verify variable binding with bytecode instructions | 2 | 34.078 | 12.392 | 0.1033% | 5.68% on average |
| Scope an isolated, raising autouse fixture to its pytest subtree | 1 | 27.591 | 10.033 | 0.0836% | 9.20% |
| Use recorded branch direction for unchanged decisions | 1 | 14.090 | 5.124 | 0.0427% | 4.70% |
| Replay primitive comparisons | 0 | 0 | 0 | No measured margin | Unestimated |
| Equivalent CPU and scheduling shortcuts | 0 | 0 simulated | 0 simulated | CPU saving only | Same plans by construction |

CPython stores attribute spellings and variable references together in co_names. The revised binding check distinguishes global/name access from attribute access before widening to module importers. Generator, coroutine, free-variable and cell-variable changes still widen. Narrower binding decisions rely on the inherited source-impact analysis. Losing a scope through a missing compiler instruction classification is a residual risk.

For pytest, the recognized fixture module compiles, imports pytest and defines only private argument-free autouse fixtures with literal raises of known built-in exceptions. All catalog tests beneath that directory are selected, plus recorded directory readers. A root fixture kept the same full catalog at the same cost. Other config/collection changes still fall back. Dropping a 36.817-second plan to zero breaks even at 12.27% conditional miss probability. Narrowing that plan to 9.226 seconds has the lower 9.20% break-even above. The prior unqualified file-only experiment missed 15 tuning cases.

The scheduler searches one through eight workers, including the 2-second fee for each. The new code defers packing the full catalog until needed and stops scanning sorted free riders after the shortest remaining test no longer fits. Cache construction runs inside the pinned image. It prepares compiler bytecode, symbol occurrences, source and coverage maps. The identity rule uses the enclosing function when all edits lie within it.

Four tuning patches changed cost. The observed savings per 40 are specific to this mixed tuning set. Each behavioral rule has at most two score-changing examples, which leaves its conditional miss probability poorly estimated. The primitive comparison rule has zero observed tuning benefit. A separate boundary check exercised 75 recorded sites, including comparison negation and equality at strict/inclusive thresholds.

## Candidate-3 cost attribution

Full fallbacks include their entire cost. For other plans, charge worker fees first, then assign the duration of each test on one busiest worker. The mid-weight bucket includes generator memory, TPCH-named tests and tests/test_optimizer.py. The heavy columns include only passing heavy tests. Other includes required heavy failures. This allocation sums to the score and describes billed time. Removing a test can expose a different busiest worker, so its removal need not recover the full amount.

| Seconds | Tuning, 110 | Registered holdout, 188 |
|---|---:|---:|
| Full-suite fallback | 257.719 | 147.268 |
| Passing TPC-DS | 278.902 | 429.080 |
| Passing parser budget | 28.817 | 374.621 |
| Mid-weight tests | 123.169 | 120.837 |
| Worker fees outside fallbacks | 234.000 | 490.000 |
| Other | 163.339 | 213.217 |
| Total | 1085.946 | 1775.023 |

Each cell below counts heavy failures / selections. Attribute a selection to the first reason adding its bit, or the final fallback if one occurred. A dash means zero selections. The reason groups combine equivalent diagnostics across source locations.

| Reason | Tuning TPC-DS | Tuning parser | Holdout TPC-DS | Holdout parser |
|---|---:|---:|---:|---:|
| Changed statement | 1/7 | 0/1 | 5/12 | 0/3 |
| Condition | 1/4 | — | 1/9 | — |
| Class attribute | 0/1 | — | 1/13 | 0/9 |
| Module | 0/3 | — | 0/4 | 0/1 |
| Name references | 2/2 | 1/1 | — | — |
| Signature | — | — | 0/1 | — |
| Fixture | — | — | 1/1 | — |
| Other direct reason | 1/2 | — | 1/1 | — |
| Fallback, new pytest module | 0/1 | 0/1 | — | — |
| Fallback, pytest configuration | 0/2 | 0/2 | — | — |
| Fallback, config/collection file | 1/2 | 1/2 | — | — |
| Fallback, compiler protocol/binding | 0/2 | 0/2 | — | — |
| Fallback, collection import | — | — | 1/3 | 1/3 |
| Fallback, internal exception | — | — | 0/1 | 0/1 |

The registered holdout delivered zero measured simulated saving from the frozen changes. It includes 22 historically exposed exotic cases. On that mix, the main remaining false-positive cost is ordinary impact selection of passing heavy tests, outside fallback. Worker fees also dominate the absolute totals, but many pay for genuinely failing tests. The hidden aggregates do not establish which of these mechanisms occurred in practice.

The baseline reason-attribution script encountered a pre-existing class-diff exception on holdout; the normal entrypoint caught it and emitted a valid full plan. Reason accounting grouped internal exceptions as fallbacks. No holdout patch was used to change the candidate. The first archive build failed because the Docker context excludes tools/. The build helper was moved into selector/ and the clean archive then passed.
