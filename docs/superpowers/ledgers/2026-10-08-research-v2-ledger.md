# SDD ledger — plan: docs/superpowers/plans/2026-10-08-luna-research-v2.md

Spec: docs/superpowers/specs/2026-10-06-luna-research-v2-design.md (FROZEN rev 3). Branch feat/research-v2 from aa41441 (plan commit b70f3db).
User approval 2026-10-08 ("devam" after plan presentation): SC-1 (recommended text), P1–P12, subagent-driven.

Ruling: work in place on branch feat/research-v2 (no git worktree) — Task 12 needs var/ data and the Docker bots run from built images, not source — cost if wrong: a branch switch in this checkout while bots are rebuilt would pick up V2 code (bots are not rebuilt during V2).

## Pre-flight scan

| Pair / task | Produces vs consumes | Finding |
|---|---|---|
| T1 → all | V2_DATA_END, load_v2_inputs, v2_windows, resample_causal, Timeframe, fixtures v2_panel/v2_settings_small | Tests use a `settings` fixture that no conftest defines (tests use module-level SETTINGS). Ruling R1 below. |
| T2 ↔ T3/T4 | tsmom_grid/breakout_grid dict keys == strategy params | consistent (fast, slow, atr_n, stop_atr, max_hold / donchian, atr_n, k_atr, max_hold). Sizes checked by hand: tsmom 6/6/6/4, breakout 6/6/6/4. |
| T2 ↔ T10 | count_trials 136 / 76 | checked: perp (22+22)×2 + 24×2 = 136; spot (16+16)×2 + 6×2 = 76. OK |
| T3 internal | 15m V1 point == NaiveMomentum; equivalence on v2_panel | 120-day fixture may give < 5 trades on 1d. Ruling R2. |
| T3 ↔ T4 | V2_STRATEGY_CLASSES extended by T4 | OK |
| T5 ↔ T6 | project_weights, xsec_limits, CAT_STOP | OK |
| T6 ↔ T7 | run_xsec without order_model; T7 adds it | fixed in plan text before commit. OK |
| T6 ↔ T8 | XSEC_BAR_EXTRA includes net_leverage/max_asset_share; T8 adds those to V1 BAR_STAT_COLUMNS | duplicate columns after T8. Ruling R3. |
| T7 ↔ T11 | missed_fills(res, models: list[WindowModel], …) | candidate windows of xsec engine have no WindowModel; Ruling R4. |
| T8 ↔ T11 | extended_metrics keys | OK |
| T9 ↔ T11 | robustness dict, no verdict keys | OK |
| T10 internal | _simulate extension + flatten_at_end in V1 modules | V1 behaviour must be golden-tested unchanged (plan has tests). OK |
| T1 internal | test_v2_windows_end_at_cutoff 18 windows, first test 2023-07-01 | checked by hand: 8+2k ≤ 42 → k ≤ 17 → 18. OK |
| T2 internal | atr 4h = max(6,14) = 14; hold(60d) = 120d = 11520 | OK |
| T5 internal | test_section12_consistency | OK |
| T11 internal | "aday yok" key `candidate` | OK |
| T12 | operational | runs only after all reviews; never holdout |

Ruling R1: tests/v2/conftest.py (Task 1) also defines fixture `settings` = luna.config.load_settings() — the plan's tests reference it — cost if wrong: none (test-only).
Ruling R2: v2_panel fixture length 240 days (not 120); equivalence tests require >= 5 trades for 15m/1h/4h and >= 3 for 1d — keeps equivalence meaningful on slow timeframes — cost if wrong: slower tests (~seconds).
Ruling R3: when Task 8 adds net_leverage/max_asset_share to V1 BAR_STAT_COLUMNS, XSEC_BAR_EXTRA becomes ["vol_scale", "ex_ante_var"] and run_xsec fills the shared columns — avoids duplicate columns — cost if wrong: small refactor in T8.
Ruling R4: missed_fills only applies to signal-engine windows (maker candidates); xsec maker misses are reported from run_xsec risk_events `missed_rebalance` (count, notional) — spec §6 asks missed-fill cost for "dolmayan maker girişleri" which xsec has as missed rebalance deltas — cost if wrong: xsec missed-fill R not computed (reported as notional only).

