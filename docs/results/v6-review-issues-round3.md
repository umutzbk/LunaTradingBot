# V6 implementation review — combined issue list, round 3

Both reviewers re-verified commit `eff9c71`. Their open findings are merged here, together with the
user's directives of 2026-10-12. The resolutions below are in the round-3 commit.

**Severity:**
- "CR" means the code reviewer gave it; "MR" means the methodology/conformance reviewer gave it.
- The user asked the reviewers to **re-assess, not carry over,** the severity of three items: the
  perp-only remainder (#4), valuation timing (#5) and a non-finite statistical result (#3).

## Issues

### 1. No allowed way to load the holdout-window model history during a forward look (Major, CR)

**Resolution:**
- The holdout download is pinned at the authorised opening: `guard.pin_manifest` writes the manifest
  sha256 next to both markers, exclusively, and only with the opening token.
- `holdout_data.load_holdout_inputs` reads only through that pin:
  - it is forward-guarded (T_F and the opening);
  - it requires the pin and verifies every file's sha256;
  - it is read only: no download, repair or write;
  - it needs no token.
- `forward.check_inputs_reach` turns a look whose history does not reach the holdout end into an
  INVALID LOOK (hard refusal), never a silent warm-up.

**Tests:**
- `test_holdout_inputs_after_the_opening_load_only_through_the_forward_guard`
- `test_verify_refuses_a_manifest_or_file_other_than_the_pinned_one`
- `test_pins_need_the_opening_token_and_both_markers_must_agree`
- `test_forward_look_refuses_inputs_that_stop_before_the_holdout_end`

### 2. Unknown funding made a historical/holdout run INVALID; the spec says this only for forward (Major, MR N1)

**Resolution:**
- Limited to `env == "forward"`, which is spec-literal (D-S2).
- Historically, unknown funding is counted and settled as 0, as in V5.

**Tests:**
- `test_unknown_funding_for_a_held_period_is_an_invalid_run` (asserts both directions)

### 3. A non-finite Newey–West p-value did not block an interim rejection (Minor CR m-b; re-assess)

**Resolution:**
- Fails closed: every NW p must be finite and ≤ the config's Holm α.

**Tests:**
- `test_a_non_finite_newey_west_p_blocks_the_interim_rejection`

### 4. A perp-only remainder after a one-legged exit; a zero-quantity pair retried forever (Minor CR m-d / MR N2; re-assess)

**Resolution:**
- A remainder leg is closed through its own leg only.
- `_reconcile` closes any pair whose legs differ by more than one step at the next fresh tick (D-H1).
- Partial pair fills are booked leg by leg.

**Tests:**
- `test_h1_closes_a_perp_only_remainder_through_the_perp_leg`
- `test_h1_reconciles_a_one_legged_pair_at_once`

### 5. The S-PIT10 lag also delayed valuation and fill-reference mids; funding visibility was not lagged (Minor CR m-e / MR N4; re-assess)

**Resolution:**
- The decision view uses t − 5 min: quotes, eligibility, stops, funding through `FundingPanel.delayed`, and H2a models built on the delayed panel.
- Equity marks, fill-reference mids and unwind valuation use t: `mark_mid` / `mark_spot_mid` (D-X9).

**Tests:**
- `test_pit10_lags_decision_quotes_but_not_marks_or_fill_reference_mids`
- `test_funding_delayed_view_shifts_visibility_only`
- `test_engines_value_positions_at_the_mark_view_not_the_lagged_decision_view`

### 6. `init_family` crash safety (Minor, CR m-h)

**Resolution:**
- The full state is written to a temp file and then linked into place: atomic, create-if-absent.
- The same family is idempotent; a different family is refused.

**Tests:**
- `test_family_file_is_never_left_empty`
- `test_family_is_fixed_once`

### 7. The opening marker was tied to the checkout path (Minor, CR m-f)

**Resolution:**
- Canonical run identity `RUN_ID = v6-<spec sha16>`.
- Markers in `~/.luna/v6_holdout/<RUN_ID>/` and `<git common dir>/luna-v6/<RUN_ID>/` (D-G1).

**Tests:**
- `test_marker_paths_do_not_depend_on_the_checkout_path`
- marker tests

### 8. A stale repair flag fired on a later, fresh book (Minor, CR m-a)

**Resolution:**
- The flag is cleared when nothing is held.

**Tests:**
- `test_a_repair_flag_left_by_a_full_exit_does_not_fire_on_the_next_build`

### 9. The Stage 0 audit covered spreads only (Minor CR m-c / MR D-A1)

**Resolution:**
- Every required family is covered: mark, trade, index, spot, funding and spreads.
- An empty or truncated download is FAIL; present shares are reported.
- The manifest must list every family for every asset.
- Provenance comes from the pinned manifest (D-A2).
- D-A1 strictness (any asset×hour coverage cell < 90 % = FAIL) goes to the user for ratification.

**Tests:**
- `test_stage0_audit_fails_a_truncated_bar_download`
- the earlier audit tests

### 10. The blind counters carried an equity signal (Minor, CR m-g)

**Resolution:**
- In `invariant_mode`, sizing (H3 build and repair, H1 cash) uses the initial capital, so no counter depends on realised P&L.

**Tests:**
- `test_blind_sizing_does_not_depend_on_realised_pnl`

### 11. The HoldoutAuthorization token can be forged through `_secret` (Minor, CR m1)

**Resolution:**
- Accepted limitation, as declared since V5: the guard is "not a security boundary".
- The reviewer is asked to confirm.

### 12. The FEAS confirmation coverage used days 1..d (Minor, MR N3)

**Resolution:**
- `coverage_share(cov, lo)` uses C's own window from day 91.

**Tests:**
- `test_feas_confirmation_coverage_uses_its_own_window`

### 13. MODEL WARMUP does not start the S-H3-12 clock (Minor, MR m8)

**Resolution:**
- Declared D-E11: the same 24 h bound applies through the missing-forecast exit.

### 14. Perp mid falls back to the trade bar, undeclared (Minor, MR m10)

**Resolution:**
- Declared D-X10.

### 15. M8 / D-E1: the V5 risk profile contradicts the H3 leg count

**Resolution:**
- Formal Erratum 001, `docs/results/v6-spec-erratum-001.md`, tag `v6-profitability-spec-erratum-001`.
- It gives an exact R0–R19 mapping.
- R5, R6, R8, R9 and R14 are now enforced, for H3 at the book level (`luna/v6/v5risk.py`).

**Tests:**
- `tests/v6/test_v5risk.py`

## Also re-checked (the user's directive)

**H3 side-ratio constants.** Each number belongs to its own metric:

| Number | Metric | Source |
|---|---|---|
| 1.5 | standalone side risk ratio q = σ($L)/σ($S) | `standalone_side_ratio_max` |
| [2/3, 1.5] | dollar ratio D_L/D_S | the 0.2 net-dollar cap: (1+0.2)/(1−0.2) |
| 2.25 | per-dollar basket vol ratio σ̃_L/σ̃_S | 1.5 × 1.5, the implied construction bound (spec note) |

Test: `test_the_2_25_bound_is_the_basket_vol_ratio_implied_by_q_max_1_5_and_the_dollar_cap`, with
cases at 2.20, 2.24, 2.26 and 2.30.

**New observation, spec-literal, not changed (D-B5).** When the dollar cap or the beta limit binds
exactly, rounding toward zero can push the book beyond the literal post-rounding check:
- the dollar check allows one max-step-notional;
- the beta check has no tolerance.

The book is then INFEASIBLE.

Tests:
- `test_with_the_cap_binding_exactly_rounding_toward_zero_can_fail_the_literal_dollar_check`
- `test_with_beta_scaled_exactly_to_the_limit_rounding_can_fail_the_literal_beta_check`

**Mutation equivalents.** One equivalent remains, with a code-path proof. The other former
"equivalent" was wrong and is now killed. See `docs/results/v6-mutation/equivalent-mutants.md`.
