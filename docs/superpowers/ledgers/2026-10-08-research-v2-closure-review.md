# Research V2 closure review (93aa3ef..b7fde1b, whole branch aa41441..b7fde1b)

Reviewer: independent final closure review, read-only. Line numbers are for the b7fde1b tree.
Two small snippets were run with `.venv` Python to confirm findings I-1 and M-A. The full suite, var/ and the background runs were not touched.

## Minor-finding verdicts (M1–M12)

| # | Verdict | Evidence |
|---|---|---|
| M1 | ADDRESSED | `luna/v2/xsec_sim.py:593`: the forward-filled flatten friction is now `(half_spread + slippage) * cost_mult`, and the fee keeps `* cost_mult` (:602). This matches `luna/engine/backtest.py:413`. Test: `tests/v2/test_xsec_sim.py:270`. |
| M2 | ADDRESSED (turnover). Holding-period note missing | `xsec_sim.py:82,436,566-567`: `traded_notional` excludes transfer fills (`book(..., transfer=True)` at :431/:438). `metrics.py:143-146` uses it. Test: `test_xsec_sim.py:286`, `test_metrics.py:234`. The requested report note ("cross-sectional holding period = period-leg") is not in `luna/v2/report.py`. **New issue:** see I-1 on mixed-engine candidates. |
| M3 | ADDRESSED | `metrics.py:242,264-266`: `(fees + spread_slip - funding_total) / gross`. The sign is correct: `pnl_net = gross - fees - spread_slip + funding`, so funding paid lowers the ratio's numerator by a negative amount and is counted as a cost. Test: `test_metrics.py:249`. |
| M4 | ADDRESSED | `backtest.py:421-425`: the whole last row is rewritten after the flatten. `xsec_sim.py:607`: `bar_row()` is recomputed on the flattened book. Both paths run only with `flatten_at_end=True`, which defaults to False, so V1 is unaffected. |
| M5 | ADDRESSED | `portfolio.py:63` adds `Position.meta` (default `{}`). `xsec_sim.py:554` stores `risk_precap`/`period` at open, and :568-570 store `entry_notional`/`traded_notional`. `xsec_sim.py:163-180` restores them on carry-in. Test: `test_xsec_sim.py:294`. |
| M6 | ADDRESSED | `xsec_sim.py:378-379`: `decide()` records misses when a new decision cuts the previous retry window short. The `i == retry_until+1` path (:574-575) still covers weekly. There is no double count: `retry_until` is reset at :415 before the :574 check. Boundary re-decide: the fresh `_State.retry_until = -1` (:102), so nothing is recorded. Test: `test_xsec_sim.py:322`. |
| M7 | ADDRESSED | `xsec_sim.py:185-189`: `pid_of` raises on more than one open leg per asset. |
| M8 | ADDRESSED. Introduces edge case M-B | `luna/v2/xsec.py:45-51`: a day whose bars are all forward-filled becomes NaN, so it counts as neither history nor a return. Test: `test_xsec.py:68`. |
| M9 | ACCEPTED-BY-RULING | progress.md:97. Correct cache invalidation (`tradelists.py` `cache_key` `order_model`). |
| M10 | ADDRESSED (mostly) | `report.py:89,230,246,358-360`: summary.json and the HTML record `data_end`, `last_bar` (panel max, `runner.py:166-174`), assets and git sha. Still missing from the original ask: the max **funding** timestamp, the spot `universe_panel` last bar, and a log line. `assert_before_cutoff` (`luna/v2/data.py:23-32`) enforces all three, so this is only an audit gap. |
| M11 | ADDRESSED (traceability) | `tests/v2/test_v2_walkforward.py:250,266` now exist under the plan names. Both compare defaults with explicit defaults, so they are close to tautological. The real V1 regression evidence is that the V1 test suite is unchanged on the branch (only `tests/test_config.py`, the SC-2 fee value) plus `tests/v2/test_order_model.py:55`. |
| M12 | ADDRESSED. Sign-convention nit M-C | `metrics.py:154-187,300-302`: daily exposure series and ex-ante vs realized ES95. `report.py:296-307,344` plots exposure over time. |

## Re-verification

### (a) No look-ahead in V2 fitting, selection or simulation

- **Signal fits are train-only.** `luna/v2/walkforward.py:126-127` keeps trades with `entry_ts >= train_start` and `exit_ts < train_end`, then calls `_check_train`. The universe is `universe_at(..., w.train_end)` (:110-113).
- **Cross-sectional fits are train-only.** `fit_xsec_config` simulates `[train_start, train_end)` only (:180) with no `flatten_at_end`. Only closed trades are used, and `_check_train` runs at :184. Funding rates come from `_known_rates(funding, train_end)` (:177).
- **Selection** (`select_candidate`, :232-241) uses train objectives and acceptance only.
- **Decisions use decision-bar data and fill on the next bar.**
  - `decide()` reads `dc.loc[dc.index <= t]` and `mkt.last[i]`, the close of the decision bar (`xsec_sim.py:377-414`).
  - Orders are placed in step 6, after step-1 fills. They fill through `broker.on_bar` on bar i+1.
  - Transfers happen at bar i+1's open (`transfer_at = i + 1`, :416; `px = open[i]`, :428).
