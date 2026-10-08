# Research V2: final whole-branch review (aa41441..8d08698)

Reviewer scope: the full diff, the frozen spec (D1-D8, §3.4, §3.5, §4.2, §5, §6), SC-1, plan P1-P12 and its 5 Review Focus items, and the executor rulings in progress.md. Read-only. I ran three small snippets in the scratchpad: timing and profiling `run_xsec` on a synthetic 21-asset x 130k-bar panel, a kill-switch carry check, and a listing of window weekdays. I did not run the test suite.

## Strengths

- **Holdout isolation is solid and layered.** `load_v2_inputs` cuts at `V2_DATA_END`, then asserts on panel, funding and universe_panel and raises `HoldoutLeak`. `v2_windows` refuses a window that ends after the cutoff. A source-scan test forbids holdout symbols. Every V2 path, including report, naive bar, missed fills and MFE/MAE, reads only `inp`. I found no second store read.
- **No look-ahead found** in:
  - `resample_causal`: complete buckets only, stamped at the last 15m bar.
  - The sparse HTF `SignalFrame` design.
  - Signal fitting: `entry_ts >= train_start`, `exit_ts < train_end`, `_check_train`, V1 `_known_rates(train_end)`.
  - Cross-sectional ranking, eligibility and covariance, all read `dc.index <= t`.
  - Fills at i+1, catastrophic stop at close then IOC at i+1, and the transfer at the fill-bar open.
  - Nested selection, which uses train objectives only.
  - `test_xsec_is_causal` and the per-timeframe resample causality tests pin this down.
- **V1 defaults are preserved.** Every shared-module change is a trailing keyword whose `None`/`False` default resolves to the old branch:
  - `default_order_model` reproduces perp IOC and spot post-only.
  - The kernel `post_only` and fee side are unchanged.
  - The `_simulate` name ordering is identical for V1 names.
  - D8 holds: no `luna/strategy/*` edits.
- **Cross-sectional accounting is careful.**
  - The same-sign re-reference is equity-neutral: fee 0, fill = ref at the same price, out then in.
  - A sign flip is a close plus an open. Both orders have the same side and limit, and SimBroker has no partial fills, so they fill or fail together.
  - Dust write-off goes through the normal fill path. `finish` requires a flat position.
  - Equity identity tests cover perp and spot, flatten and engine switch.
- **GO/NO-GO wiring is faithful.**
  - `evaluate` is V1's, unchanged, with the same argument mapping as V1 `_finish`.
  - The bar is V1 naive momentum only (`naive_momentum_expectancy`; the test pins that a better naive mean reversion does not raise it).
  - Robustness is computed after the verdict and is never passed to it.
  - Reports are per account, and "aday yok" appears unless GO.
- **All 5 Review Focus items have tests that exercise the named behaviour.**
  - Focus 1: loader cutoff.
  - Focus 2: filled bar / short history.
  - Focus 3: k clip / empty universe.
  - Focus 4: NaN cov fails closed. The equity-≤0 half is handled by `decide` skipping targets when `eq <= 0`; there is no event.
  - Focus 5: engine-switch flatten with the identity across the switch.
- **The rulings are well reasoned.** The ones I weighed and agree with:
  - eligibility `max(L+1, 31)`;
  - `eligible`/`raw_targets` take `t`;
  - dust write-off;
  - LOO re-fits;
  - shared span;
  - worst-case unknown funding in the cross-sectional objective;
  - `SOURCE_MODULES` in the trade-list cache key.

## Issues

### Critical

None found. I found no leakage, no V1 behaviour change and no verdict-wiring error.

### Important

**I1. Cross-sectional fitting runtime makes the perp run about 10 h+ at `--jobs 2`; the asset leave-one-out (LOO) step dominates.**
- **Where:**
  - `luna/v2/portfolio_risk.py:52-59` (`ewma_cov`);
  - `luna/v2/xsec_sim.py:141-151` (`run_xsec` setup);
  - `luna/v2/runner.py:186-188` (LOO re-fits);
  - `luna/v2/walkforward.py:160-188`.
