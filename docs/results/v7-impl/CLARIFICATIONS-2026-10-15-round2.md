# V7 post-freeze implementation clarifications — round 2 (2026-10-15)

| | |
|---|---|
| Frozen spec | `docs/superpowers/specs/2026-10-14-luna-v7-ml-research-design.md`, tag `v7-ml-spec-frozen` → commit `29959fc563c0fc58430744810095cefa1d63c9a8`. The tag and the spec are unchanged. |
| Branch | `feat/v7-ml-implementation` (decisions taken against the docket at HEAD `c5cd954`) |
| Decided by | the user, 2026-10-15 (explicit written decisions: "V7 IMPLEMENTATION — USER DECISION ROUND 2 + FIX ALL PRE-FREEZE BLOCKERS") |
| Inputs | `DECISION-DOCKET.md` (119 items), `implementation-redteam-r1.md`, `CLARIFICATIONS-2026-10-15-round1.md` |
| Scope | Explicit pre-code-freeze user decisions on every open docket item. None changes a frozen number. Where a decision selects one reading of the frozen text, that reading is binding for the implementation and for `v7-code-frozen`. |

## A. Normative docket items — APPROVED AS RECOMMENDED

For every docket item classified NORMATIVE AMENDMENT REQUIRED, the user approved the docket's recommended interpretation, with three explicit overrides (section B). The approved items are:

DK-01, DK-05, DK-06, DK-10, DK-16, DK-28, DK-29, DK-36, DK-37, DK-52, DK-54, DK-59, DK-62, DK-63, DK-64, DK-66, DK-67, DK-72, DK-75, DK-79, DK-84, DK-89, DK-94, DK-95, DK-96, DK-97, DK-99, DK-100.

Each takes the "Recommendation" text of its docket entry verbatim. Notes on the ones that require code or validation work:

| Item | Approved recommendation | Work it implies |
|---|---|---|
| DK-01 | No N2 amendment; N2 only for listings that fit one page; `IsTruncated=true` fails closed; daily files addressed as exact N1 URLs (+ `.CHECKSUM`) | truncation check in the S-1.7 fetch layer (SC-03) |
| DK-05 / DK-06 | Closed phrase list and sentence-scoped `scheduled_ts` mapping, no semantic inference; validate against the Stage-0 notice set before freeze, outcome-free | per-notice match / no-match report with reasons (validation step C4) |
| DK-10 | Labels keep τ; in the executed book a fill priced by the S-5.4 gap rule is booked at the substitute bar's open time, and B(e) is taken at that time | `taker_at` and B(e) at gap-rule fills (book) |
| DK-37 | With b clipped to 0, re-estimate a with b fixed at 0 | none (current code, `cv.py`) |
| All others | "Keep" the current code | none, beyond tests that pin the behaviour where missing |

## B. Explicit overrides

### OVERRIDE 1 — SC-04 Kraken exploratory path: IMPLEMENT BEFORE CODE FREEZE

- Do not defer or drop the Kraken exploratory path.
- Implement S-1.6 / S-2.4 / S-13.3 comparison #5 / L11 before `v7-code-frozen`, including its outcome / seal guard (the L11 "Kraken outcome guard").
- It remains exploratory ("exploratory (previously observed window)", S-1.6). It may never alter the primary S-13.2 / S-13.4 verdict beyond the frozen reporting semantics (it is one member of the S-13.3 Holm family of six, DK-83).

### OVERRIDE 2 — DK-69: required non-halted exit without a reference price

Replaces the docket recommendation ("keep the S-5.4 stressed wait"). For a required non-halted exit:
- search for the legitimate first executable 1-minute bar within the frozen 60-minute data-gap window (S-5.4 "use the first 1-minute bar within 60 min after τ");
- if none exists, the affected required evaluation path is **INVALID**;
- no price is invented;
- an exit is never carried into an arbitrarily later period;
- normal HALTED / terminal-event handling (S-5.4 halted wait, DEC-43 classes A / B / C, AMB-20) remains separate and unchanged.

Rationale (user): fail-closed; aligns book execution with the frozen data-gap principle.

