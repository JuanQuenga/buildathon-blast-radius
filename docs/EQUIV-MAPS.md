# Recorded operator equivalence (candidate 35, MBA)

Mechanical edits often swap `is`/`is not` for `==`/`!=`, or a strict ordering for its
non-strict twin (`<`↔`<=`, `>`↔`>=`). The impact model previously certified only five
`self.attr is X` sites, so such an edit in a widely shared function kept every covering
test (for example `trie.py:32`, 1,618 tests).

`tools/build_equiv_maps.py` runs the clean pinned suite once with
`tools/equiv_trace/sitecustomize.py`, which rewrites every single-operator `is`/`is not`
and ordering comparison in `sqlglot.*` into an observer. The observer computes the
original result exactly once and returns it. It evaluates the alternative operator only
when both operand types compare without side effects: builtin scalars and containers
against scalars, plain enums, `Expression`/`Dialect` equality against a foreign
non-string scalar, identity-equality objects and `Expression` structural equality
against each other (a type check, then a hash that sqlglot caches consistently and
invalidates up the parent chain on mutation), and exact builtin orderable types and
tuples of them. `Dialect` equality with a string is never evaluated because it can
import a dialect. Nested comparisons evaluated by the observer itself are not recorded.

| Map | Content | Selector use |
|---|---|---|
| `identity.json` | sites where both forms agreed on every observation (631 of 663 observed) | the single substitution selects nothing |
| `order.json` | ordering sites whose operands were never equal (60 of 262) | the single substitution selects nothing |
| `equiv-tests.json` | remaining sites (16 identity, 116 ordering) with the tests in whose call phase an observation disagreed or was not evaluable | the substitution keeps exactly those tests |

A site is absent from `equiv-tests.json` when any disagreement happened outside a test's
call phase (collection, fixtures, class setup, spawned executor workers) or inside a
module body (lazy dialect imports keep their state for later tests). The rules apply
only to single-file, single-hunk edits that replace one line with one line and whose
ASTs are identical apart from that operator. Runtime source readers are kept as before.
Repeated trace runs (random hash seeds and `PYTHONHASHSEED=0`) produced byte-identical maps.

Local paired runs against candidate 34 (same machine, two-CPU containers, 585 labelled
patches): zero misses, sample unchanged at 94.862, and the rules fired on ten patches
(`dev__4e9q` 14.3→0, `holdout__w7a2` 16.7→0, `holdout__vzb4` 12.1→0,
`holdout__d4ax` 16.1→2.1, `holdout__ancs` 8.8→7.7; the sample's two identity edits are
unchanged). Other differences came from CPU-limited probe timeouts on unrelated patches.

Paired simultaneous runs against candidate 35 (one physical core each, 585 labelled
patches): 3827.333 → 3775.353 simulated seconds (−51.980), zero misses, sample
unchanged at 94.862. On 120 random `is`/ordering swaps at uniformly chosen sqlglot sites
the plans cost 636.231 → 161.139 seconds. Oracle labels (full pinned suite) for 36
substitutions at mapped sites, 40 rule-fired random swaps and 16 newly certified
identity sites are in the candidate report.