- **What I measured** (synthetic 21 x 130k 15m panel, one 6-month train simulation): weekly broad 9.3 s, weekly btc_eth 4.9 s, daily broad 34.5 s. The profile shows:
  - pandas pairwise `ewm().cov()` is about 52% of the time. It runs once per decision (about 0.32 s each) and builds the full EWMA cov history to read only its last row.
  - About 3 s per call is fixed setup: the repeated `idx.union` loop over all panels, `_Market` over all 21 assets and full history (~175 MB transient), and `daily_closes` of the whole panel. This setup runs even for btc_eth and for windows with small universes.
- **Perp arithmetic:**
  - Fits: (2 weekly + 2 daily configurations) x 11 active windows x 24 points, about 4.9 h serial.
  - Weekly-broad LOO: 21 assets x 264 simulations x 9.3 s, about 14 h serial.
  - Total about 20 h serial, about 10 h wall time with 2 workers, before the signal work. Spot is about 1/4 of this (6 grid points).
- **Why it matters:** a 10+ h run on a PC with a history of OOM or interruption means more restarts. Resume granularity is one unit, and a cross-sectional LOO unit is about 40 min.
- **Fix** (all are result-neutral; verify bit-for-bit equality on the test fixtures):
  - (a) Compute the EWMA covariance's last row directly with numpy: normalised weights (1-α)^k with adjust=True, pairwise-complete to match pandas. Add a test against `ewm().cov()`.
  - (b) Build the union index, `_Market` and `daily_closes` once per worker (memoise on `id(panel)`), or pass a panel restricted to `universe ∪ {BTC}`.
  - (c) In LOO re-fits, reuse the original `ConfigWindowModel` for every window whose universe does not contain the removed asset.
  - Expected gain is roughly 3x or more.
  - If you choose not to optimise, at least budget for it: run the real-data smoke run with a timer and set expectations.

**I2. Runner failure handling wastes hours and loses finished work.**
- **Where:** `luna/v2/runner.py:122-129`, `163`, `208-210`.
- **What is wrong:**
  - All units are submitted at once, and results are consumed in submission order (`for fut in futs: fut.result()`).
  - If one unit raises, `fut.result()` propagates, and `finally: pool.shutdown()` (wait=True, cancel_futures=False) then waits for every queued unit to finish, possibly hours. Their results are discarded and never checkpointed.
  - Even without errors, a unit that finished out of order is not checkpointed until all earlier-submitted units finish, so a crash or OOM kill loses it.
- **Fix:**
  - Iterate `concurrent.futures.as_completed(futs)`, checkpoint each result as it arrives and log a progress count.
  - On the first exception, call `pool.shutdown(cancel_futures=True)`, or keep running the others and report the failures at the end.

**I3. A kill switch carried into the next cross-sectional window never flattens (realistic variant). Confirmed.**
- **Where:** `luna/v2/xsec_sim.py:402-407`, with the gate at `435`.
- **What is wrong:**
  - The flatten (`st.targets = {}`, `retry_until = 10**12`) fires only on the kill *transition* (`rs.kill_switch and not was_killed`).
  - Each `run_xsec` call starts a fresh `_State` with `retry_until = -1`. When a window starts with `initial_state.kill_switch=True`, no order is ever planned and decisions are blocked, so carried legs stay open until a catastrophic stop happens to close them.
  - My snippet carried `kill_switch=True` into window 2 with two open legs. They stayed open 10 days and about 2 months and closed only by `catastrophic_stop`.
  - The triggers are a kill at or near a window's last bar, or IOC closes that keep failing (forward-filled bar or price band) up to the window end.
- **Why it matters:**
  - The realistic variant feeds GO checks 4 (max DD, worst day, CVaR5).
  - V1's pipeline re-asserts exits for every position on every bar while killed, so the cross-sectional simulator diverges from V1 R4 semantics.
- **Fix:** at the top of each bar (or once at init), `if rs.kill_switch: st.targets = {}; st.retry_until = max(st.retry_until, 10**12)`. Add a test that carries `kill_switch=True` with open legs.