- **Causal resampling.** `resample_causal` (`luna/v2/timeframe.py`) emits only complete buckets. Each is stamped at the open of its last 15m bar, which is when its close is known under the engine's decide-at-close convention. The daily close used for ranking at the 23:45 stamp is the close of that same bar.
- **Ranking and covariance.**
  - `lookback_returns` and `eligible` (`xsec.py:54-78`) read only stamps `<= t`.
  - The covariance uses `hist[elig].iloc[-200:]` with `hist <= t` (:384-387).
  - `ewma_cov` and `trailing_corr` use the last rows only.
- **Boundary re-decide** (`xsec_sim.py:471-482`): the decision is taken at `idx[i0-1]`, with marks at `mkt.last[i0-1]` and data up to that bar. Its orders fill on `i0`. This is causal.
- **Forward-filled days.** NaN closes (M8) make an asset ineligible when the close at t or at t−L is synthetic, and remove synthetic zero returns from the covariance. This is causal, but see M-B. `real_at` requires a real 15m bar at the decision stamp.
- **Not look-ahead in V2's sense, inherited from V1:** `_Funding.fallback` in the `estimated`/`estimated_x2` sensitivity variants uses the mean of the whole loaded (pre-cutoff) funding series.

### (b) V1 behaviour unchanged at default arguments

- **Order model.** `luna/types.py` `default_order_model(kind)` = perp taker, spot maker. This is exactly the old `kind == "perp"` branching it replaces in:
  - `planner._make_order` (IOC/band vs POST_ONLY/half-spread);
  - `constructor.construct` (round-trip fee side);
  - `tradelists.build_tradelist` (fee side and the `maker` flag of `simulate`).
- **Pipeline** passes `order_model=None` through.
- **`run_backtest`.** `flatten_at_end=False` skips the whole new block. BAR_STAT_COLUMNS gains two **appended** columns (`net_leverage`, `max_asset_share`). The existing columns keep their positions and values. The only V1 consumers are `luna/report/metrics.py:68-70` (by name) and `engine/walkforward._stitch` (concat).
- **`engine/walkforward._simulate`.**
  - With `initial=None`, `owners=None`, `flatten_last=False` and `order_model=None`, the code is identical.
  - Strategy ordering is unchanged for V1 names: `present - set(STRATEGIES)` is empty.
- **`Position.meta`.** It defaults to `{}` and V1 never sets it. Persistence (`store/models.py` `position_to_row`/`position_from_row`) uses an explicit field list, so the paper-bot DB format is unchanged.
- **Tradelists.**
  - `SOURCE_MODULES` is absent on V1 classes, so V1 code hashes are unchanged.
  - `order_model` in the cache key means one cache miss (M9).
- **Tests.** No V1 test file changed except the SC-2 fee assertion (`git diff aa41441 b7fde1b -- tests ':!tests/v2'`).
- **Note.** SC-2 changes V1 spot *config input* (fees), not V1 code. This is intended and documented. bot-spot picks it up on its next image rebuild.

### (c) No V2 path reads bars at or after 2026-07-01, and none touches the holdout

- **Loading.** The only data entry point is `load_v2_inputs` (`luna/v2/data.py:41-50`).
  - It calls `load_inputs(..., data_end=V2_DATA_END)`. That function reads bars up to `data_end - 15m` and funding `< data_end` (`luna/report/walkforward.py:129-150`).
  - It then asserts the panel, funding and spot `universe_panel` with `assert_before_cutoff`.
- **Windows.** `v2_windows` uses `holdout_months=0`, ends at V2_DATA_END, and raises if the last `test_end` is past it.
- **No other reads.** grep finds no `BarStore`, `store.`, `read_parquet`, `holdout`, `run_holdout` or registry use anywhere in `luna/v2/` besides `data.py`. The runner, walk-forward and report consume `Inputs` only.
- **CLI.** `research-v2` (`luna/cli.py:199-235`) calls only `load_v2_inputs` and `run_research`, and writes to `var/reports/research_v2_*`. Its test asserts that `holdout_registry.json` is not created.
- **Caches.** Trade-list cache keys include a data hash of the truncated panel, in a separate dir (`var/research_v2`).

### SC-2

