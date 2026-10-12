# V6 mutation testing: surviving mutants

Mutation sets are in `scripts/v6_mutation/*.json` and are run with `scripts/v6_mutation/mutate.py`.
Each mutant is applied alone, its tests are run, and the source is restored.

## Survived and proven equivalent: 1

### `h2.json` "running period visible" (`luna/v6/data.py`, `FundingPanel._visible`)

- **Original:** `ended = (self._t[a:b] + HOUR.value) <= t`
- **Mutant:** `ended = (self._t[a:b]) <= t`

**Claim:** for every call that can happen, `ended` is True for every period in the slice. Therefore
the mutant (which only weakens `ended`) gives identical results.

**Proof.** `_visible(lo, hi, t)` takes the periods p with lo ≤ p ≤ hi. It has exactly two callers in
the code base (`grep -n "_visible" luna/`):

1. `fl(t, hours, min_known)` calls `_visible(t − hours·1h, t − 1h, t)`. Every p ≤ t − 1h, so
   p + 1h ≤ t, and `ended` holds.
2. `label(s, h, t)` raises unless s + h·1h ≤ t, then calls `_visible(s, s + (h−1)·1h, t)`. Every
   p ≤ s + (h−1)·1h, so p + 1h ≤ s + h·1h ≤ t, and `ended` holds.

`newest_end` and `abs_rate` do not use `_visible`. Each has its own explicit bound:
- `newest_end` only considers periods p ≤ t − 1h;
- `abs_rate` returns NaN if p + 1h > t; the test `test_funding_panel` pins this.

So no input reaches `_visible` with a running period, and no test can tell the two versions apart.
The check stays as defence in depth: a future caller with hi > t − 1h would still be safe.

## Previously listed as equivalent, now killed: 1

### `h1_fwd_guard.json` "H1 collateral ignored" (`luna/v6/h1.py`, S-H1-2 cash rule)

The earlier argument was that sizing already caps q·px ≤ free/(2 + cost), so the explicit check
`need > free` could never fire. **That was wrong.**

The quantity is floored with a float tolerance: `q = floor(n/px/step + 1e-9)·step`. When n/px/step is
within 1e-9 below an integer, q rounds **up** to that integer. need = q·px·(2+cost) can then exceed
free by up to about 1e-9·step·px·(2+cost), which is more than the check's 1e-9 USD tolerance whenever
step·px is large.

The test `tests/v6/test_conformance_fixes.py::test_h1_cash_rule_refuses_a_pair_whose_floor_tolerance_rounds_q_above_the_cash`
builds that case:
- price 1000, step 1, zero costs;
- free = 6000 − 1e-6, so n/px = 2.9999999995 and q = 3;
- need = 6000 > free.

The original code refuses the pair (`skipped_cash`); the mutant sends it. The mutant is now killed.

## Deliberately not mutated: overlapping guards (round 4)

Both halves of each pair below are kept on purpose. Pair 2 is observationally equivalent under single
mutation. Pair 1 is **not strictly equivalent**; the correction (code reviewer, m-3) is given there.

1. **`holdout_data.download`: the early `manifest.json exists → FileExistsError` check, and the
   exclusive `O_EXCL` create of `manifest.json`.**
   - Remove `O_EXCL`: the early check raises first, so the outcome is the same, except for the race
     between two processes, which only `O_EXCL` closes.
   - Remove the early check: **not equivalent.** On a second call after the manifest exists, a part
     whose bytes no longer match its sidecar (deleted or altered) would be refetched and rewritten
     before the `O_EXCL` create raises. The kept early check prevents any write after the download
     completed, which is why it stays. The final `FileExistsError` is the same either way, so the
     existing tests do not distinguish the two.
   - Covered by `test_holdout_download_resumes_after_a_transient_failure_without_refetching` (third call).
2. **`engine._decide`: the V5 price fallback (`insts` mid, else the last valuation mid), and
   `v5risk.violations` failing closed on any non-finite input.**
   - A NaN that slipped through would make the decision INFEASIBLE "V5 inputs not finite" rather
     than pass silently.
   - The fail-closed half is mutated and killed (`round4.json` "V5 NaN passes").
