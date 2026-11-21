# Candidate 18 robustness release

Candidate 18 starts from candidate 14 and preserves its probe order, admission rules, CPU caps and test scheduler. It bounds input reads, validates unified-diff hunk lengths and uses one monotonic work deadline from the start of the batch. Patches untouched when the deadline expires retain their full-catalog fallback. With 40 or fewer patches the deadline is 120 seconds. With 60 it is 180 seconds, leaving 90 seconds for shutdown and output on the final runner.

The reader accepts up to 4 MiB per patch, checks UTF-8 strictly and normalizes CRLF. For non-text input, an oversized patch, a line over 256 Ki characters, more than 2,048 hunks, unsupported file modes, or inconsistent diff lengths, the selector emits the full catalog. The largest patch in the 462-case matched and mutation corpus was 145,813 bytes. Supported fixture and Python impact decisions retain their original behavior.

## Release checks

The no-cache linux/amd64 build finished in **48 seconds** in a fresh builder limited to 2 CPUs and 3.5 GB. Runs used the starter's two-CPU, read-only, network-disabled, unprivileged Docker flags.

| Partition | Patches | C18 simulated s | C18 selector CPU s | Missed commits | Plan bytes vs archived C14 |
|---|---:|---:|---:|---:|---|
| Official sample, included in tuning | 30 | 95.064 | 1.299 | 0 | Identical |
| Matched tuning | 161 | 1302.610 | 105.531 | 0 | Identical |
| Matched holdout | 197 | 1483.558 | 125.021 | 0 | Identical |
| Generated mutations | 104 | 2660.839 | 88.119 | 0 known heavy misses | Identical |
| Fixture mutations | 36 | 449.576 | 0.364 | 0 known relevant misses | Identical |

The 169 additional fully labelled cases had zero misses. All 20 additional labelled holdouts matched the archive. Of the 149 additional labelled development cases, 141 matched the archive on the initial run. Four differences involved historical C14 timeouts. Three historical collection successes hit their 6.4-second limits in the new run. A fourth patch retained its generator after an earlier patch's TPC-DS timeout consumed the batch allowance. **Fresh tagged C14 reproduced all 63 C18 plan rows in the two batches containing those four historical non-timeout differences.** A further C18 repeat moved the borderline TPC-DS and collection checks across their limits again. An unconditional promise of repeat-identical plans would exceed the observed behavior of C14 itself. Another 350 cases had no full-suite label.

All 21 adversarial patches yielded valid, nonempty plans. Nineteen selected all 1,620 catalog tests. The module deletion and rename retained 1,577 after collection, including both heavy executor tests. The sweep covers malformed and empty input, truncated or overlong hunks, header-only diffs, mode changes, an oversized patch, very long lines, thousands of hunks, a binary patch, NUL, invalid UTF-8, CRLF, setup and collection configuration, and new modules. Forcing the deadline before any work selected the full catalog on all 21 cases. A separate mid-batch control checks that an already-completed plan survives and the next patch retains its default full plan. The 16 selector unit tests and failure, premature exit, collection, result-difference and timeout controls passed. The host-side checker can be run with `python tools/check_input_fallbacks.py IMAGE CATALOG`.

The two 60-patch stress batches finished in **152.62 and 160.79 wall seconds** inside the selector. The slower run left 109 seconds before the contest limit. Against candidate 14, both retained every historical heavy exclusion. Against older tags, the historical monotonicity check still flags one extra max-nodes selection against C8 on generated mutations and one against C13 on the stalled stress batch. The matched tuning and holdout heavy exclusions pass against both older tags.

## Timer and practice forecast

Cold and forked probes already install a process CPU timer (`ITIMER_PROF`) inside the process running patched code. The independent parent wall watchdog also covers a stalled child. On the 31 successful matched TPC-DS receipts, **0/31** reached 85% of any successful stage's installed CPU cap. The closest used 70.2%. In the constructed dual-probe workload, unchanged C14 sometimes cleared a borderline edit in 9.59 seconds and sometimes expired at 11.49 seconds. Keeping the original cap preserves candidate 14's policy.

For practice, our central forecast is **174.5–175.5 seconds** if the same probes and fallbacks reproduce C14's 150.503 simulated seconds and about 24–25 selector CPU seconds. A borderline clearance returning to the schedule would add about 21.45 simulated seconds, taking that scenario to roughly **196–200 seconds** with 24–28 CPU seconds. The hidden batch composition and CPU speed remain unknown. Candidate 14 remains the selected final entry pending the coordinator's practice verification.

D2's practice receipt BB-DC55275C reported 150.503 simulated seconds, 24.83/24.01/24.36 CPU seconds and zero missed commits. Combined with D1, this puts the 20.020-second reduction on the unattempted side of that diagnostic. The receipt does not distinguish an eligibility guard from the cost screen or a batch admission limit.
