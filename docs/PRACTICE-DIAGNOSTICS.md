# Practice diagnostics

Use these tags only for hourly practice measurements. All five start from candidate 14, use the same diagnostic implementation, and differ in the default mode embedded in the image. Candidate 14's official baseline was 150.503 simulated seconds and zero missed commits.

| Order | Tag | Commit | Change to the candidate-14 plan |
|---|---|---|---|
| 1 | diagnostic-d1-tpcds | 91ca333 | Remove TPC-DS when the selector lacks a failing full-frame comparison. Protect edits to the test body and the fixture's source SQL. |
| 2 | diagnostic-d2-attempted | 0406efd | Apply D1 only when the TPC-DS probe was attempted. Covers probe errors, timeouts and inconclusive comparisons. |
| 3 | diagnostic-d3-unattempted | 21b4e21 | Apply D1 only when the TPC-DS probe was never attempted. Includes eligibility, collection, queue admission, exhausted batch budget and incidental selections. |
| 4a | diagnostic-d4-parser | fcf8d67 | If D1 yields no meaningful worker savings, remove the max-nodes parser test unless a pinned assertion failed or the test body changed. |
| 4b | diagnostic-d4-full | 2706281 | Separately replace surviving full-catalog fallbacks with the conditional required set, if available, and tests in directly edited test modules. |

The TPC-DS comparator runs the test's DuckDB query, converts full executor rows into the same pandas frame as the test and records a positive only when its final frame comparison raises an assertion. It adds CPU on paths with divergent digests. The production selector continues to retain inconclusive selections.

### Local measurements

Two-CPU Docker runs with the starter limits. Only patch bytes and the catalog entered the container. Worker seconds exclude the miss penalty. The fixture oracle covers the five relevant optimizer and executor tests.

| Mode | Sample worker s | Sample CPU s | Missed / 30 | Fixture worker s | Missed / 36 |
|---|---:|---:|---:|---:|---:|
| Candidate 14 | 95.064 | 0.967 | 0 | 449.576 | 0 |
| D1 | 80.116 | 3.40–3.57 | 1 | 417.134 | 0 |
| D2 | 80.116 | 3.50 | 1 | 449.576 | 0 |
| D3 | 95.064 | 3.13 | 0 | 417.134 | 0 |
| D4 parser | 95.064 | 1.50 | 0 | 449.576 | 0 |
| D4 full | 95.064 | 1.51 | 0 | 449.576 | 0 |

D1 kept the genuine frame difference on sample patch `2jrq`. The `3utz` probe failed during parsing and D1 removed TPC-DS, giving 380.116 penalized simulated seconds. D1 produced byte-identical plans on repeats of both batches. D1's tagged checkout was 9,481,442 bytes. Its four-CPU, 8 GiB clean build and load took 137 seconds. D2 through D4 used the shared image with each tag's mode selected at runtime for local comparisons.

### Reading the official feedback

Subtract 300 seconds per missed commit from reported simulated time. Compare the resulting worker time with 150.503. If one isolated 23.454-second worker disappears, expect **127.049 worker seconds**, or **427.049 penalized simulated seconds** with one miss. If D1 costs 24–30 CPU seconds, the respective totals are **151.0–157.0** or **451.0–457.0**. If no worker cost disappears, the zero-miss total under that CPU assumption is **174.5–180.5**. Aggregate feedback cannot identify a patch. Repacking can change worker fees, makespan and which other tests fit into spare capacity. A single run can remove both a passing and a failing selection. A new missed commit can also result from losing a different test that previously fit for free.
