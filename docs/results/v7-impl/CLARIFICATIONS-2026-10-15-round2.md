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
