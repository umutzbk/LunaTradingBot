# V6 implementation review — combined issue list, round 4

Both reviewers reviewed exact HEAD `84e5ed6`. Each verdict was FAIL with Critical 0 / Major 2; one
Major was shared, so there are 3 distinct Majors. The methodology reviewer stated **"INTERPRETATION FIX,
NOT OUTCOME TUNING"** for Erratum 001; the condition is that the code implements it exactly (E1).

## Majors

### 1. Forward coverage averaged the snapshot share instead of counting evaluable days (code M-A, methodology E2)

This was a regression from round 3.

**Resolution:**
- `forward.coverage_share` takes the share of evaluable days (each day ≥ 90 % fresh) inside the
  window from `lo`.
- The test that had pinned the wrong mean is corrected.
- A test with every day 85 % fresh expects 0.

**Tests:**
- `test_coverage_is_the_share_of_evaluable_days_not_the_mean_snapshot_share`
- `test_feas_confirmation_coverage_uses_its_own_window`

### 2. The R6/R8 checks did not follow V5 (methodology E1)

**Resolution:**
- `v5risk.violations` returns nothing unless the decision opens or grows a leg, as in V5, where
  R5–R9 and R14 apply only to opens.
- R6 adds up V5 `risk_amount`: 0.15 × held qty × entry price, scaled down on a reduction, plus
  0.15 × notional for opened quantity (`H3Engine._leg_risk`; H1 uses the pair's entry prices).
- R14 vetoes an open once 30 orders were already sent before it, in send order.
- Any non-finite input fails closed. This also covers code N3, a NaN mid passing as truthy.

**Tests:**
- `test_closes_and_reduces_are_exempt_from_r5_to_r9_and_r14_as_in_v5`
- `test_r6_open_risk_of_held_legs_is_measured_at_the_entry_price`
- `test_each_retained_rule_at_its_v5_boundary`
- `test_non_finite_notional_or_risk_fails_closed`

### 3. The single holdout download was not restart-safe (code M-B)

**Resolution:**
- Every fetch retries with exponential backoff (`_retry`; it also catches rate-limit errors that
  arrive as an HTTP 200 body).
- Spot `/Trades` paging is throttled.
- Parts are staged: tmp file → atomic replace → sha256 sidecar. A complete part is never fetched or
  written again, so `download` can be called again in the same authorised process to resume.
- Only `manifest.json` (and the pins) are write-once.

**Tests:**
- `test_holdout_download_resumes_after_a_transient_failure_without_refetching`
- `test_transient_rate_limit_is_retried`
- `test_parts_are_complete_only_with_a_matching_sidecar_hash`

## Minors

| # | Source | Issue | Resolution | Test |
|---|---|---|---|---|
| 4 | Code N1 | Pins not all-or-nothing | Rolled back like the markers | `test_pins_are_written_all_or_nothing` |
| 5 | Code N2 | One asset reaching the end satisfied the reach check | Every asset's funding and prices must reach HOLDOUT_END −1 h, else INVALID LOOK (names the assets) | `test_forward_look_refuses_when_any_asset_stops_before_the_holdout_end` |
| 6 | Code N3 | NaN mid could pass the V5 check silently | Finite fallback, then fail closed | see Major 2 |
| 7 | Methodology minor 1 | R14 sequencing | Now as V5 (orders before each open) | see Major 2 |
| 8 | Methodology minor 2 | R12 wording | The reviewer's mm = 0 note is recorded in the freeze record; the erratum file stays immutable | — |
| 9 | Methodology minors 3, 4 | D-A1 strictness and D-B5 rounding | Go to the user for sign-off / awareness | — |

**Severity re-assessment the user asked for** (code reviewer, round 3 of the re-review): the
perp-only remainder (#4), valuation timing (#5) and the non-finite NW p (#3) are each **Minor**, with
reasons given. All three are fixed.

**m1 (token forgeability):** the code reviewer agrees it is an accepted limitation, since the guard is
not a security boundary.