**I4. Cross-sectional engine state is not carried across contiguous test windows, so the rebalance decided on a window's last bar is lost.**
- **Where:** `luna/v2/walkforward.py:262-278` with `luna/v2/xsec_sim.py:155-157` (fresh `_State`, local `pending`).
- **What is wrong:**
  - Each window's `run_xsec` restarts with no targets, no pending orders, no `transfer_at`, no `retry_until` and no `blocked`.
  - Window starts include 2025-09-01, a Monday. The weekly decision at Sun 2025-08-31 23:45 is the previous window's last bar, so its orders (and the re-reference) are dropped. The book keeps the prior week's legs for another week with no `missed_rebalance` event.
  - The daily sensitivity loses the decision at every window boundary (10 days).
  - Pending catastrophic-stop or kill IOCs submitted on a last bar are also dropped (the stop is re-detected next window; the kill is not, see I3).
- **Why it matters:** a silent deviation from P5/P6 inside the development OOS. It is small in the primary (1 of about 87 weekly rebalances) but unreported. V1 carries `PendingExits` for the same reason.
- **Fix:** either carry a small cross-sectional carry object (state and pending orders) through `_run_segment_xsec`, or, causally and more simply, at `run_xsec` start, if `idx[i0-1]` is a rebalance stamp, run `decide(i0-1, …)` with data ≤ `idx[i0-1]` and treat i0 as the fill/transfer bar. The same fix closes I3's "pending kill IOC lost" path.

**I5. LOO results are kept whole in the parent process and pickled again into `results.pkl`; this is a memory risk on this PC.**
- **Where:** `luna/v2/runner.py:194-206`, `236-246`.
- **What is wrong:**
  - Every `loo:`/`candloo:` unit returns a full `WalkForwardResult`: OOS equity, ~64k-row `bar_stats` (9 columns), benchmark, candidates, risk events, portfolio with `closed`/`funding_log`.
  - On perp there are about 205 LOO units (8 broad signal x 21, 8 btc_eth x 2, 1 cross-sectional broad x 21 and 1 btc_eth x 2, plus about 21 candidate LOOs). At roughly 5 MB or more each, that is over 1 GB resident in the parent.
  - The parent also holds `_CTX` inputs and trade lists, about 50 full config/candidate runs, and then serialises everything into one `results.pkl` (another transient copy).
- **Why it matters:** robustness reads only `.trades` of these results (`asset_loo` → `trade_metrics(run_without(a).trades)`).
- **Fix:** have LOO units return `res.oos.trades` (or a slim object of trades plus per-window). Optionally drop `bar_stats`/`candidates` from what `results.pkl` stores, or write it per section.

**I6. Resume never checks whether checkpoints match the current run.**
- **Where:** `luna/v2/runner.py:106-112`; `cli.py` `--resume`.
- **What is wrong:**
  - Checkpoints are reused by unit name only.
  - Nothing records or checks git HEAD, account, `--assets`, `V2_DATA_END` or the trade-list cache key.
  - The `cand:*` and `candloo:*` names do not include the account, so `--resume <perp dir> --account spot` would mix accounts.
- **Why it matters:** fixes from this review will land mid-campaign. A resume after a code change would silently combine units from two code versions into one verdict.
- **Fix:** write `manifest.json` (HEAD sha, dirty flag, account, assets, data cutoff, trade-list cache key) at start, and refuse `--resume` on a mismatch.

### Minor

- **M1.** `luna/v2/xsec_sim.py:471`: the flatten on a forward-filled last bar applies frictions without `cost_mult`; `run_backtest` multiplies them (`backtest.py`, flatten block). As a result, `cost_2x*` variants under-charge that path. Fix: use `(…)*cost_mult`.
- **M2.** `luna/v2/xsec_sim.py:201-206` + `luna/v2/metrics.py:127-135`: the cross-sectional turnover is badly overstated, and its holding period is capped at one week by construction.
  - Cause: re-referenced (transfer) quantity is added to `entry_notional`, so a leg held for months counts as traded each week.
  - Fix: exclude transfer fills from `entry_notional`, or record traded notional separately for turnover. Note in the report that cross-sectional holding period = period-leg.
