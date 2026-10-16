# SDD ledger — plan: docs/superpowers/plans/2026-10-14-luna-v7-ml-implementation.md

Spec: docs/superpowers/specs/2026-10-14-luna-v7-ml-research-design.md (tag v7-ml-spec-frozen, 29959fc). Branch feat/v7-ml-implementation.
Plan commit: see git log (docs(v7): implementation plan).

## Preflight scan
| Rows | Produces vs consumes / self-consistency | Finding |
|---|---|---|
| T1 self | constants/spec/windows/folds; tests match interfaces | OK; V6 looks via day1(T_F)+day·D (T_F already midnight → 2027-01-12/04-12/10-14) |
| T1→T19 | T19 calls spec.read_frozen_spec() | MISSING in T1 interfaces → Ruling R1 |
| T1→T2 | windows.W_HOLD, m6, spec.verify_spec | OK |
| T2 self | authorize_holdout needs tag v7-code-frozen which doesn't exist | OK: tests use temp git repos/monkeypatch |
| T2→T10/T14 | guard.check_outcome_window(lo,hi,*,auth,now_ns) | OK |
| T3→T4/T5 | R3 frame open_ts ns + volume; funding frame columns | OK |
| T4→T5,T7,T8,T9,T10 | Instance, current_instance, instance_of | OK |
| T5→T8,T9,T10 | annotate() columns instance,nominal_h,reconciled,interval_transition,invalid,r8 | OK |
| T6→T8,T10,T14 | U6State.active, terminal.classify | OK |
| T7→T8..T14 | halted, P, first_executable_after, half_spread, beta_hat | OK; plan writes ĥ=max(2·Ŝ/2,1bp) = λ=2 literal |
| T8→T9,T14 | gates()/GateRow | OK |
| T9→T12 | FEATURES, Preprocessor | OK |
| T10→T12 | LabelResult | samples structure for cv undefined → Ruling R2 |
| T11→T12,T13 | Config, fit/refit, search_space | OK; AMB-02 α→C |
| T12→T13,T16 | FoldModel, date_ic | OK |
| T13→T14 | a0_score, a1_keep, ml_score | OK; AMB-03 FL30 units |
| T14→T15,T16,T17,T18 | BookRun | OK; T14 large → standard/most-capable model |
| T15→T16 | Ledger, k10_ok, net_trim, stressed | OK |
| T16 self | selection.choose validity incl A0×3 (EC-2) | OK |
| T17 self | L-norm path rejects Filters | OK |
| T18 self | EC-11 | OK |
| T19 self | parses spec blob | depends on R1 |

Ruling R1: T1 additionally produces `spec.read_frozen_spec(repo=None) -> str` (the frozen blob text via git show tag:path, hash-checked) — T19 needs it; cost if wrong: trivial.
Ruling R2: cv `samples` = pandas DataFrame with columns decision_ts, symbol, side, h, label_known_at, y_reg, y_pos, plus FEATURES columns; one row per (decision, symbol, side) for a given h — cost if wrong: an adapter in T12/T19.

