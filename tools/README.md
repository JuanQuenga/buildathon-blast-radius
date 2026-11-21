# tools/ — offline map builders for `selector/impact.py`

Everything here runs **offline, once**, against the pinned sqlglot checkout
(`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`). The output is
`selector/maps/` (≈1.3 MB, committed), which the selector loads at runtime with
the standard library only. Nothing here reads benchmark or sample answers;
`eval_impact.py` uses them for measurement only.

| File | What |
| --- | --- |
| `trace_plugin.py` | pytest plugin. Per test: executed lines in `sqlglot/` + `tests/` (`sys.monitoring` LINE events, each location disabled after its first hit and re-armed per test), files opened / directories listed under the checkout (audit hook, import system excluded), sqlglot classes instantiated or whose class object was touched (wrapped `__init__` and metaclass `__getattribute__`, recorded with their MRO), the test class MRO, and the outcome. `TRACE_FORK=1` (default) forks every test after collection, so lazy imports, caches and `setUpClass` work are attributed to each test that needs them; `TRACE_FORK=0` runs the file sequentially (warm caches, real order). The spawn `ProcessPoolExecutor`s used by the optimizer/executor tests are replaced with an in-process executor that still pickles arguments and results (pickling goes through `sqlglot.serde`). |
| `run_trace.py` | Runs the whole catalog in parallel chunks with the plugin. |
| `namespaces.py` | Imports every sqlglot module in the profiling venv and dumps module namespaces, class namespaces of by-name-imported classes, the MRO of every sqlglot class and the importable top-level modules. |
| `build_maps.py` | Unions one or more trace runs and bakes `selector/maps/index.pkl.xz` (tests, deduplicated test bitmaps, per-file line → bitmap with collection-time lines expanded to every test of the collected file, opens, listdirs, class families, collection imports, static scopes/statement spans, module import graph, namespaces) and `selector/maps/src.pkl.xz` (pinned `sqlglot/**.py`, `tests/**.py`, `pyproject.toml`, `setup.cfg`, list of tracked files). |
| `eval_impact.py` | Fast in-process evaluation of `impact.select` on labelled case dirs (misses, simulated seconds with `selector.schedule`, full fallbacks, CPU). |

## Regenerate

```sh
BR=blast-radius                       # scratch dir with the pinned checkout
W=$BR/work-impact
uv venv -p 3.13 $W/.venv
uv pip install -p $W/.venv/bin/python -r $BR/starter/sample-kit/requirements.lock
rsync -a --exclude .git $BR/sqlglot/ $W/src/          # writable copy of the pin
CAT=$BR/starter/sample-kit/sample/tests.json
PY=$W/.venv/bin/python

# 1. traces: forked/cold + sequential/warm, different hash seeds (~75 s each)
$PY tools/run_trace.py --src $W/src --python $PY --catalog $CAT --out $W/trace-fork1 --fork 1
$PY tools/run_trace.py --src $W/src --python $PY --catalog $CAT --out $W/trace-fork2 --fork 1 --seed 2
$PY tools/run_trace.py --src $W/src --python $PY --catalog $CAT --out $W/trace-seq1 --fork 0 --seed 1
$PY tools/run_trace.py --src $W/src --python $PY --catalog $CAT --out $W/trace-seq2 --fork 0 --seed 3

# 2. namespaces of the pinned package
$PY tools/namespaces.py $W/src $W/ns.json

# 3. bake (git checkout of the pin for the file list)
python3 tools/build_maps.py --src $BR/sqlglot --ns $W/ns.json --out selector/maps \
    --trace $W/trace-fork1 --trace $W/trace-fork2 --trace $W/trace-seq1 --trace $W/trace-seq2

# 4. measure
python3 tools/eval_impact.py eval/cases/sample
```

`build_maps.py` prints the number of traced tests and any test that did not
pass on the clean pin (expected: 1,621 traced, 0 failing; the extra one is the
officially skipped `test_variadic_iif_execution`, which has no catalog number).