- **M3.** `luna/v2/metrics.py:202`: `cost_to_gross` leaves funding out of "total cost". Spec §6 says toplam maliyet ("total cost"). Include funding as a cost, or label the key "trading costs / gross".
- **M4.** `luna/engine/backtest.py:401-425` and `luna/v2/xsec_sim.py:477-479`: after the end-of-window flatten only `equity`/`n_positions` of the last bar are rewritten. `gross_leverage`, `net_leverage`, `max_asset_share` and `open_risk` stay at their pre-flatten values. This affects exposure maxima only marginally.
- **M5.** `luna/v2/xsec_sim.py:160-164`: legs carried into a new window get `risk_precap = risk_amount`, and their `entry_notional` is rebuilt from the current quantity. This slightly distorts cap-adjusted R and turnover for legs that cross a boundary; it disappears with I4's carry.
- **M6.** `luna/v2/xsec_sim.py:439`: `missed_rebalance` is never emitted for daily rebalance. The retry window `i+96` reaches the next decision, which resets `retry_until`. The daily sensitivity therefore under-reports missed fills.
- **M7.** `luna/v2/xsec_sim.py:169-173`: `pid_of` assumes one position per asset. This holds today (same-side flip orders, no partial fills), but nothing enforces it. Add an invariant assert.
- **M8.** `luna/v2/xsec.py:231-238`: history counts include days made of forward-filled bars, so a halted asset still "has history". This is harmless with the real-bar-at-stamp rule; consider counting real days.
- **M9.** `luna/research/tradelists.py:298`: `order_model` is now always part of the cache key, so every existing V1 trade-list cache misses once. This is behaviour-neutral, just a one-time rebuild.
- **M10.** Task 12 step 4 expects "the loader assertion was logged", but `load_v2_inputs` logs nothing. Add the per-account max bar and funding timestamps to the log and `summary.json` (e.g. `data_last_bar`) as auditable isolation evidence.
- **M11.** Two plan-named tests, `test_v1_simulate_unchanged_by_extensions` and `test_flatten_at_end_default_false_keeps_v1`, are not present under those names. The unchanged V1 suite covers the default path, so this is a traceability gap only.
- **M12.** §6 "Exposure izleme: zaman içinde" (exposure tracked over time) and the cross-sectional ex-ante **ES** comparison are reported only as aggregates or VaR exceedance; there is no time series or ES. A reporting-completeness gap.

## Declined to judge

- **Nested selection compares signal objective (research `r_net`) with cross-sectional objective (engine `r_multiple`).** The two R scales differ, but P9 pre-commits this.
- **Risk-grid degeneracy:** with k ≤ 2 the 30% asset cap clips both gross 1.0 and 1.5 (k=1 always). These are frozen §4.2 values.
- **HTF strategies evaluate price, trailing and time stops only at HTF decision stamps,** in both engine and kernel. The V1 2% emergency stop still runs every 15m bar. This is consistent with §3.1 ("karar yalnızca kapanmış üst-timeframe barından sonra": a decision only after a closed higher-timeframe bar) and with the equivalence tests.
- **Flat windows count as non-positive** in check 7: same as V1 `_windows`.
- **A flat window after a cross-sectional window is treated as "signal" engine,** so the book is flattened via `model_switch` rather than closed at the next rebalance. This is a defensible reading of P10.
- **R14 (orders/day 20 spot, 30 perp) applies to P6 per-bar retries** of post-only rebalance orders (SC-1 keeps R14). Could bind if maker fill rates are low; check R14 veto counts in the smoke run.
- **Survivorship bias and universe-at-train_end:** inherited from V1 and stated in the caveats.
- **Signal-family runtime:** not measured. I estimate it is minor next to cross-sectional fitting.
- **Correctness of V1 `evaluate`, `_choose_params`, `fit_expectancy`, `universe_at` and `_known_rates`:** V1 code, unchanged and out of scope.

## Assessment

**Ready to run the research? With fixes.** I found no leakage, no holdout read and no V1 regression, and the verdict wiring matches §3.5. Before the multi-hour run:
- Fix I3 (kill not flattening across windows) and I4 (the lost boundary rebalance), so the realistic and OOS simulations match the spec.
- Fix I2, I5 and I6 so a crash or resume cannot waste hours, exhaust memory or mix code versions.
- Either optimise the cross-sectional simulation (I1) or plan for a perp run of about 10 h or more at `--jobs 2`.
