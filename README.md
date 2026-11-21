# Blast Radius entry: sqlglot test selector

Entry for the Builders Buildathon **Blast Radius** track (RWX). For every patch in
`/data/commits/*.patch` (each a unified diff against sqlglot
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`) the image prints one JSON line on
stdout choosing which tests from `/data/tests.json` to run and how to split
them across up to eight simulated workers.

## How it works

- `selector/main.py`: entrypoint. Reads the catalog and patches, asks the
  impact model for each patch, schedules, validates every line, prints. Any
  exception, timeout or missing map for a patch falls back to the full suite
  for that patch. A global time budget keeps the batch well under the limit.
- `selector/impact.py` + `selector/maps/`: maps each changed file/hunk to
  the tests that can observe it. The maps come from the pinned sqlglot checkout
  (coverage, imports, fixtures). They contain no answers and no table from
  patches or edit kinds to failing tests.
- `selector/schedule.py`: minimises `max worker load + 2 s × workers` over
  k = 1..8 (longest-first, local search, exact branch-and-bound when small),
  then fills the slack under the makespan with plausible tests and then any other
  test ("free riders"). The extra tests cost nothing and reduce the chance of a miss.
- `selector/patch.py`: dependency-free unified-diff parser (`-p1` like `git apply`).
- `selector/verify.py`: optional runtime verification. It applies the patch into
  a `/tmp` overlay and runs candidate tests in a forked child. **Off by default.**
  See [docs/verification.md](docs/verification.md) for the measurements and why.
- `eval/`, `tools/`: offline benchmark and map building. Not copied into the image.

The full-suite fallback on the official catalog is 4 workers at 36.817 s per
patch, which is the lower bound: the longest test, 28.817 s, plus 4 × 2 s.

## Run locally

```sh
make local KIT=/path/to/starter/sample-kit    # no Docker
make sample KIT=/path/to/starter/sample-kit   # Docker, scorer isolation flags
```

## License, libraries, provenance

- License: MIT (see `LICENSE`). Everything in this repository was written
  during the hackathon weekend (from 2026-11-20). **No pre-weekend code is used.**
- Runtime: Python 3.13 standard library only, on the official
  `python:3.13-slim` image (pinned digest).
- Open-source projects used: [sqlglot](https://github.com/tobymao/sqlglot) (MIT),
  at the pinned commit, used to build maps offline and as the code under test.
  Offline tooling uses pytest and coverage.py; see `tools/` and `eval/` for exact versions.
- The official starter kit (`score.py`, sample patches) is used only for local
  evaluation and is not vendored. Sample answers never reach the selector or the image.