- **Commit 4b945dd** touches only:
  - `config/default.yaml:35-37` (spot maker 0.0025→0.004, taker 0.004→0.008, with an inline comment carrying the old values and the date);
  - the `tests/test_config.py` assertion;
  - two superseded notes in V1 result docs;
  - the record `docs/superpowers/specs/2026-10-08-research-v2-spec-change-2-fee-schedule.md`.
- **Unchanged:** perp fees, spread, slippage, and every strategy, risk and grid value.
- **The record contains** old and new values in a table, the source (the Kraken fee schedule URL, fetched 2026-10-08, plus a secondary source), the date, who requested it, what it affects, and its consequences. The frozen spec text is not edited.
- **Requirement met.**

## Issues

### Critical
None.

### Important
None that block. I-1 is report-only but will appear in the final reports, so it is listed first.

### Minor

- **I-1 (new with M2): candidate turnover drops signal-engine trades when a candidate mixes engines.** `luna/v2/metrics.py:143-144`.
  - `_stitch`/`_concat` of signal and cross-sectional window results leaves `traded_notional` NaN for signal trades. The column is present, so the cross-sectional branch is taken, and `.sum()` skips the NaN rows.
  - Confirmed: one signal trade plus one cross-sectional trade gives turnover 0.2. It should be 0.4.
  - Fix: `traded_notional.fillna(2 * gross_notional)`.
  - The same pre-existing pattern affects `cap_adjusted_r` (`metrics.py:41-42`): signal rows get NaN, so the reported mean covers cross-sectional trades only. Fix with `risk_precap.fillna(risk_per_trade x equity at signal)`.
  - Neither feeds GO/NO-GO (`luna/report/gonogo.CHECKS`). A report rebuild from `results.pkl` would fix the output without re-simulating.
- **M-B (new with M8): an eligible perp asset can have fewer than 30 finite daily returns, and then the whole book closes for that rebalance.** `luna/v2/xsec.py:66-78` with `luna/v2/portfolio_risk.py:96-97,153-156`.
  - Eligibility counts at least 31 non-NaN *closes*. One all-forward-filled day costs *two* returns, so a newly eligible asset can reach `ewma_cov` with fewer than 30 observations. The covariance entry is then NaN, and `project_weights` returns `_closed` (all targets 0, event `cov_unavailable`).
  - Confirmed with a snippet: 32 closes with one gap gives 29 returns and a NaN covariance.
  - It fails closed (conservative). Before M8 it was impossible. It only matters near listing.
  - Check the final perp run's events for `cov_unavailable`. If there are none, there was no impact.
  - Fix: require at least 30 finite log returns in the covariance slice for eligibility, or drop such an asset from `elig` before ranking.
- **M-C: opposite sign conventions in `ex_ante_es`.** `luna/v2/metrics.py:175-187`. `forecast_es95_mean` is a positive loss magnitude, while `realized_es95` (`cvar`) is a negative return. Label them or negate one.
- **M-D: M2's report note is missing.** There is no statement that cross-sectional holding period / `holding_hours` is per period-leg and capped at the rebalance interval (`luna/v2/report.py` caveats).
- **M-E: M10 is partial.** The funding max timestamp and the spot `universe_panel` last bar are not in summary.json, and nothing is logged.
- **M-F: `docs/results/research-v2.md:113` is stale.** It still cites spot maker 0.25% and pre-closure numbers, and `docs/results/walkforward-spot.md`'s superseded note points readers to it. Regenerate it from the final runs before merging. This is already the plan; noted as a merge precondition.

## Declined to judge

- **Whether any real perp asset has all-forward-filled days near eligibility (M-B's real impact).** That needs var/ data, which is out of bounds.
- **Correctness of the external fee figures and the 2026-07-09 restructure date in SC-2.** These are external facts; I checked the change's scope and documentation only.
- **Items already ruled at progress.md:90** (mixed R scales, grid degeneracy, HTF stop timing, survivorship, and the others). They are unchanged.
- **The full-series mean in the `estimated` funding fallback.** Inherited from V1, and used only in the sensitivity variants.
- **Whether the background runs started at b7fde1b with a clean tree.** Check `manifest.json` `git_sha`/`git_dirty` in each run directory.

## Assessment

**Yes. Ready to merge after the final runs**, with these preconditions:

1. Regenerate `docs/results/research-v2.md` from the final runs (M-F).
2. Confirm each run's `manifest.json` shows `git_sha` b7fde1b and `git_dirty=false`.
3. Grep the perp run's events for `cov_unavailable` (M-B).

I-1 and M-C affect only reported diagnostics. They can be fixed with a report rebuild and need no re-simulation. If the final candidate never mixes engines, I-1 has no effect on the output.

No look-ahead, V1 regression or holdout access was found.