Implementation reading (consistent with DK-10, approved): when the substitute bar exists, the exit fills at that bar's open price and is booked at that bar's open time. INVALID propagates under the existing rules (a required arm ⇒ INVALID RUN; a secondary arm ⇒ only its comparison is void).

### OVERRIDE 3 — DK-70: no stale carry of held-leg marks

Replaces the docket recommendation ("keep the stale carry"). At a required 15-minute mark (a leg or the hedge is held):
1. use the frozen mark-price source;
2. use only the already-authorized same-timestamp / current-period fallback chain (S-4.6: mark 15-minute close → last-price 15-minute close of the same period);
3. if no legitimate current price exists, the affected required path is **INVALID**.

No previous-event stale carry, no interpolation, no synthetic price. Rationale (user): stale marks could delay daily-loss, drawdown-kill and P3 behaviour.

Implementation reading: this is the AMB-21(b) treatment (round 1) extended to legs. A frozen HALTED / terminal-event rule that supplies its own handling for a leg at that tick still governs; where none does, the path is INVALID. INVALID propagates under the existing rules.

## C. Implementation clarifications (IC) — confirmed

The docket's blocker rule reads: "An item whose recommendation is the current code is resolved by the user's confirmation." The user's instruction approves the recommended interpretation of every open user-decision item. The controller reads that approval as covering the 72 IC items too (each awaited the user's confirmation), including:
- DK-51 (the R35 reading of AMB-16(e)) and DK-76 (AMB-28 details incl. R36 / R37), which the user had been asked to confirm;
- DK-39 (T_fit = the union of the purged T_tr and T_sel blocks, S-10.4 step 6 "each with the purge"; red-team M7 resolved by logging, not by a code change);
- the IC items whose recommendation changes code: DK-17 (break F2 / F4 lag and reversal pairs at an invalid record), DK-108 (instance switch at the first bar's `known_at`), DK-26 (automated K7 audit), DK-27 (L9 canary criteria).

This reading is controller ruling R38 and is flagged in the round-2 report for the user to veto.

## D. Code bugs — authorized

Every item classified CODE BUG — NO USER DECISION REQUIRED is authorized implementation work without further approval: SC-01, SC-02, SC-03, SC-05, SC-06, DK-09 (CRAN equality, external prerequisite if R is unavailable), DK-42 (A1 veto entry-only, red-team M8), DK-77, DK-78, DK-105, DK-106, DK-110, DK-111, DK-112, DK-113, DK-114, and the must-fix deferred minors of the red-team triage.

## E. Required validation before code-freeze review

1. all V7 tests; 2. the rest of the repository suite; 3. ruff / static consistency; 4. the AMB-05 notice matcher against the Stage-0 notice set (outcome-free); 5. the CRAN `bidask` 2.1.5 AR equality test (or an exact portable script, marked as the only external prerequisite); 6. an outcome-free real-scale `W_DEV` dry run (ingestion, PIT gates, universe, features, runner views, memory / runtime profile; no labels, training, IC, P&L or economic selection); 7. independent implementation red-team round 2.

## F. Hard boundaries (unchanged)

No `W_DEV` training, no real-data labels, no IC, no P&L, no economic selection, no `W_HOLD` access, no V6 masked outcomes, no code-freeze tag. The frozen spec and tag stay unchanged. An item that would require changing a frozen economic or model rule, rather than implementing a decided interpretation, is stopped and reported.

## Implementation status of these decisions

Filled in as each item lands (see `IMPLEMENTATION-STATUS.md`).

| Item | Code status | Tests |
|---|---|---|
| DK-10 (A, approved) | Done (Task 22a). Labels keep τ. Book: a gap-rule fill is booked at the substitute bar's open time and open price (`ExecView.ref_at`, `taker_at`; `forced_at` already did); E_M posts its limit at that time (`maker_post`, AMB-29 (d)); funding and B(e) follow the booked time. | `test_gap_rule_fill_booked_at_substitute_bar`, `test_maker_posts_at_gap_substitute_bar` (book/test_execution.py); `test_gap_fill_booked_at_substitute_bar_with_its_funding_and_b` (book/test_engine_round2.py) |
| Override 2 / DK-69 | Done (Task 22a). A required non-halted exit with no 1-minute bar within 60 min → `INVALID RUN — required exit without a reference bar within the 60-minute data-gap window` (detail `symbol#instance@ts`); the stressed wait stays for HALTED only; DEC-43 / AMB-20 / entries unchanged; drift and `risk_gross` reduces of a non-halted leg are required exits too (controller ruling R42, fix round 1; a halted reduce is skipped, S-5.4); the INVALID fires at the order's due time and is cancelled with the order (AMB-29 (g)); cessation inside the window → terminal rule (AMB-29 (c)). AMB-19 (d) RESOLVED. | `test_required_exit_without_bar_within_60_min_is_invalid` (book/test_execution.py); `test_required_decision_exit_without_bar_is_invalid`, `test_required_forced_flatten_without_bar_is_invalid`, `test_risk_gross_reduce_without_bar_is_invalid`, `test_em_drift_reduce_without_bar_is_invalid`, `test_halted_reduce_is_still_skipped`, `test_em_remainder_without_bar_invalidates_at_its_due_time_unless_replaced` (book/test_engine_round2.py); propagation: `test_em_runs_are_secondary`, `test_holdout_invalid_required_arm` (test_verdict.py), `test_selection_invalid_if_any_required_invalid` (test_selection.py) |
| Override 3 / DK-70 | Done for non-halted legs (Task 22a): mark → same-period last close → `INVALID RUN — required leg mark missing` (detail `symbol#instance@tick`); no stale carry. **HALTED sub-case STOPPED**: no frozen rule supplies the mark and INVALID would follow on every such halt tick → AMB-29 (a), normative amendment required (option (i) would match the AMB-21 (b) hedge rule); current code (stale carry) kept, marked. A ceased, not yet halted leg is INVALID (fix round 1). AMB-19 (e) RESOLVED except that sub-case. | `test_leg_without_mark_at_tick_is_invalid`, `test_halted_leg_without_mark_keeps_current_code_amb29`, `test_ceased_not_halted_leg_without_mark_is_invalid` (book/test_engine_round2.py); propagation: test_verdict.py, test_selection.py |
| DK-42 / M8 | Done (Task 22a). A1 `passes_hurdle` = A0's `s_rule > 0`; separate entry-only `vetoed` flag; the constructor never exits on the veto. AMB-13 (c) RESOLVED. | `test_a0_a1_order_and_veto` (test_arms.py); `test_a1_veto_is_entry_only` (book/test_constructor.py); `test_a1_veto_never_exits_a_held_leg` (book/test_engine_round2.py) |
| DK-77 / m7 | Done (Task 22a). An exit counts as a resting maker order only inside [post, post + 30 min) of a limit actually posted (`Position.maker_resting`, AMB-29 (e)). | `test_maker_remainder_in_halted_wait_is_not_reexited` (book/test_engine_round2.py) |
| DK-78 | Done (Task 22a). The one-sided flatten and the kill cancel in-flight hedge-open tokens (`integrity` `hedge_open_cancelled_by_*`). | `test_one_sided_flatten_cancels_inflight_hedge_open`, `test_kill_cancels_inflight_hedge_open` (book/test_engine_round2.py) |
| DK-17 (C, IC) | Done (Task 22b). F2 lag-1 and F4 reversal pairs are two adjacent records of the PIT sequence, both valid: an invalid (dropped) record breaks the pair, as in the TS AR(1) reference; windows and the F4 denominator (valid records) unchanged. AMB-08 updated. | `test_f2_f4_pairs_break_at_an_invalid_record` (test_round2_minors.py) |
| DK-108 (C, IC) | Done (Task 22b). `current_instance` = latest instance with `first_bar_ts + 15 min <= t - 1 min`; every PIT caller uses it; notice versions are attributed by `started_instance` (literal start, so none is dropped) and `U6State.active` is gated at the same known time. AMB-31. | `test_instance_becomes_current_at_first_bar_known_at` (test_round2_minors.py), `test_current_instance_before_end_known` (data/test_instances.py), `test_version_in_first_16_min_binds_to_new_instance_used_once_current` (data/test_notices.py) |
| DK-39 (C, IC) | No code change (Task 22b). T_fit = purged T_tr ∪ purged T_sel (S-10.4 step 6 "each with the purge"); red-team M7 resolved by logging. AMB-30. | `test_t_fit_excludes_tr_rows_purged_at_the_sel_start` (test_cv.py) |
| DK-53 / DK-109 (C, IC) | Done (Task 22b). AMB-16 (e) logs the general case (the proportional `risk_gross` / `cap_gross_entry` cuts preserve an existing L/S imbalance) and the hedge outside the S-7.3 gross limit (S-7.4 caps it). New reporting-only diagnostic: decision note `non_neutral_post`, `BookRun.non_neutral_decisions`, verdict `details["non_neutral_decisions"]`. | `test_non_neutral_post_book_is_reported` (book/test_constructor.py), `test_non_neutral_decisions_are_counted` (test_round2_minors.py) |
| DK-106 / m1 (D) | Done (Task 22b). `ns_array` window-checks integer inputs (signed, unsigned, nullable) with the 2017-2100 window; `ns_array([1_700_000_000_000])` raises. One test moved its tiny integers into the window. AMB-32 (a). | `test_ns_array_window_checks_integers` (test_round2_minors.py), `test_ns_array_accepts_valid` (test_foundations.py) |
| DK-110 / m2 (D) | Done (Task 22b). `to_ns` refuses floats (Python and numpy); no caller passes floats. AMB-32 (a). | `test_to_ns_refuses_floats` (test_round2_minors.py) |
| DK-111 / m3 (D) | Done (Task 22b). The keyed groups of `constants.FROZEN` are `MappingProxyType`; everything else is a scalar, tuple or frozen dataclass; mutation raises; copying / iteration still work. AMB-32 (b). | `test_frozen_constants_are_immutable` (test_round2_minors.py) |
| DK-112 / m8 (D) | Done (Task 22b). An accounting `ValueError` on a required run -> `INVALID RUN — K10 / accounting integrity failure` with the error text as detail, in `selection.choose` and `verdict.holdout`, never raised; a secondary run voids only its comparison (unchanged). AMB-32 (c). | `test_selection_accounting_error_is_invalid_k10`, `test_verdict_accounting_error_is_invalid_k10` (test_round2_minors.py) |
| DK-113 / m9 (D) | Done (Task 22b). Every engine table (decisions incl. `killed`, fills, marks, positions, funding flows, daily) carries its schema columns when empty; `holdout` returns `INVALID RUN` (integrity) for a hand-built decision log without `killed` instead of raising. AMB-32 (d). | `test_empty_engine_frames_carry_the_schema_columns`, `test_holdout_returns_a_class_for_a_malformed_decision_log` (test_round2_minors.py); `test_holdout_negative_wiring_p2_p3_p5_p9_p11` (test_verdict.py) |
| DK-114 / m10 (D) | Done (Task 22b). `BookRun.research` is set by `runner.run_book` (hence every `dev_selection` book); `run_record` records `research = flag or any(run.research)` and refuses it from a dirty tree; `holdout_inputs` refuses non-research runs. AMB-32 (e). | `test_research_runs_always_record_as_research` (test_round2_minors.py), `test_dt4_run_record`, `test_holdout_inputs_require_authorization` (test_runner.py) |
| Deferred 19b minor (D) | Done (Task 22b). `holdout_inputs` refuses a final model whose classifier map lacks ML*'s classifier (A2 `logit_en`, A3 `lgb_clf`) or that has only one of the reg / clf maps. AMB-32 (f). | `test_holdout_inputs_require_authorization` (test_runner.py) |
| Deferred 17-18 minor / DK-97 note (D) | Logged (Task 22b), reporting only: in AMB-23 (j), an unfilled (halted) L-265 reduce counts as constructible (D1 optimism), and `d1_share_scaled` ignores the liquidity cap. No code change. | none (log only) |
| SC-05 / M4, m4 (D) | Done (Task 24). `authorize_holdout` / `preflight` require every DEC-19 prerequisite: code freeze (tags, HEAD == `v7-code-frozen`, clean tree, frozen spec), a persisted pre-opening record re-verified at HEAD in the opening process (`opening.load_pre_opening`: hash-verified DevSelection with a valid S-10.6 choice and an empty S-10.7 kill, a passed LeakageReport re-derived from its content, linked by ML*, fold-model and label-panel hashes, produced at HEAD; I-1, M-5), the W_HOLD file manifest to pin, the phrase, one opening only. The opening writes two markers and two pins (code commit, manifest, DevSelection / LeakageReport / record shas, ML*, fold / final-model hashes, W_DEV ML* run sha). `HoldoutAuthorization` has no public key (closure); `check_outcome_window` re-checks both markers and both pins (`verify_authorization`) for every W_HOLD window. `holdout_inputs` is bound to the pin (report, DevSelection or W_DEV ML* run, fold models on every path) and to the audited final model (`model_hash(final_model) == report.final_model_hash`, I-2). `verdict.holdout` requires the holdout report to cover exactly its four runs (M-1); a holdout check over zero decisions is not complete (M-3). Not a cryptographic boundary. AMB-34 (g)-(l). | `test_forged_authorization_is_refused`, `test_authorization_without_markers_or_pin_is_refused`, `test_opening_happy_path_pins_the_dev_selection_and_the_report`, `test_cross_process_reverification`, `test_preflight_requires_every_prerequisite`, `test_missing_preconditions_are_refused`, `test_writer_refuses_what_the_opening_would_refuse`, `test_persisted_record_semantics_are_rechecked` (11 cases), `test_explained_k7_trigger_is_accepted`, `test_tampered_or_rehashed_records_are_refused`, `test_dev_issuer_is_only_used_by_dev_selection` (test_opening.py); `test_pin_manifest_once` and the updated guard tests (test_guard.py); `test_holdout_inputs_require_authorization` (test_runner.py, I-2 / M-4); `test_holdout_report_must_cover_exactly_the_runs`, `test_holdout_report_must_link_to_the_pre_report` (test_verdict.py, M-1 / M-3 / M-4); `test_holdout_report_complete_needs_checked_decisions`, `test_covers_exactly`, `test_persisted_report_round_trip` (test_leakage.py) |
| SC-06 / M5 (D) | Done (Task 24). One canonical, hash-checked source (`luna.v7.release`): the R_V6 release record (S-1.2 basis a / b / c, evidence and V6-holdout documents by sha256, record hash) and the forward review-pass record (T_F7), each a committed JSON read through git from an annotated tag; absent or not verifying = sealed. Shared by `windows.m6` / `m6_cached` (end = R_V6), `forward.l7` / `require_l7` / `forward_evaluate` (no R_V6 / T_F7 / now argument; `ForwardInputs` lost them), `ForwardStore.read` / `iter_records` (no time or L7 argument; open only at the canonical L7 on the trusted wall clock `release.trusted_now_ns`), `netguard.partition_m6_keys` / `drop_m6_keys` (no caller span) and the outcome / engine guard (M6 lifted only on both clocks; W_FWD7 refused before L7, the extension window before the extended L7). AMB-34 (a)-(f); AMB-24 (d) superseded. | `test_without_a_record_m6_is_open_and_l7_undefined`, `test_canonical_record_ends_m6_and_derives_l7`, `test_early_written_release_and_extension`, `test_forward_l7_cannot_be_fed_a_guessed_r_v6`, `test_store_sealed_regardless_of_arguments` (M5 probe), `test_unverified_release_record_is_absent` (7 cases), `test_unverified_review_record_is_absent`, `test_netguard_takes_no_caller_span` (M5 probe), `test_outcome_guard_releases_m6_only_by_record_and_trusted_clock`, `test_w_fwd7_replay_runnable_only_at_l7` (test_release.py); updated test_forward.py, test_forward_store.py, test_netguard.py |