## Tasks
Task 1: dispatched (base b70f3db, implementer sonnet)
Task 1: first implementer interrupted twice by session restarts (uncommitted draft left); re-dispatched fresh implementer (sonnet) on the draft
Ruling R5: switch execution from subagent-driven to inline (executing-plans) — background subagents are killed every time the user's session ends between "devam" messages (Task 1 implementer lost 3 times); inline work lands commits within one turn. Review: one whole-branch review on the most capable model at the end (re-dispatched if interrupted) — cost if wrong: per-task review gates are lost; defects surface only at the final review.
Task 1: draft from interrupted implementers verified by controller (13 v2 tests pass, ruff clean); 1d incomplete-bucket test asserts 1 complete day for 98 bars (brief's "0" was a plan typo: 96+2 bars from 00:00 contain one full day)
Task 1: minor (deferred): tests/store/test_db.py::test_lock_exclusive_across_processes flaked once under full-suite load (passes alone) — V1, pre-existing timing test
Task 1: complete (commits b70f3db..4b9d720 feat(v2): holdout cutoff enforcement and causal 15m resampler (V2-1)
Task 2: complete (commits 4b9d720..0ee7caf, tests: pytest tests/v2 -> 22 passed)
Task 3/4: Ruling: TSMOM and breakout implemented and committed together in one commit (same module luna/v2/strategies.py) — avoids an intermediate state of a half-built file — cost if wrong: one commit covers two plan tasks.
Task 3/4: Ruling: luna/research/tradelists._source_files also hashes cls.SOURCE_MODULES (V2 strategies list luna/v2/timeframe.py, params.py) — the resampler/grids shape signals but sit outside the class MRO, so stale trade-list caches would otherwise survive resampler changes — cost if wrong: none for V1 (attribute absent).
Task 3: complete (commits 0ee7caf..70fbee1, tests: full suite -> 566 passed)
Task 4: complete (commits 0ee7caf..70fbee1, tests: full suite -> 566 passed)
Task 5: complete (commits 70fbee1..ae55d84 incl. SC-1 record d0354fd, tests: pytest tests/v2 -> all pass)
Task 6: Ruling: eligible() and raw_targets() take the decision stamp t (plan signatures omitted it) — ranking must be read at the decision stamp, never at the last available daily row — cost if wrong: signature differs from plan text only.
Task 6: Ruling: eligibility requires max(lookback + 1, 31) daily closes (plan said max(lookback, 30)) — an L-day return needs L+1 closes and the 30-day covariance needs 31 — cost if wrong: an asset joins one day later.
Task 6: Ruling: untradeable residue of a closing leg (qty-step rounding / minimums) is written off at the mark (Portfolio.write_off_dust), as V1 plan_exit does — otherwise a killed or rotated leg never closes — cost if wrong: residue P&L booked at the mark instead of a fill.
Task 6: Ruling: plan test "test_spot_rebalance_buys_wait_for_cash" renamed ..._wait_for_sells and accepts R8 or R17 vetoes — on spot, R8 (gross 1x, closes never free budget within a batch) fires before R17; the behaviour (buys retried after sells fill) is the one the plan asks for — cost if wrong: none.
Task 6: complete (commits ae55d84..8016477, tests: full suite -> 597 passed)
Task 7: Ruling: missed_fills(res, models, panel, funding, settings, kind) takes panel/funding instead of an Inputs object — keeps luna.v2.fills independent of the report layer — cost if wrong: callers pass inp.panel, inp.funding.
Task 7: Ruling: maker tests use wickless bars (high/low = max/min(open, close)) — on the v2_panel wicks every post-only order fills, so the unfilled path is never exercised — cost if wrong: none (test data only).
Task 7: complete (commits 8016477..8fedfb1, tests: full suite -> 608 passed)
Task 8: Ruling: extended_metrics also returns planned_risk_usd (mean risk_amount) next to planned_r — spec §6 "Planned R" names the planned risk amount itself — cost if wrong: one extra key.
Task 8: complete (commits 8fedfb1..255498b, tests: full suite -> 619 passed before final lint-only edit; tests/v2/test_metrics.py -> 11 passed after)
Task 9: Ruling: time-LOO assigns trades to windows by exit_ts and asset HHI uses |net P&L| by asset — exit_ts matches how window net_pnl is booked; |P&L| keeps HHI defined when assets have opposite-sign P&L — cost if wrong: diagnostic definitions only.
Task 9: complete (commits 255498b..9013cb8, tests: pytest tests/v2 -> 103 passed)
Task 10: Ruling: orchestration lives in a new luna/v2/runner.py (resumable units, worker pool with one input copy per worker); luna/v2/walkforward.py keeps fitting/selection/simulation — the plan put both in walkforward.py; splitting keeps each file one responsibility — cost if wrong: one extra module.
Task 10: Ruling: all configurations share one span: OOS starts at the first window where any primary configuration has a non-empty universe (V1 skips leading empty windows) — keeps config runs, candidate and naive bar on the same development OOS — cost if wrong: a btc_eth config could start a window earlier on its own.
Task 10: Ruling: asset leave-one-out re-fits the configuration without the asset (signal: trade lists filtered; xsec: train simulations re-run); the candidate's LOO re-runs the nested selection over those re-fits (configs whose universe never held the asset keep their original fit) — matches spec §5 "walk-forward yeniden koşar" — cost if wrong: compute time.
Task 10: Ruling: cross-sectional train objective applies the V1 worst-case unknown-funding rule per trade (2 x max(|rate|, floor) x unknown hours x notional / risk) — same funding honesty as V1 _choose_params — cost if wrong: perp xsec objective slightly conservative.
Task 10: Ruling: luna/v2/strategies.py builds every class at import — pickled strategy instances (worker processes, checkpoints) resolve by name — cost if wrong: none.
Task 10: complete (commits 9013cb8..0c322ed, tests: full suite -> 644 passed)
Task 11: Ruling: report wording avoids the word "holdout" entirely (the untouched final generalization set is described by date) — a test asserts summary.json never mentions it, the strongest guard that no holdout metric leaks into V2 output — cost if wrong: none.
Task 11: Ruling: missed-fill cost is computed on the candidate's maker run (perp: order_alt_edge; spot: edge); xsec missed rebalances are reported as count + notional (R4) — cost if wrong: perp main (taker) has no missed fills by construction.
Task 11: complete (commits 0c322ed..8d08698, tests: full suite -> 651 passed)
Ruling R6: final whole-branch review runs BEFORE Task 12 (the multi-hour research run) — results must come from reviewed code; a real-data smoke run (asset subset) runs in parallel to catch runtime issues — cost if wrong: none.
Task 12 pre: smoke run started 2026-10-08T13:45:53Z: luna research-v2 --account perp --assets BTC,ETH --jobs 1 (detached, log var/research_v2_smoke_perp.log); final review dispatched (opus) on aa41441..8d08698
Final review (opus, aa41441..8d08698): With fixes — 0 Critical, 6 Important (I1 runtime, I2 pool failure handling, I3 kill carried into next window never flattens, I4 xsec decision at window-boundary stamp dropped, I5 LOO memory, I6 resume without manifest), 12 Minor. Full text: final-review.md. Smoke run stopped (pre-fix code).
Final: fixed I3 kill carried into next window — test_kill_carried_into_next_window_flattens RED->GREEN (also: no catastrophic-stop relabel while killed)
Final: fixed I4 boundary rebalance dropped — test_rebalance_on_window_boundary_is_not_dropped RED->GREEN
Final: fixed I1 runtime — test_ewma_cov_matches_pandas_exactly_with_gaps pins pandas equality (written before the numpy rewrite, GREEN on both); subset panel per xsec call; refit_without (test_refit_without_reuses_windows_without_the_asset, written with the fix)
Final: fixed I2 pool failures — test_pool_checkpoints_finished_units_and_cancels_on_failure (written with the fix)
Final: fixed I5 LOO memory — test_loo_results_are_trades_only (written with the fix)
Final: fixed I6 resume manifest — test_resume_refuses_a_different_run (written with the fix); suite 658/658, commit d395c66
Final: minor (deferred): M1 flatten on forward-filled last bar ignores cost_mult in frictions (xsec_sim)
Final: minor (deferred): M2 xsec turnover counts weekly carry-over as traded; holding period capped at one week by the trade definition
Final: minor (deferred): M3 cost_to_gross excludes funding
Final: minor (deferred): M4 last-bar leverage/share stats stale after end-of-window flatten
Final: minor (deferred): M5 carried legs lose risk_precap (mostly removed by the I4 fix)
Final: minor (deferred): M6 missed_rebalance never emitted for daily rebalance
Final: minor (deferred): M7 one-position-per-asset in xsec pid_of not asserted
Final: minor (deferred): M8 eligibility counts forward-filled days as history
Final: minor (deferred): M9 order_model in cache key: every V1 trade-list cache misses once
Final: minor (deferred): M10 last bar read not in summary.json (now in units manifest.json)
Final: minor (deferred): M11 two plan-named tests absent under those names (V1 suite covers)
Final: minor (deferred): M12 exposure reported as aggregates only; no xsec ex-ante ES comparison
Final: Ruling: reviewer "declined to judge" items (mixed R scales in nested selection, degenerate gross grid under the 30% cap, HTF stop timing, flat windows non-positive, flat-after-xsec counts as signal engine, daily order cap on retries, survivorship) all stand as pre-committed by spec/P-decisions — changing them now would be a spec/plan change — cost if wrong: see final-review.md.
Task 12: pre-flight OK (git clean at d395c66, no holdout registry, 20.5 GB free, 16 cores). Docker daemon NOT running (Docker Desktop down; paper bots stopped) — reported to user, not touched.
Task 12: runs started 2026-10-08 17:15 TR, detached cmd processes: research-v2 --account perp --jobs 2 -> var/reports/research_v2_perp_20261008T141559 (log var/research_v2_perp.log); --account spot --jobs 2 -> var/reports/research_v2_spot_20261008T141600 (log var/research_v2_spot.log). Resume with --resume <dir> if interrupted.
Task 12: complete (runs finished EXIT 0: perp 17:16-20:18, spot 17:16-19:20; both INSUFFICIENT DATA, aday yok; docs/results/research-v2.md committed 93aa3ef)
Closure (user instruction 2026-10-08): SC-2 fee correction committed 4b945dd (spot 0.25/0.40 -> 0.40/0.80; perp 0.02/0.05 verified unchanged; old spot docs marked superseded).
Closure: Ruling: V1 spot walk-forward not re-run, marked superseded — a V1 walk-forward re-run builds trade lists over bars after 2026-07-01 (holdout fit window) — cost if wrong: V1 spot numbers stay on the old fee (documented).
Closure: minors M1-M12 closed in b7fde1b (suite 669/669). Result-affecting: M8 (eligibility/returns), M1 (2x-cost flatten on filled bar). Report-only: M2, M3, M4, M5, M6, M10, M12. None: M7, M11.
Closure: Ruling: M9 (order_model in trade-list cache key forces one V1 cache miss) accepted, no change — correct cache invalidation, one-time cost — cost if wrong: one recompute.
Closure: Ruling: perp re-run although its fees did not change — M8/M1 change simulation code, so the final report must come from final code — cost if wrong: ~3 h compute.
Closure: final runs started (perp + spot, --jobs 2), logs var/research_v2_{perp,spot}_final.log.
Closure review (opus, 93aa3ef..b7fde1b + whole branch): Ready to merge after final runs; 0 Critical/Important; M1-M12 addressed (M9 by ruling); new minors I-1, M-B, M-C, M-D, M-E fixed in 1cc450a (suite 672/672); M-F = research-v2.md regenerated from final runs.
Closure: spot final run (b7fde1b) died with MemoryError (Windows commit limit) while the test suite ran alongside both runs; both runs stopped; Ruling: final runs restarted from 1cc450a sequentially (spot then perp, --jobs 2, var/run_v2_final.cmd) with nothing heavy alongside — final report comes from final code incl. M-B — cost if wrong: ~5 h wall time.
Closure (2026-10-09, new PC; old PC var/ could not be copied): Ruling: data re-downloaded instead of copied. Perp: `luna backfill --venue perp --assets <the 21 run assets>`, so today's 24h-volume prefilter cannot change the universe. Spot: the backfill was called with end = 2026-07-01, i.e. archive-only (pinned SHA256, last bar 2026-06-30 23:45, no Trades gap-fill). V2 never reads at or after V2_DATA_END, so the gap-fill was useless for V2. Instrument caches re-fetched 2026-10-08. Cost if wrong: perp bars could differ from the old PC's (no data hash in the manifest).
Closure: final runs on e0203c8 (source identical to 1cc450a; only HANDOFF.md differs). Spot var/reports/research_v2_spot_20261008T222035: INSUFFICIENT DATA, 0 trades, no candidate. This reproduces the old PC's final spot result exactly (naive 158 trades, +0.98 R, +$150). Perp var/reports/research_v2_perp_20261009T000905: INSUFFICIENT DATA, 27 trades, +0.27 R, CI [-0.37, +0.96], no candidate. This is identical to the d395c66 run. Both reports are verified and SHA256-checkpointed by the overnight controller (var/overnight/checkpoints).
Closure: spot run interrupted twice (Claude Code memory-pressure reaper; manual stop to switch --jobs 1 -> 2) and resumed with the manifest-checked --resume. No unit mixed across runs.
Closure: Ruling: .claude/ (Claude Code session lock) excluded locally via .git/info/exclude (not committed). As an untracked file it would make git_dirty true and block a manifest-checked resume. Cost if wrong: none.
Closure: operational minor (not result-affecting): the spot Trades gap-fill checkpoint write (os.replace) raised PermissionError on Windows while the file was being read concurrently.
Closure: docs/results/research-v2.md regenerated from the final summaries only (scripts/v2_results_summary.py). Verdict: V2 CLOSED — NO CANDIDATE (perp and spot).