## Progress
Task 1: dispatched (BASE afda650, implementer sonnet)
Ruling R3: per-task gate = tests/v7 + ruff; the whole-repo suite (slow pre-existing tests/forward collector tests, >20 min on this host) runs once at Task 20 in the background — cost if wrong: a cross-module regression found late
Task 1: implementer stopped after duplicate hand-backs (background full-suite attempts); review in progress
Task 1: complete (commits afda650..58ed6c8, review clean)
Task 1: minor (deferred): constants dict fields mutable inside frozen dataclasses (use MappingProxyType)
Task 1: minor (deferred): timeutil.to_ns accepts floats (bypasses unit guard)
Task 1: minor (deferred): windows.overlaps returns np.bool_/inf compare
Task 1: minor (deferred): spec._git no timeout / FileNotFoundError not SpecMismatch; verify_spec tag/hash-mismatch branches untested
Task 2: dispatched (BASE 58ed6c8, implementer sonnet)
Ruling R4: AMB numbers named in the plan (AMB-01 CRAN, AMB-02 alpha->C, AMB-03 FL30 units) are placeholders; implementers take the next free number in AMBIGUITIES.md — cost if wrong: none (cross-refs by title)
Ruling R5: Task 2 reviewer Minors 1 (drop_m6_keys does git I/O by default) and 2 (unparseable key/period fails open) are upgraded to Important — the brief requires a pure netguard and a seal guard must fail closed — cost if wrong: one extra fix round
Task 2: minor (deferred): HoldoutAuthorization._secret public; check_outcome_window doesn't re-check markers
Task 2: minor (deferred): Memento-Datetime naive tz handling; forward_store sha check weak; index append no fsync/lock; extract_transport_id flat list / silent [] on unknown shape; no explicit no-I/O test
Task 2: fix round 1/5 (5 addressed, 0 open; commits 3b27aa5..7651732)
Task 2: complete (commits 58ed6c8..7651732, review clean)
Task 2: minor (deferred): regex anchoring not isolated by tests (control-char check fires first); directory key without trailing slash dropped
Task 2: AMB entries: catalog JSON shape (impl clarification); N2 paging `marker` refused (normative amendment required)
Task 3: dispatched (BASE 7651732, implementer sonnet)
Task 3: review → 1 Important (REST markPrice dropped on overlap); fix round 1 sent
Task 3: minor (deferred): missing-symbol-column KeyError; normalise bar_ns default fragile for R5; NaN false conflict in union; R2 column names differ from later interval annotation (integration in Task 5)
Task 3: fix round 1/5 (2 addressed, 0 open; commits a2a5d0f..82876cf)
Task 3: complete (commits 7651732..82876cf, review clean)
Task 3: minor (deferred): merge() docstring omits mark_source; mark_source not in R2 TableSpec; raises test lacks match=
Ruling R6: Tasks 4 and 5 dispatched to ONE implementer (two commits) and reviewed together — small, tightly coupled modules (T5 consumes T4) — cost if wrong: a larger review surface
Ruling R7: funding records outside any instance: reconciled=False, invalid=False, r8=NaN, in_instance=False (never used) — documented as AMB impl clarification — cost if wrong: a flag rename
Tasks 4-5: dispatched (BASE 82876cf, implementer sonnet)
Tasks 4-5: review → 1 Important (u3_gap TypeError on pd.NA)
Ruling R8: Tasks 4-5 reviewer Minor 1 (untested per-symbol path, silent missing symbol, asserts) and Minor 3 (unlogged interpretations incl. membership ts < last_trade_ts) enter the fix round — fail-closed and the no-unlogged-interpretation global constraint — cost if wrong: one extra round
Tasks 4-5: fix round 1 sent
Tasks 4-5: fix round 1/5 (4 addressed, 0 open; commits b3f2cd4..e18c10e)
Ruling R9: explicit interval 4 h with 8 h spacing = previous nominal → reconciled, nominal 4, r8 = rate·8/4 stays: S-2.6 says the explicit field controls and reconciliation accepts spacing = previous nominal; it is spec-literal, not invented. Surface it in the final report as a spec observation (possible amendment) — cost if wrong: r8 doubled on such records (rare; regime-change records)
Tasks 4-5: complete (commits 82876cf..e18c10e, review clean)
Tasks 4-5: minor (deferred): empty multi-symbol annotate returns no annotation columns; annotate/assign lack the retrospective-membership (not as-of) caveat
Task 6: dispatched (BASE e18c10e, implementer sonnet)
Task 6: review → 3 Important (version union per S-2.5; classify with known_at None; R12 row not instance-bound)
Ruling R10: Task 6 Minors "seconds dropped in scheduled time" and "underscore as token boundary" enter the fix round (both change which instance/time a notice maps to — DEC-37/DEC-40 exactness) — cost if wrong: one extra round
Task 6: minor (deferred): USDⓈ-M context is notice-level not symbol-sentence-level (spec wording is notice-level); halted_fn cache keyed by closure; classify single-notice (postponed new time only in postponement → C) undocumented
Task 6: fix round 1 sent
Task 6: fix round 1/5 (6 addressed, 0 open; commits 8af270e..f8ce719)
Task 6: complete (commits e18c10e..f8ce719, review clean)
Task 6: minor (deferred): _SYM_RE in "respectively" lists treats `_` as boundary; stale test comment in test_each_version_evaluated_by_itself
Task 7: dispatched (BASE f8ce719, implementer sonnet)
Task 7: review Approved (0 C / 0 I / 6 minor)
Ruling R11: Task 7 minors "NaN close as fill price", "β̂ ignores known_at (forward leakage)" and "P(τ) no fallback on NaN 1m open" enter a fix round — invariants (no invented price, L1 PIT) — cost if wrong: one extra round
Task 7: fix round 1 sent
Task 7: fix round 1/5 (2 addressed, 1 open — beta known_at opt-in fail-open; commits 46ee8c5..3ba1c96); fix round 2 sent
Task 7: fix round 2/5 (4 addressed, 1 new open — NaN known_at cast to INT64_MIN passes PIT filter; commits 3ba1c96..c139517); fix round 3 sent
Task 7: fix round 3/5 (3 addressed, 1 new open — finite out-of-range known_at cast to INT64_MIN; commits c139517..729ad44); round 4 dispatched to a fresh implementer (opus) with a shared ns validator
Task 7: fix round 4/5 (1 addressed, 0 open; commits 729ad44..9425b47)
Task 7: parked — ns_array does not window-check INTEGER inputs (INT64_MIN / ms ints pass the PIT filter) — Ruling: real but non-blocking: production paths come through klines.to_ns_checked, which applies the 2017–2100 window; flagged for the final fix wave (window-check ints too) — cost if wrong: a hand-built frame with garbage int known_at passes PIT
Task 7: complete (commits f8ce719..9425b47, 1 parked)
Task 8: dispatched (BASE 9425b47, implementer sonnet); common-context gained a 'Lessons' section
Task 8: review → 3 Important (U7 invalid last record; bars1m known_at default; retrospective in_instance for U3/U7)
Task 8: minor (deferred): max_known_at omits instance provenance / U5 slice approximation; U6State rebuilt per call (perf); future-row tests only for some gates
Task 8: fix round 1 sent
Task 8: fix round 1/5 (4 addressed, 0 open; commits 44de11c..74abea1)
Task 8: complete (commits 9425b47..74abea1, review clean)
Task 8: minor (deferred): gates() re-annotates full PIT funding history per call (perf); no test "all PIT funding invalid"; no future-dated bars1m/notice gate test
Task 9: dispatched (BASE 74abea1, implementer opus — judgment-heavy §9 formulas)
Task 9: review → 1 Important (AMB-08(a) side adjustment misclassified)
Ruling R12: keep the literal §9 reading (only carry-direction features × −s; F10 momentum and F11 BTC return not side-adjusted) and reclassify it as "normative amendment required — user decision" in its own AMB entry — the spec text literally limits the adjustment to carry-direction features; changing it is a methodology choice reserved to the user — cost if wrong: linear models cannot use directional price features until the user rules
Task 9: minor (deferred): F12 rank for non-eligible candidate untested; winsorization-collapse edge untested; perf (ĥ recomputed 22×/call)
Task 9: fix round 1 sent
Task 9: fix round 1/5 (4 addressed, 0 open; commits e272c83..f74ae29)
Task 9: complete (commits 74abea1..f74ae29, review clean)
Task 9: minor (deferred): AMB-09 option (B) wording muddled; AMB-08 list lettering gap
Task 10: dispatched (BASE f74ae29, implementer opus)
Task 10: implemented ccc8e54 (AMB-10); review dispatched (opus)
Task 10: review → 1 Important (AMB-10 b/g invalid causes misclassified) + minors
Ruling R13: btc_exit_halted and beta_missing kept as conservative interim code, reclassified "normative amendment required — user decision" — they add invalid causes outside S-5.5 — cost if wrong: a few more invalid labels (likely zero in W_DEV)
Ruling R14: Task 10 minors (non-discriminating half-spread tests, settle_ts None crash, per-label git call perf) enter the fix round — named-risk test gap, fail-closed, real-scale feasibility — cost if wrong: one extra round
Task 10: fix round 1 sent
Task 10: fix round 1/5 (4 addressed + 1 new open — caller-supplied m6_span fail-open; commits ccc8e54..7d080e5); fix round 2 sent (canonical cached M6 span verified in guard)
Task 10: fix round 2/5 (1 addressed, 0 open; commits 7d080e5..aad189a)
Task 10: complete (commits f74ae29..aad189a, review clean)
Task 10: minor (deferred, final wave): netguard.partition_m6_keys/drop_m6_keys take an unverified caller span (pure by design) — callers must pass windows.m6_cached(); consider a cross-check helper. Latent: a test patching windows.m6 before the first m6_cached call would poison the process cache.
Ruling R15: Tasks 11 and 12 dispatched to one implementer (two commits), reviewed together — T12 consumes T11 directly — cost if wrong: larger review surface
Tasks 11-12: dispatched (BASE aad189a, implementer sonnet)
Tasks 11-12: review → 1 Critical (cross-family selection) + 4 Important (final T_sel selection; search space not enforced; AMB-12 classification; L9a canary)
Ruling R16: side pooling in date-level IC and the b=0 intercept re-estimate stay as interim code, flagged "normative amendment required — user decision" — spec silent; both affect selection/entries — cost if wrong: different selected hyperparameters / entry rule until the user rules
Ruling R17: canonical sample ordering + duplicate rejection enters the fix round (S-17 deterministic ordering) — cost if wrong: none
Tasks 11-12: minor (deferred): purged tr→sel rows also excluded from T_fit (undocumented); selection test doesn't recompute IC; convergence not recorded; Config.family str not Literal; uv.lock revision 5→3 churn
Tasks 11-12: fix round 1 sent
Tasks 11-12: fix round 1/5 (6 addressed, 0 open; commits 2da52a0..0a11268)
Tasks 11-12: complete (commits aad189a..0a11268, review clean)
Tasks 11-12: minor (deferred): select_hparams doesn't enforce canonical samples/idx pairing; final_model refuses (not drops) late label_known_at rows — callers pre-filter
Task 13: dispatched (BASE 0a11268, implementer sonnet)
Task 13: review Approved (0 C / 0 I / 5 minor)
Ruling R18: Task 13 minor "TS EWMA/AR(1) filter on funding_ts not known_at, not current-instance" enters a fix round — L1 applies to every model/reference — cost if wrong: none
Task 13: minor (deferred): tautological piece in frozen-config test; a0_score param shadows cost_side
Handoff to Task 14: A1/ML rows with passes_hurdle False stay in the ranking — constructor must apply the hurdle/veto per the spec (A1 = A0's entries minus vetoed: decide/record whether a vetoed slot is backfilled — log AMB)
Task 13: fix round 1 sent
Task 13: fix round 1/5 (2 addressed, 0 open; commits 0b00cbb..ff14bfc)
Task 13: complete (commits 0a11268..ff14bfc, review clean)
Task 13: minor (deferred): AMB-14 "(reference only, no test)" stale; AR(1) late-known test uses inequality not equality; garbled comment in test_ewma_half_life
Ruling R19: the Task 13 implementer ran `ruff format` over 27 committed luna/v7 + tests/v7 files, leaving uncommitted, unreviewed formatting churn; verified AST-identical and reverted with git checkout (no logic lost) — cost if wrong: none
Ruling R20: Task 14 split into 14a (constructor + risk + hedge: pure decision logic) and 14b (execution + engine event loop), separate dispatches and reviews — size — cost if wrong: an interface adjustment between 14a and 14b
Task 14a: dispatched (BASE ff14bfc, implementer opus)
Task 14a: review → 1 Important (U8-false held leg exited via rank path)
Ruling R21: a held leg unscored solely because U8 is false gets no rank/hurdle exit (S-7.3 "U8 doesn't trigger an exit"); flagged normative with the alternative scoring pool — cost if wrong: ranks of others would differ under the alternative
Ruling R22: S-7.3 caps (gross, per leg, liquidity) implemented as HARD risk limits on the marked book (reduce-only), overriding the 25 % drift band; flagged normative — conservative reading of conflicting literal rules — cost if wrong: extra resize trades
Ruling R23: AMB-16(d) (empty side ⇒ flat even inside min hold) is a PRE-RUN BLOCKER for any W_DEV book run until the user decides
Task 14a: fix round 1 sent (incl. tick continuity, trigger_ts on forced exits, asserts→raise, AMB doc updates)
Task 14a: fix round 1/5 (6 addressed, 0 open; commits 6d93da6..5736594)
Task 14a: complete (commits ff14bfc..5736594, review clean)
Task 14a: minor (carry to 14b): gross-cap with fixed > gmax gives reduce-to-0 (14b must treat reduce-to-0 as close); AMB-17(f) ineligibility exit fill rule (S-4.2 forced vs t+15 min) is 14b's to decide/log
Task 14b: dispatched (BASE 5736594, implementer opus)
Task 14b: review → 3 Important (unwind data gap invalidates run unlogged; AMB-19 economic items misclassified; hard-cap ruling kills drift rule)
Ruling R24 (supersedes R22): S-7.3 caps bind targets/entries only; the marked book drifts within the 25 % band — the hard-cap reading makes S-7.1's drift rule dead and costs ~1–3 %/yr turnover; flagged normative, PRE-RUN BLOCKER with AMB-16(d) — cost if wrong: marked book may exceed caps by up to 25 %
Ruling R25: constructor uses halted flags evaluated at t (PIT), not at t+15 min; execution applies the halted check at t_e — removes 15 min look-ahead — cost if wrong: none
Ruling R26: unwind with a missing (non-halted) final-minute bar stays INVALID (conservative) but is logged normative + pre-run blocker
Task 14b: fix round 1 sent
Task 14b: fix round 1/5 (5 addressed, 0 open; commits d8fea8e..bc8f1d3)
Task 14b: complete (commits 5736594..bc8f1d3, review clean)
Task 14b: minor (final wave): maker_exit set too broadly (E_M-only risk/cooldown effects; narrow to a resting maker order); stale AMB-19(d) sentence; AMB-19(j) stale; cap_gross_entry can leave neutrality band (doc in AMB-16(e)); order_cancelled_at_unwind logged at schedule time; latent exit-token over-fill if reduce + live exit token
Task 15: dispatched (BASE bc8f1d3, implementer opus)
Task 15: review → 3 Important (missing B unclassified run failure; K10 lacks PR=−Σdq·p check; AMB-21(a) per-instance assets misstated)
Ruling R27: per-asset attribution N_a (P5) keyed by SYMBOL (conservative, consistent with constructor's "asset"); per-instance is the recorded alternative — cost if wrong: P5 concentration slightly stricter
Ruling R28: missing BTC B(e) stays fail-closed, flagged normative PRE-RUN BLOCKER
Task 15: fix round 1 sent
Task 15: fix round 1/5 (4 addressed, 0 open; commits 81c5dee..e9b194d)
Task 15: complete (commits bc8f1d3..e9b194d, review clean)
Handoff to Task 16: use accounting.k10_ok (incl. q_breaks) and daily_ok → S-13.4 integrity (INVALID RUN); P5/§12 per-asset via per_asset_net/per_asset_rho (symbol keys); P6/P10/S-10.7 use mean_daily_net; S-13.1 uses daily returns r_t.
Task 16: dispatched (BASE e9b194d, implementer opus)
Task 16: implemented be2822c (AMB-22); review dispatched (opus)
Task 16: review → 2 Important (E_M treated as required arm; K8/leakage/integrity inputs fail open)
Ruling R29: E_M runs are secondary (void only their comparison); if P1(E_T) fails and E_M unavailable → NO-GO with flag maker_split_undecidable; flagged normative — cost if wrong: maker-dependent vs no-edge label undecided in that corner
Ruling R30: K8/leakage/integrity/dev_kill required inputs; verdict thresholds K8 at 0.5 % itself; holdout() refuses when dev_kill non-empty
Task 16: fix round 1 sent
Task 16: fix round 1/5 (3 addressed, 0 open; commits be2822c..3d8e11b)
Task 16: complete (commits e9b194d..3d8e11b, review clean)
Task 16: minor (deferred): empty decisions frame may lack `killed` column → holdout raises (check engine _frame); _check_inputs hard-raises on mislabelled E_M runs; K8 type check rejects np.float32
Ruling R31: Tasks 17 and 18 dispatched to one implementer (two commits) and reviewed together — moderate, both consume BookRun/verdict — cost if wrong: larger review
Tasks 17-18: dispatched (BASE 3d8e11b, implementer opus)
Tasks 17-18: review → 1 Important (extended-look thresholds unlogged) + minors
Ruling R32: minors entering the round: EC-11 arm matching, terminal_events required, structured A0 terminal time, D1 path-independent share reported, DEC-34 missing-symbol reclassified normative — fail-closed / user-decision routing — cost if wrong: one extra round
Tasks 17-18: fix round 1 sent
Tasks 17-18: fix round 1/5 (5 addressed, 0 open; commits 28e0525..92b22aa)
Tasks 17-18: complete (commits 3d8e11b..92b22aa, review clean)
Tasks 17-18: minor (deferred): unfilled reduce counts as constructible (D1 optimism); d1_share_scaled ignores liquidity cap; d1_share_scaled needs post/prices columns
Task 19: dispatched (BASE 92b22aa, implementer opus)
Ruling R33 (plan defect): no module builds the engine's DecisionView (gates/ĥ/β/qv24/candidate features+scores) or adapts per-quarter fold models / drives the S-10.6 concatenated outer-quarter book — add Task 19b 'runner' before Task 20 (built and tested on synthetic data only; never executed on research data) — cost if wrong: one extra task
Task 19: review → 3 Important (dead FROZEN constants; assert-nothing rows; e2e lacks verdict stage)
Ruling R34: e2e verdict stage + E_M runs + multiplier scaling moved into Task 19b (runner) — the runner builds HoldoutInputs — cost if wrong: none (tracked in 19b brief)
Task 19: fix round 1 sent (items 1, 2 + verbatim spread reference + NOT_CODED completeness)
Task 19: fix round 1/5 (3 addressed, 0 open; commits 3889fa2..03be9d3)
Task 19: complete (commits 92b22aa..03be9d3, review clean)
Task 19: minor (deferred): ml_hurdle units (bps) confusing if nonzero; isotonic clip literal in cv.py; HOURS_PER_YEAR literal (F9)
Task 19b: dispatched (BASE 03be9d3, implementer opus)
USER DECISIONS round 1 (2026-10-15): AMB-16(d) approved (flatten whole book; min hold doesn't block); AMB-16(e) approved (target caps; marked book under hard risk limits; drift rule reachable); AMB-20 approved (INVALID); AMB-21(b) approved (invalid only when BTC required; fail closed). Runner = pure orchestrator + 8 required tests (sent to 19b). Docket of all remaining normative items required before code freeze; unresolved outcome-changing items = code-freeze blockers.
Ruling R35: AMB-16(e) implementation reading — marked-book hard limit = gross ≤ 1.0·E at decisions (reduce-only, proportional); per-leg and liquidity caps stay target/entry caps with the 25 % drift rule (a continuous per-leg marked cap would make the drift rule unreachable, which the decision forbids) — listed in the docket for the user to confirm — cost if wrong: per-leg marked exposure can exceed 0.5E/K by < 25 %
Plan: after 19b → Task 19c "clarification round 1 code" (AMB-16(e) gross hard limit; AMB-21(b) BTC-required INVALID in engine; tests) → commit CLARIFICATIONS doc → Task 20 (suite, red-team, DECISION-DOCKET.md). No READY FOR CODE-FREEZE REVIEW while normative items open.
Task 19b: implemented 95f6387 (AMB-25..27); clarifications doc committed 29ca7f1; review dispatched (opus)
Task 19b: review → 4 Important (unbounded bar cache; DT4 schema mismatch; one-sided book carried until next decision; notices default fails open)
Ruling R36: the AMB-16(d) "next executable opportunity" timing (flatten at the next tick/S-4.2 opportunity when a side empties, vs next decision) moves to Task 19c (engine change); 19b logs the interim reading as normative AMB
Task 19b: fix round 1 sent (cache, DT4, required notices/terminals, minors)
Task 19b: fix round 1/5 (5 addressed, 0 open; commits 29ca7f1..d2812d5)
Task 19b: complete (commits 03be9d3..d2812d5, review clean)
Task 19b: minor (deferred): run_record research flag is caller convention; holdout_inputs doesn't check classifier family; LRU never hit on sequential replay (perf); OVERNIGHT_PROGRESS.md untracked makes tree dirty for code_state
Task 19c: dispatched (BASE d2812d5, implementer opus)
Task 19c: implemented 6c2ec02 (555 passed); review dispatched (opus)
Task 19c: review → 2 Important (hedge not flattened with the book; E_M maker-first flatten not "next executable opportunity")
Ruling R37: "entire ML book" includes the S-7.4 book-level BTC hedge (spec §0/S-7.4 make it part of the book; kill already exits it) → one-sided flatten exits the hedge too at S-4.2 timing; empty-side flatten is forced/taker under E_M — literal reading of the user decision — cost if wrong: hedge closed hours earlier
Task 19c: fix round 1 sent
Task 19c: fix round 1/5 (3 addressed, 0 open; commits 6c2ec02..b18ea86)
Task 19c: complete (commits d2812d5..b18ea86, review clean)
Task 19c: minor (deferred): conditional detail assertion in test_em_runs_are_secondary; in-flight hedge-open token not cancelled by flatten (very unlikely); 14-min asymmetry documented (AMB-28 c'')
Clarifications status table updated: 2dc7344
Task 20: v7 suite 558 passed at 2dc7344, ruff clean; non-v7 repo suite running in background (free RAM ~0.4 GB); whole-branch implementation red-team (opus) and DECISION-DOCKET builder (opus) dispatched in parallel
Task 20: whole-repo suite (non-v7) 977 passed, 2 skipped, 3 deselected (network) in 580 s at 2dc7344; with v7 558 → 1535 passed total
Task 20 red-team r1: C0 / M8 / m11 — M1 real-scale infeasible (memory/time), M2 L9 canaries/K7/leakage report missing (verdict fail-open on leakage), M3 fetch layer/loaders/K9/E6 W_HOLD path/W_HOLD feature guard/Kraken missing, M4 DEC-19 preconditions + guard accepts forged auth, M5 caller-supplied L7/M6 spans unseal store/N2, M6 AMB-05 misclassified, M7 T_fit purge divergence, M8 A1 veto as exit. Not acceptable for code-freeze review even after docket. Docket builder sent the additions.
Task 20: docket (119 items: 72 IC / 31 normative / 16 bug; 117 blockers) + red-team r1 + status committed c5cd954. STOP: report to user; awaiting decisions.
USER DECISIONS round 2 (2026-10-15): all 28 NAR approved as recommended; overrides SC-04 (implement Kraken before freeze), DK-69 (first executable 1m bar within 60 min data-gap window else INVALID path; no stressed wait), DK-70 (no stale leg marks: mark -> same-period last close -> INVALID path). All BUG items authorized (SC-01..06 + minors). Validation C1-C7 incl. outcome-free real-scale W_DEV dry run (user-authorized reading of var/v7/stage0 W_DEV data and Binance public downloads, guarded), notice validation, CRAN script, red-team r2. Still no training/labels/IC/P&L/selection/W_HOLD/V6/tag.
Task 21: decisions recorded 3970358 (CLARIFICATIONS-2026-10-15-round2.md; docket status table; AMBIGUITIES status line)
Ruling R38: the user's approval of "every open NAR / user-decision item" covers the 72 IC items (docket blocker rule: IC resolved by user confirmation) incl. DK-39 (keep union of purged blocks; M7 resolved by AMB log), DK-51, DK-76 — flagged in report — cost if wrong: user re-opens individual IC items
Ruling R39: DK-105 (forward replay + forward refit) is authorized BUG work; implemented before red-team r2 (Task 28) although the docket marks it a forward-stage (not W_DEV/W_HOLD) blocker — reduces post-tag code — cost if wrong: one extra task
Ruling R40: round-2 task order — 22a book decisions (DK-10/69/70/42/77/78), 22b small bugs/minors (DK-17/108/39 log/106/110-114/m6/neutrality log), 23 SC-02 leakage, 24 SC-05+SC-06 seals (consume LeakageReport), 25 SC-03 fetch/K9/E6/W_HOLD guard (+DK-01), 26 SC-01 scale + read-only loaders + budget tests, 27 SC-04 Kraken, 28 DK-105 forward, 29 validation (notice report, CRAN script, dry run — controller), 30 red-team r2 — dependency order (leakage report before authorization; loaders with storage) — cost if wrong: rework across tasks
Task 22a: dispatched (BASE 3970358, implementer opus)
Ruling R41: add Task 26c (metadata ingestion: R11 notices from N5/N6 captures with closed-list kind, R1 instruments/E8 identity, R12 empty) — no frozen code builds NoticeVersions/R1 rows; needed for the dry run, the DK-05 validation and E6 at the opening — kind closed list extends approved DK-05 approach, ratified via the validation report — cost if wrong: one extra task
Task 22a: implemented 46bb931 (571 passed); concerns AMB-29(a) halted-leg mark stopped (normative), (b) partial risk_gross reduce w/o price skipped
Ruling R42: a required risk_gross / partial reduce of a non-halted leg is a 'required exit' under override 2 (fail closed: no bar within 60 min -> INVALID), since skipping it lets the marked book exceed the frozen hard gross limit, which AMB-16(e) forbids — cost if wrong: extra INVALID runs in rare gaps
DK-09: CRAN equality run with real R 23/23 (7a8b452); conda env removed; pagefile growth reduced C: free to ~3 GB
Task 22a: review → 1 Critical (required reduce skipped, contra R42) + 5 minors; fix round 1 sent
Task 22a: fix round 1 committed 46f7c4f (576 passed)
Task 22a: fix round 1/5 (6 addressed, 0 open; 46bb931..46f7c4f)
Task 22a: complete (commits 3970358..46f7c4f, review clean)
OPEN USER ITEM: AMB-29(a) halted-leg mark without any 15m close (spec silent; current = stale carry for halted legs only; option (i) INVALID matches AMB-21(b) hedge rule)
Task 22b: dispatched (BASE 46f7c4f, implementer opus)
Task 22b: implemented ce9f8ad (590 passed); review dispatched
Task 22b: review Approved (C0/I0) + 5 minors; hardening round 1 sent for minors 1-3 (accounting ValueError, ns guards, deploy research flag)
Task 22b: hardening round 1 committed 46137ef (607 passed, controller re-ran gate: 607 passed, ruff clean)
Ruling R43: float64 timestamps inside the 2017-2100 window stay accepted (ingest emits int64 ns; float64 only from derived inputs) — refusing would need every caller audited — cost if wrong: sub-microsecond rounding on a float path
Task 22b: complete (commits 46f7c4f..46137ef, review clean)
Task 23: dispatched (BASE 46137ef, implementer opus)
Disk: 24.7 GB free after reboot (pagefile 28 GB -> 16 GB); dry-run storage budget ~10 GB OK
Task 23: implemented 13c531e (629 passed); review dispatched
Ruling R44: K7 evaluated set includes ML*'s A1 classifier as well as the regressor (conservative; K7 trigger only forces an audit) — cost if wrong: an extra audit trigger
Task 23: review Approved (C0); I-1 cross-process re-verification, I-2 final-model binding, M-1/3/4/5 carried into Task 24 brief
Task 23: complete (commits 46137ef..13c531e, review clean)
Task 24: dispatched (BASE 13c531e, implementer opus)
Task 24: implemented 163b657 (671 passed); concerns: single-process opening no resume (AMB-34k), W_HOLD guard reads marker files per call (perf → 26b); review dispatched
Task 24: review → 1 Important seal bypass (marker/pin paths from auth record; probe opened W_HOLD with forged auth + tmp files), I-2 resume, minors m1-m6; fix round 1 sent
Ruling R45: resuming the SAME opening (unchanged pin, same RUN_ID markers, full preflight, no verdict-produced marker, distinct resume phrase, logged RESUME files) is not a second opening under DEC-19 — implemented before freeze because post-tag code cannot change and the host has a reboot incident — flagged for user — cost if wrong: user removes the resume path before tagging
USER (2026-10-15): R45 APPROVED as implementation clarification — immutable opening_id bound to code commit/tag, manifest, DevSelection, LeakageReport, DEC-19 preconditions, opening ts; resume only same opening_id, no verdict, unchanged bound state, full preflight, canonical paths, distinct resume phrase; all attempts (incl. refused) append-only logged; 8 named adversarial tests. Forwarded to Task 24 implementer.
Task 24: fix round 1 committed 27cdabc (691 passed); re-review dispatched
Task 24: re-review of 27cdabc → 3 Important (I-A cache not location-bound; I-B caller repo/home trusted → relocated resume after verdict; I-C verdict.holdout pure → verdict without marker); fix round 2 sent
Task 24: fix round 2 committed f4a327c (697 passed); re-review dispatched
Task 24: re-review of f4a327c APPROVED (I-A/I-B/I-C closed, probes P1-P6 refused); round 3 sent for minors m-a..m-d
Task 24: round 3 committed 6716c5c (702 passed)
Task 24: complete (commits 13c531e..6716c5c, adversarial re-review approved)
Task 24: open for red-team r2: windows.m6 / forward.l7 / ForwardStore still accept caller repo (forged annotated tag needed); W_DEV books not persisted for a later opening process (re-run must match pinned hash)
Task 25: dispatched (BASE 6716c5c, implementer opus)
Task 25: implemented 488839a (768 passed); review dispatched
Ruling R46: after the legitimate opening (hardened auth), N3 fundingRate endTime bound is raised to < 2026-07-01 (W_HOLD end), symmetric with N1 W_HOLD content and E6 N5/N6 bound; M6 still refused — the S-1.7 table is explicitly pre-opening and W_HOLD funding must be built by the same S-2.6 REST ∪ archive rule as W_DEV — flagged for user — cost if wrong: W_HOLD funding gaps unfillable (more invalid labels/settlements)
Task 25: review → 3 Important (R46 not applied; notices/terminals/insts bypass W_HOLD guard; K9 unconfigured = unlogged) + minors; fix round 1 sent. Note for red-team r2: docs/results/v7-stage0/scripts make direct network calls (historical Stage-0; not luna/v7)
Task 25: fix round 1 committed df8e298 (784 passed); re-review dispatched
Task 25: re-review Approved (all I/m addressed); n-1..n-3 carried into Task 26a brief
Task 25: complete (commits 6716c5c..df8e298, review clean)
Task 26a: dispatched (BASE df8e298, implementer opus)
Task 26a: implemented c8b27a7 (814 passed); concerns: 1m store 33 B/row uncompressed (~20-25 GB full) over budget; R4 daily stopped; funding month-boundary fail-closed; pickle BookRun persistence
Ruling R47: R4 (mark/index/premium 15m) is ingested from monthly files only — DEC-33's monthly ∪ daily union names R3/R5; monthly files cover W_DEV to 2025-06 inclusive and partial delisting months; a missing R4 bar follows the existing gap/fallback chain — cost if wrong: a few R4 bars missing at month tails
Task 26a: review → 3 Important (FundingLoader refuses window ending at W_HOLD[0]; resumed ingest leaves new symbol silently empty; layout 47 B/row → scaled-int delta ~20 B/row) + minors; fix round 1 sent
Ruling R48: funding archive rows outside their file's month are re-partitioned by event time and compared under the S-2.6 key/±60 s rule; rows >= the active N1 bound are dropped in memory and logged; per-chunk ingest failures are ledgered and non-fatal — data handling only — cost if wrong: rare duplicate handling differs
Task 26a: fix round 1 committed a25459f (833 passed, 20.2 B/row); re-review dispatched
Task 26a: re-review → Important: restricted unpickler bypass (code exec via pandas dotted names); Minor -0.0, opt-in pin; fix round 2 sent (replace pickle with canonical parquet+JSON)
Task 26a: fix round 2 committed b0fbd3a (839 passed, no pickle); re-review dispatched
Task 26a: round-2 re-review Approved (no executable deserialization; pin mandatory); 4 minors carried to 26b
Task 26a: complete (commits df8e298..b0fbd3a, review clean)
Task 26b: dispatched (BASE b0fbd3a, implementer opus)
Task 26b: implemented 8e8b6d7+607140a; controller gate 866 passed, ruff clean; extrapolation W_DEV 6-12 h, ~2.2-2.5 GB; review dispatched
Task 26b: review → 4 Important (U6 fallback cache unbounded; view-store key incomplete/unverified, no computed code id; S-10.6 doesn't use shared universe by default; ≤3 GB not RSS-measured) + minors; equivalence verified verbatim; fix round 1 sent
Task 26b: fix round 1 ce0c0ee; controller gate 877 passed, ruff clean; bench 40 symbols: pass peak RSS 412 MB, 7.5 ms per symbol-decision, extrap 5.2 h pass + ≤6.4 h replays; 120-symbol bench running
Bench (synthetic) two-point RSS fit: pass 40 sym 412 MB, 120 sym 771 MB → ~4.5 MB/sym + ~232 MB → 500 sym ≈ 2.48 GB (520 ≈ 2.57 GB) < 3 GB; replay 288→492 MB → 500 sym ≈ 1.5 GB; time pass ≈ 7.5 h single process + replays ≤ 5.9 h
Task 26b: re-review Approved (I1-I4, m1-m6 fixed); 2 minors (anchor lag check, dep versions in view key) carried into 26c
Task 26b: complete (commits b0fbd3a..ce0c0ee, review clean)
Task 26c: dispatched (BASE ce0c0ee, implementer opus)
Task 26c: implemented d154d01+4f2efd6+c246e9e (focused tests only, low RAM); AMB-40(a) STOPPED normative: USD-M exchangeInfo has no multiplier field, quoteAsset not on E8 list → literal reading = empty universe; also (d) ended instances need a source dated within the instance → USER DECISION needed before dry run; review dispatched
USER (2026-10-15) AMB-40: multiplier from baseAsset closed prefix list; quoteAsset added to identity whitelist; (d) literal within-instance (report exclusion counts in dry run). Recorded 9f9229c
Task 26c: review → 3 Important (kind default 'delist' misclassifies postponement/cancellation wordings; whole-page match text wider than 'title or body'; per-symbol identity beyond R1) + minors; fix round 1 sent with AMB-40 user decisions
USER (2026-10-15) FUTURE (after code freeze AND successful W_DEV only): deploy V7 paper/forward monitoring runtime to the DO VPS if benchmarked profile fits — live=false, no real orders, separate Docker service, reuse collector data without changing provenance, persistent volume, healthcheck, auto-restart, bounded RAM/CPU, heartbeat, all seal/W_HOLD/M6 guards kept, never disturb collector; benchmark first; if VPS too small, report required size instead. No full historical training/backtest on VPS unless profiling supports it. NOT part of current round.
Task 26c: fix round 1 9399556 (focused tests, low RAM); Ruling R49: multiplier-like prefix = leading digit run that is a power of ten >= 10 (or 1M); closed list 1000/1000000/1M gives factor, other powers excluded, non-power digit runs (1INCH) factor 1 — keeps legit digit-led coins — cost if wrong: a non-multiplier power-of-ten-led name excluded
Task 26c: R49 19c180b; controller full gate 952 passed, ruff clean; re-review dispatched
Task 26c: re-review → N1 Important (same-code catalog entry makes real notice article_conflict); m-a R49 1M edge; updat/suspend over-veto → C4/DK-05 ratification item (user); round 2 sent
Task 26c: round 2 2efa33d; controller full gate 955 passed, ruff clean; N1 fix matches reviewer's spec (accepted without further re-review)
Task 26c: complete (commits ce0c0ee..2efa33d)
OPEN USER ITEM (DK-05 ratification at C4): modifier stems updat/suspend leave 'Update: … Will Delist' / 'Suspend … and Delist' unclassified (fails open on exposure)
Task 27: dispatched (BASE 2efa33d, implementer opus)
USER (2026-10-15) DECIDED — never ask again: AMB-29(a) INVALID if a halted held leg needs a mark and no legitimate current mark/fallback exists (no stale carry) → code change after Task 27 (Task 27b); R46 approved as implemented (canonical W_HOLD auth, M6 sealed); R38 confirmed (72 IC approved unless red-team finds a genuine contradiction). DK-05: finish the outcome-free Stage-0 notice validation first, show counts/examples for 'Update: … Will Delist' and 'Suspend … and Delist', then apply the narrowest deterministic rule justified by evidence. Continue 27 → 28 → validations → red-team r2 without routine updates.
Task 27: implemented 2a39756 (focused tests only); AMB-41 STOPPED normative: S-2.4 gates on Kraken data, Kraken ML* book/labels/IC (no 1m bars, pro-rata funding, no quote volume), #5 pairing window → comparison #5 UNAVAILABLE (m=6 kept) → final-report USER item; review dispatched
Task 27b: dispatched (BASE 2a39756, AMB-29(a) INVALID; parallel with read-only Kraken review and C4/C6 validation agents)
Task 27: review Approved (C0/I0); AMB-41 reclass: (a)(b)(e)+F8 normative (user item), (d)(f) IC, U2 implementable; minors 1-8 → fix round after 27b commits (verdict.py overlap)
Task 27b: d78a1c1 (focused 100 passed); halted leg w/o mark → INVALID_MARK; B/C via bc_terminal_due; edge: class-A settlement tick w/o close → INVALID_MARK (W_DEV A=0)
Task 27: minor fix round 1 sent
C4 (first run): FROZEN code processes 0/54 real captures (no releaseDate on article object — catalog releaseDate / article publishDate; bodies are JSON node-trees). Diag: 2/74 Stage-0 terminations matched with usable scheduled_ts; misses: base-asset-only naming (DEC-40 whole-token), token swaps, no capture. DK-05: updat vetoes 28/47 catalog titles ('Update the Leverage & Margin Tiers'); 17 instances' only evidence vetoed; OMG 'Postponed' word order missing.
Rulings R50 (releaseDate = catalog releaseDate else article publishDate, later if both), R51 (node-tree body text extraction), R52 (DK-05 narrowest: drop 'update … leverage & margin tiers' spans when updat is the only modifier; 'postponed'+'delist' → postponement) — evidence in C4 — cost if wrong: notice kinds differ on few titles
NORMATIVE USER ITEM (final report): DEC-40 whole-token symbol vs base-asset-only notices (AKRO/DODO/ANC/ICP/TLM/LUNA/BLZ/SC/1000BTTC/YFII…), token-swap notices; W_DEV U6 coverage of B/C terminations very low → INVALID RUN risk
Task 26d: brief written; dispatch after Task 27 fix round commits (AMBIGUITIES.md overlap)
C6 phase A: STOPPED by disk rule (free 4.0 GB; pagefile grew to 23.8 GB under memory pressure — not the ingest). Sample R3 BTCUSDT 93,504 rows 26.1 B/row; frozen ingest handled real files OK; K9 only deliberate probes. Perf findings: HttpxTransport opens a new client/TLS per GET (~0.29 s/req); DEC-33 daily files for every month = 97% of requests (~859k req, ~15 h per table at 8 threads).
Ruling R53: fetch daily files only for months whose monthly file is missing or incomplete (bar count < expected); union then equals the frozen DEC-33 union except that monthly/daily conflict detection is skipped where daily adds no rows — flagged — cost if wrong: an undetected monthly/daily conflict on complete months
Perf bug: Fetcher transport must reuse one pooled client (no semantic change) → Task 26e (with R53)
Host: free RAM ~0.6 GB, pagefile 24 GB; reboot recommended before C6 phase B
Ruling R53 WITHDRAWN: DEC-33 literal 'union of monthly and daily archive files' + conflict reporting requires reading daily files for every month; keep literal and fix throughput instead (pooled client, more workers) — est. ~4 h per 15m/1m table
Task 27: fix round 1 8b222d8; controller full gate 978 passed (incl. 27b), ruff clean
Task 27: complete (commits 2efa33d..8b222d8, review approved); Task 27b complete (d78a1c1)
NORMATIVE USER ITEM (final report): AMB-41 (a) 1m bars on Kraken, (b) funding convention (candidate V6 S-DATA-1/V5 FundingLedger), (e) 'V5 gate values where they differ' scope, F8 trade counts → comparison #5 UNAVAILABLE until decided
Task 26d: dispatched (BASE 8b222d8, implementer opus; R50-R52 + pooled transport + C4 re-run)
Task 26d: 7e1e40e+8fb87fa; controller full gate 1000 passed, ruff clean; C4 after fix: 54/54 usable; 6/74 Stage-0 terminations matched w/ scheduled_ts, 2 null, 1 OMG postponed, 65 missed (no capture 40, DEC-40 base-only 36, unclassified 13); AMB-42 normative (base-only 22 instances, token swaps, mixed-notice U6 for OMG/XEM); review dispatched
Task 28: dispatched (BASE 8fb87fa, implementer opus) in parallel with read-only 26d review
Task 26d: review → Important: R52(b) ignores word order (delisting titles with 'Previously Postponed' → postponement, fails open); minors (mixed-notice note, unknown node shapes, cookie jar, C4 provenance, Fetcher close); fix round 1 sent
Task 26d: fix round 1 af83882+e506e9c, C4 re-run 81f76c7 (counts unchanged); fix matches reviewer's spec (accepted); full gate after Task 28 lands
Task 26d: complete (commits 8b222d8..81f76c7)
Note: scripts/v7/dryrun_ingest.py must use 'with Fetcher.default(...)' before C6 phase B
Task 28: 1579daf; controller full gate 1029 passed, ruff clean; AMB-43(d) stale-quarter tick no-op STOPPED normative (forward-stage); (e) forward-model hash record + store loader not built (forward-stage); review dispatched
Task 28: review → Important: refit only before verdict marker (AMB-43b) but no write-once forward-model record / samples binding → selection channel + permanent-loss risk; contradicts DK-105 blocker status
Ruling R54: write-once forward-model record (opening_id, model hash, samples sha256, code commit, config) next to opening markers; samples must equal pinned W_DEV panels + W_HOLD panels issued under the same auth; load only via verified record; record built in the W_HOLD freeze — cost if wrong: one extra mechanism
NORMATIVE USER ITEM (forward stage, final report): AMB-43(d) stale-quarter tick 'no-op for risk' vs AMB-29(a) INVALID on missing mark (reviewer: §15 coverage logic favours 'skip')
Task 28: fix round 1 sent
Disk: user-approved cleanup (conda clean --all = 9.1 GB, HuggingFace cache deleted ~19 GB) → C: free 32.8 GB
Task 28: fix round 1 4dcd854 (R54 built)
Task 28: controller gate at 4dcd854: 1 failed (test_no_module_unpickles — forward_model pickle.loads) / 1039 passed; round 2 sent: canonical non-executable model format
C6 phase A resumed (disk 32.8 GB free; pooled transport); parallel with Task 28 round 2
Task 28: round 2 bafe5c1 (modelcodec, no pickle)
Task 28: re-review of bafe5c1 (gate 1046 passed) → Important: W_HOLD refit panels not bound to canonical grid/window/universe (one-shot outcome-informed selection); round 3 sent
Task 28: round 3 d77adda; controller full gate 1052 passed, ruff clean; matches reviewer fix (a) — accepted
Task 28: complete (commits 81f76c7..d77adda); open forward-stage items: AMB-43(d) normative (user), AMB-43(e) part 2 store→EngineInputs loader (forward-stage review)
Next: wait C6 phase A; then C6 phase B (gates/universe/features/views profiling); then red-team r2
Red-team r2: dispatched on d77adda (opus, read-only) in parallel with C6 phase A
Red-team r2: C0/M5/m6 committed a92923e — NOT READY. MJ1 outcome-side scale (1m resident), MJ2 ingest gaps silent, MJ3 verdict inputs unbound, MJ4 manifest missing, MJ5 caller repo forward/M6
Ruling R55: ingest-completeness gate — DEC-33 conflicts and unresolved ingest gaps block every research entry point (fail closed, literal blocker); re-ingest resolves append-only
Ruling R56: DEC-19 pinned manifest = canonical JSON of W_HOLD N1 listings (key,size,published checksum) for all datasets/symbols ingested at opening, built via single-page N2; opening requires its content-verified sha; post-opening W_HOLD ingest must match it
Tasks 29b (MJ3/MJ5/minors) and 29c (MJ4/MJ2) dispatched in parallel (briefs written just after dispatch; agents notified); 29a (MJ1) after 29b
USER ITEMS for decision now: AMB-42 (U6 coverage; W_DEV INVALID RUN more likely than not), AMB-41 (#5 unavailable), AMB-43(d) (forward stale tick), L9a false-failure / seed policy
USER (2026-10-16): A-1 amend DEC-40 (base-asset + USDS-M context; token-swap notices); A-2 L9a Bonferroni FWER 5% over 6 tests (z exact, ~2.638), frozen seed, no reroll; A-3 forward stale-quarter ticks skipped; A-4 Kraken #5 stays unavailable (m=6). Recorded in AMENDMENTS-2026-10-16.md 5213e0d. Implementation: Task 29d (A-1 + A-2 + A-3) after 29b/29c
Task 29b: 9f9fa8a (MJ3/MJ5/minors; focused ~610 passed); m2 forward T_fit gap at W_HOLD start → Ruling R57: bridge panel (W_DEV decisions excluded by S-1.5 window end, issued under the opening) so refit samples cover full grid D0..W_HOLD end; added to Task 29d
Task 29c: fdc8e24 (R56 manifest, R55 gate; AMB-44/45); concerns: opening/forward_model gated via inputs only (AMB-45f); C6 dry-run store needs ingest.adopt_ledgers
Ruling R58: authorize/resume/forward_model are gated through their gated inputs (panels/runs from label_panel/run_book, store-backed) — accepted, no guard API change — cost if wrong: a hand-built input could bypass; red-team r3 to check
Tasks 29b+29c: controller full gate 1076 passed, ruff clean; combined review dispatched; Task 29d dispatched
USER (2026-10-16): when checks pass, proceed to the next step without waiting
Tasks 29b+29c review → 2 Important: store-less universes bypass R55/R56 (R58 premise false); R56 consumer refuses R10 aggTrades (E_M impossible after opening) + minors; fix round sent to 29c implementer
Ruling R56b: pinned manifest also covers W_HOLD daily aggTrades listings for all in-scope symbols (per symbol-month single-page)
Ruling R58 REVISED: every research entry point refuses a universe without a store ingest check; synthetic tests via an in-memory source with a real empty verified ledger
Tasks 29b/29c: fix round 1 9f18281 (store-less refused, R56b aggTrades, minors); re-review dispatched
Task 29d: 183ccee + ac6c29d (A-1/A-2/A-3/R57; C4 after A-1: 5/16 book-window B/C protected, Stage-0 missed 65→52); AMB-46 false base matches (BTC/BNB/ETH/USDC/NOT)
C6 phase A done (representative R3; R4/R2 full; R5 2024-Q2; metadata). Real DEC-33 conflict BTCUSDT 2023-11 (5 bars, open); 72 N3 refusals for non-alphanumeric (delivery/SETTLED) symbols; peak 635 MB; full R3 ~6.3 h, full R5 ~9.1 GB / 15.5 h
USER (2026-10-16): A-5 AMB-46 same-sentence base+context; A-6 DEC-33 conflicts → drop keys as missing (recorded), W_DEV and W_HOLD
Task 29e: implement A-5 + A-6 + driver N3 skip for non-perpetual symbols; C4 re-run
Tasks 29b/29c fix round 1: re-review APPROVED; hardening (StoreSource under auth, manifest prefix index, AMB-44 stale-manifest note) + A-6/R55 conflict reconciliation added to Task 29e
Tasks 29b, 29c: complete (commits 5213e0d..9f18281)
Task 29e: d448668 + c2a49e1 (A-5: 17 false matches removed, none lost; A-6; StoreSource-under-auth; manifest index)
C6: BTCUSDT 2023-11 rechunked under A-6 (7 keys dropped, unresolved=null); 72 N3 non-perpetual gaps marked not applicable (r2_rest unresolved 0)
Tasks 29d+29e review → Important: A-3 skip keyed on quarter starting at ts but the tick marks with the bar ending at ts → stale quarter still voids forward run; minors (C4 count text 18 vs 17, StoreSource check not tied to universe identity/window, a6_resolutions caller-supplied, perpetual_shaped symbol rule, swap-destination + one-letter-base classes for user list)
Ruling R59: A-3 skip keys on the quarter whose close the tick needs (ending at ts, i.e. ev(ts - 15m)) — the A-3 intent ('stale quarter → no mark, no risk, no INVALID') — cost if wrong: one-quarter offset in forward risk skip
Controller full gate at c2a49e1: 1122 passed, ruff clean; 29d/29e fix round sent (R59 + minors)
Tasks 29d+29e: fix round b3dfbf3 (R59 etc.); controller full gate 1122 passed, ruff clean — accepted (fixes match review)
Tasks 29d, 29e: complete
Task 29a: dispatched (BASE b3dfbf3, implementer opus)
Task 29a: 3860c4f lazy 1m (byte-identical labels/books; small bench labels 3-4 ms, runs 3-9 s, peaks <300 MB); remaining: R3/R4 resident (~10-17 GB), label_panel dicts (4-8 GB/horizon), aggTrades whole, guard path cost; round 2 sent
Task 29a: round 2 7844ca5 (lazy 15m/R4/aggTrades, columnar label_panel, cached guard location; byte-identical); extrap W_DEV peak ~2.2 GB (≤3.1 GB label process), ~25 h single / 8-10 h on 3 procs; full gate running
Task 29a: controller full gate 1137 passed, ruff clean; review dispatched; controller running bench_outcome_side at 20 and 60 symbols
Task 29a review → Important: lazy universe silently builds ~2.5 GB chunk universe on view-store miss (budget unenforced); minors; round 3 sent
Bench outcome side 20 sym: peaks labels 288 / E_T 311 / E_M 322 / canary 403 MB; extrap labels 4.3 h, books 24.6 h (linear upper), L9 5.5 h; 60-sym run in progress
PAUSED (2026-10-16, user: weekly usage nearly exhausted; resume later). Task 29a round 3 implementer STOPPED mid-work; uncommitted WIP left in working tree (AMBIGUITIES.md, rawstore.py, runner.py, runner_select.py, test_lazy_1m.py, test_perf_equivalence.py, test_runner.py) and saved as task-29a-round3-WIP.patch. HEAD 7844ca5 (gate 1137 passed). 
RESUME PLAN: (1) finish Task 29a round 3 (review fixes: refuse view-store miss in lazy universe w/ compute_missing opt-in; clear ChunkCache after from_source; fold-training memory if bit-identical; pandas version assertion; minors) — either continue from WIP or `git checkout -- .` and redo; (2) full gate; (3) re-run bench_outcome_side at 20 and 60 symbols (60-sym run failed while code was being edited); (4) C6 phase B on var/v7/dryrun (gates/universe/features/views profiling, outcome-free); (5) red-team round 3 focused on 29a–29e + C6; (6) final report (READY / NOT READY for code-freeze review). Never create v7-code-frozen without explicit user approval.
OPEN USER ITEMS for final report: AMB-46 new classes (swap-destination tickers; one-letter bases e.g. AUSDT); forward-stage items (AMB-43(e) store→EngineInputs loader, recorder format); build_hold_manifest.py not run against network.
