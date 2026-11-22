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
| `equiv-tests.json.gz` | remaining sites (16 identity, 116 ordering) with the tests in whose call phase an observation disagreed or was not evaluable | the substitution keeps exactly those tests |

A site is absent from the per-test maps when any disagreement happened outside a test's
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
the plans cost 636.231 → 161.139 seconds.

Candidate 38 adds only candidate 37's static `JSON_PATH_PART_TRANSFORMS` narrowing. Paired
against candidate 35: 3840.847 → 3779.018 (−61.829), zero misses, sample byte-identical.
Full-suite oracle labels for 92 operator substitutions where the rules fire (36 at mapped
sites, 9 of them with 839 failing tests between them; 40 random swaps the rules
narrowed; 16 at newly certified identity sites): zero misses.

## Candidate 40: `and`/`or` swaps and `return X` -> `return None and X` (MBA + gmktech)

The observer also rewrites every two-operand `BoolOp` whose value is only truth-tested
(an `if`/`while`/`assert`/conditional-expression/comprehension test, a `not` operand,
or an operand of such a `BoolOp`; never in class bodies) and every `return X` whose `X`
is effect-free. Exactly one of `a and b` / `a or b` evaluates `b`, so an evaluation
agrees only when both operands' truthiness is known and equal **and `b` is effect-free**:
the swapped form either skips effects the original ran or runs effects it skipped. When
the original skipped `b`, the observer evaluates it (a raise means the swap diverges).
An evaluation whose `b` raised never reaches the observer's exit and is flushed as
divergent at the end of the test's call phase. `return X` agrees when `X` evaluates to
`None`; `X` is evaluated through a thunk so an `X` that raises (where the mutant would
return `None`) is recorded as divergent.

Effect-free means names, constants, attributes, subscripts, slices, unary/binary
operators, comparisons, `BoolOp`, conditional expressions, tuple/list/set displays, and
calls to `isinstance`, `issubclass`, `len`, `type`, `id`, `callable`, `bool` (when the
module never rebinds the name), `sqlglot.helper.is_int`/`seq_get`, the methods
`is_type`, `upper`, `lower`, `startswith`, `endswith`, `isdigit`, `isspace`, `text`,
`meta_get`, `find_ancestor`, and `<expr>.args.get(...)`. `Dialect.get` (lazy import),
`Schema.find`, `any`/`all` (may consume an iterator) and everything else are not.

The first MBA trace treated an evaluated `b` as agreeing whenever the truthiness matched,
so `if self._match(A) and self._match(B)` → `or` (same truthiness, one token fewer
consumed) could be certified; 50 of its 108 certified sites had a non-trivial `b`. It
also recorded nothing for a `return X` whose `X` raised. Both are fixed and covered by
`TestBoolAndReturnObserver`.

`tools/build_equiv_maps.py` now runs two traces in parallel and merges them
monotonically (a site is certified only if every run agreed and saw it; per-test lists
are unions). Identity/order lists are also unioned with the committed maps, which keeps
`test_variadic_iif_execution` on `generator-5130-68`: the scoring image's SQLite skips
that test, so a Docker trace no longer sees it. identity.json and order.json are
byte-identical to candidate 38.

| Map | Content |
|---|---|
| `bool.json` | 131 of 1,563 observed `and`/`or` sites that agreed on every evaluation |
| `ret.json` | 7 of 1,375 observed `return` sites that always returned `None` |
| `equiv-bool.json.gz` | 737 bool and 619 return sites with the tests in whose call phase an evaluation diverged |

`equiv-bool.json.gz` is separate from `equiv-tests.json.gz` (identity/order, 12 KB) so
identity and ordering edits never decode the larger lists. The rules apply only to
single-file, single-hunk, one-line edits whose ASTs are otherwise identical.

Validation (gmktech, two-CPU containers on cores 4-7/12-15, candidate 38 vs 40 run
simultaneously on separate physical cores): 138 oracle-labelled random swaps (98 `and`/`or`,
40 `return`; 117 with failing tests) plus the 92 labelled operator substitutions (`equiv`,
`randlab`, `newid`): 1144.081 → 1110.284 (−33.797), with no new misses. The two misses (`b036`, `b038`, both
`parser.py` condition analysis with identical selections) are pre-existing in candidate
38. The two random swaps whose oracle runs hang (`b044`, `b047`) get selections identical
to candidate 38. 709 labelled dev/holdout/revert/exotic/transfer/randops/sample patches:
zero misses; −10.911 after excluding three probe-timeout flips that re-ran identically
(`holdout__f129` 13.790 → 2.936). Sample output is byte-identical to candidate 38
(94.862, zero misses) with paired probe CPU unchanged.
