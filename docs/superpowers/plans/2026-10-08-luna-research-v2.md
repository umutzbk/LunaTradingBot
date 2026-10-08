# Luna Research V2 (momentum-focused research) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer the V2 research question: is there a momentum edge, after costs, that recurs out of sample and does not hinge on one trade, one asset or one period? If so, in which family, timeframe, universe, order model and account? Produce one separate GO/NO-GO per account (perp, spot) on the development walk-forward OOS 2024-09 → 2026-07, without ever touching the holdout.

**Architecture:** Everything new lives in the `luna/v2/` package. V1 strategy classes and parameters stay unchanged (D8). Shared V1 infrastructure gets only default-preserving keyword extensions: order model, end-of-window flatten and two extra bar-stat columns.
- **TSMOM and breakout.** These strategies compute signals on causally resampled 1h/4h/1d bars. They emit a *sparse* `SignalFrame` indexed by each higher-timeframe bar's decision stamp, which is the open time of its last 15m bar. Because of that, the existing 15m engine, research kernel, trade lists and ranker run them unchanged: decisions are made only at higher-timeframe closes and fills happen at the next 15m open.
- **Cross-sectional momentum.** This is a weekly-rebalanced portfolio, so it gets its own bar-by-bar simulator. The simulator reuses V1 `Portfolio`, `SimBroker`, `risk.engine.check` and R3/R4, under the portfolio risk engine of spec §4.2.
- **Selection and evaluation.** Every configuration (account × family × timeframe × universe) is fitted train-only per window. The account's single candidate is a nested, train-only selection across configurations. That candidate gets the V1 GO/NO-GO; robustness diagnostics are reported next to it and never change the verdict.

**Tech Stack:** Python 3.12, uv (set `UV_SYSTEM_CERTS=1`), pandas, numpy, numba (existing kernel), plotly, typer, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-06-luna-research-v2-design.md`. It is FROZEN (rev 3, tag `research-v2-spec-frozen`). Section numbers (§) and decisions (D1–D8) below refer to it. The V1 spec `docs/superpowers/specs/2026-10-06-luna-trading-bot-design.md` still governs everything V2 does not change.

## Global Constraints

- **Holdout (D7, §3.4):** `V2_DATA_END = 2026-07-01T00:00:00Z`.
  - No V2 loader may return a bar that closes after it (bar open > `V2_DATA_END - 15min`) or a funding rate stamped at or after it.
  - No V2 module imports or calls `run_holdout`, `holdout_report` or the `luna holdout` command.
  - Never run `luna holdout`, never create `var/holdout_registry.json`.
- **Evaluation-set names (§11):** reports call the OOS set "development walk-forward OOS (2024-09 → 2026-07)". The final generalization set 2026-07 → 2026-10 is never read; no V2 report contains a metric about it.
- **No result-driven changes:** spec criteria and every pre-committed value in this plan (grids, caps, stops, thresholds) are fixed once the plan is approved. After any V2 result is seen they change only through a new spec-change record approved by the user.
- **D8:** V1 strategy classes (`luna/strategy/*`) and V1 grids are not edited.
  - Shared modules may gain keyword arguments whose default reproduces V1 exactly.
  - The full V1 suite (525 tests at `aa41441`) must pass after every task.
- **D6:** no mean-reversion or pairs work in V2.
- **Walk-forward:** 6 months train / 2 months test / step 2, contiguous test windows, carried state, history start `2023-01-01`, `holdout_months = 0` with `data_end = V2_DATA_END`. These are V1's 18 windows; 11 are active from 2024-09.
- **Selection:** parameters, timeframe, universe, `k`, lookback and the risk grid are all chosen on train trades only (`entry_ts >= train_start`, `exit_ts < train_end`).
- **Ranker acceptance (D3):** `n >= 30`, hierarchical shrinkage `n0 = 50`, day-block bootstrap 90% lower bound > 0 (`settings.ranking`).
- **GO/NO-GO (§3.5):** V1 `luna.report.gonogo.evaluate` unchanged, per account, never merged.
  - The naive bar is **V1 naive momentum** on the same account and the same OOS span. On spot it is long-only, as V1 already runs it there.
  - Robustness outputs are diagnostics, never a veto.
- **Paper bots:** do not rebuild or restart the `bot-perp` / `bot-spot` / `dashboard` containers during V2. They keep running the V1 image.
- **Memory:** this PC has run out of memory before (Windows commit limit). Research runs default to `--jobs 2` worker processes.
- **Commands:** `UV_SYSTEM_CERTS=1 uv run pytest …`, `uv run ruff check .`. Work on branch `feat/research-v2`, created from `feat/luna-core` at `aa41441`.
- **Commits:** one commit per task, conventional message, ending with:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  ```

## Pre-committed decisions (approved together with this plan)

The spec leaves these to the plan (V2-2 "timeframe başına grid", §3.3 selection, §4.2 mechanics). They are fixed from approval on.

| # | Decision | Value |
|---|---|---|
| P1 | TSMOM grid (EMA cross, fixed price stop, exit on trend flip) | (fast, slow) ∈ {(1d, 5d), (4d, 20d), (12d, 60d)} × stop_atr ∈ {2, 3}. A point is dropped if `fast` < 3 bars of the timeframe. Sizes: 15m/1h/4h: 6 each; 1d: 4. The 15m point (1d, 5d, 3) **is** V1 naive momentum (EMA 96/480, ATR 96, 3×ATR, 2,880-bar hold). |
| P2 | Breakout grid (Donchian N close-break, k×ATR initial stop, ATR trailing exit, no squeeze filter) | N ∈ {2d, 10d, 30d} × k_atr ∈ {2, 3}. Dropped if N < 3 bars of the timeframe. Sizes: 15m/1h/4h: 6 each; 1d: 4. |
| P3 | Lengths shared by P1/P2 | ATR window = max(1 day, 14 bars) of the timeframe. Time stop = max(30 days, 2 × slow/N), counted in 15m bars. No regime gate (regime column "NONE"). |
| P4 | Cross-sectional grid | Lookback ∈ {14d, 30d, 90d} on daily closes × k ∈ {1, 2}. Perp effective k = min(k, ⌊n/2⌋) long and short, equal raw weight gross/(2k). Spot effective k = min(k, n) long, equal weight 1/k. Perp × risk grid {gross 1.0, 1.5} × {vol 10%, 15%} → 24 points. Spot → 6 points. |
| P5 | Rebalance clock | Weekly: decision on the 15m bar opening Sunday 23:45 UTC (the 1d bar's decision stamp); fill on Monday 00:00. Daily sensitivity: every 23:45. Eligible assets: in the window's universe, real bar at the decision, at least max(lookback, 30) daily closes. |
| P6 | Cross-sectional execution | Main order model: perp taker IOC within the price band, spot maker post-only. Unfilled rebalance orders are re-planned at the current mid on every following bar for at most 96 bars, then dropped until the next rebalance and recorded as missed. Catastrophic-stop exits are always IOC and retry until filled. |
| P7 | Catastrophic stop (§4.2) | Checked at every 15m close per period leg: `leg net P&L since the period start + open MTM <= -1.5% × equity` → IOC close at the next bar open. The asset is blocked until the next weekly rebalance. |
| P8 | Cross-sectional "trade" | One trade per (rebalance period, asset).<br>• A leg carried with the same sign is re-referenced at the rebalance fill bar's open: an equity-neutral internal transfer, no order, no cost.<br>• The period's delta order costs are booked to the new period's leg, or to the ending leg when the target is 0. A sign flip is a close plus a new open.<br>• `risk_amount = 1.5% × equity + round_trip × |notional|`; `risk_precap` uses the pre-projection notional. |
| P9 | Account candidate (§3.3) | Nested and train-only, per window. Among all primary configurations whose model is accepted (signal: at least one accepted ranker cell; cross-sectional: the portfolio cell passes the D3 rule on train leg-trades), pick the highest train objective. Ties go to the smaller config id. No accepted config → flat window.<br>Objective: signal = V1 `_choose_params` value (mean train `r_net`, perp worst-case unknown-funding rule); cross-sectional = mean train R of the chosen grid point. |
| P10 | Engine switch | When consecutive candidate windows use different engines (signal ↔ cross-sectional), the outgoing engine flattens at its last bar. Each leg is booked at the worse of that bar's open and close, with taker frictions and fees; exit reason `model_switch`. Signal → signal switches carry positions to their owning strategy as V1 does. |
| P12 | Universes (§2.3) | The V1 rule at each window's `train_end`: ADV ≥ $10M and ≥ 548 days of history; on spot, ADV comes from the perp panel and the universe is restricted to assets with spot bars, exactly as V1's spot walk-forward. `btc_eth` = that universe ∩ {BTC, ETH}. |
| P11 | Variants | Every configuration: `realistic`, `edge` (kill switch off).<br>Candidate: `realistic`, `edge`, `cost_2x`, `cost_2x_edge`, `order_alt_edge` (perp maker-only / spot taker-only); perp only: `funding_zero`, `funding_estimated`, `funding_estimated_x2` (edge).<br>Robustness reads `edge`. Asset leave-one-out re-runs the config's (or the candidate's nested) walk-forward without that asset. |

## Spec-change record required before Task 5: SC-1 (needs your approval)

The frozen spec says V1 R1–R19 stay "aynen" at order level for the cross-sectional book (§4.2). With V1's values that makes the book impossible:
- **R5** (per-trade risk ≤ 1%): a leg's planned risk is 1.5% plus costs by §4.2's own definition, so every leg is vetoed.
- **R6** (open risk ≤ 3%): allows only 2 such legs.
- **R7** (at most 3 positions): blocks 2k ≥ 4 legs.
- **R9** (spot leg notional ≤ 50%): conflicts with §4.3's 40% asset cap.
- **R10:** treats every rebalance resize as pyramiding.

§4.3 also gives spot top-k no stop, while R11 requires one.

**Recommended SC-1:**
- For cross-sectional orders only, R5/R6/R7/R9 are set non-binding and rebalance resizes are not flagged as pyramiding. They are replaced by the §4.2/§4.3 controls, which the projection enforces before any order exists.
- Every other rule keeps its V1 value: R0–R4, R8 at the V1 gross cap, R10 close-validity, R11–R19.
- Spot top-k legs use the same 1.5% per-asset catastrophic stop, so R11 has a stop to check.
- TSMOM and breakout are unaffected.

**If SC-1 is rejected,** the cross-sectional family is reported as "not executable under V1 limits" and V2 continues with TSMOM and breakout only. Tasks 5–6 would then shrink to the projection functions without the simulator.

## Review Focus

1. **The store gains bars after 2026-07-01** (the data store is live and keeps growing). Expect the V2 loader to truncate them and to raise `HoldoutLeak` if anything slips through. Test: Task 1 `test_loader_never_returns_bars_after_cutoff`.
2. **Asset with a forward-filled or missing bar at a rebalance, or too little history.** Expect it to be excluded from ranking, get no order on the filled bar and no NaN weight. Test: Task 6 `test_xsec_skips_assets_without_real_bar_or_history`.
3. **Universe smaller than 2k (perp) or empty in a window.** Expect k to be clipped and the window to run flat without crashing; carried legs close at the first rebalance. Test: Task 6 `test_k_clipped_and_empty_universe_runs_flat`.
4. **Covariance NaN or singular, or equity ≤ 0, at projection.** Expect the projection to fail closed (all-zero weights plus a `cov_unavailable` event), never NaN orders. Test: Task 5 `test_projection_fails_closed_on_nan_cov_or_bad_equity`.
5. **Candidate switches engine mid-walk-forward with open positions.** Expect flatten with `model_switch`, an equity identity that holds across the switch, and no position left in the other engine. Test: Task 10 `test_selected_walkforward_engine_switch_flattens`.

---

### Task 1 (V2-1): Holdout isolation + causal resampler

**Files:**
- Create: `luna/v2/__init__.py`, `luna/v2/data.py`, `luna/v2/timeframe.py`
- Test: `tests/v2/__init__.py`, `tests/v2/conftest.py`, `tests/v2/test_data.py`, `tests/v2/test_timeframe.py`

**Interfaces:**
- Consumes: `luna.report.walkforward.load_inputs(settings, store, account, instruments, *, data_end, assets) -> Inputs`, `luna.engine.walkforward.make_windows`, `Window`.
- Produces:
  - `luna.v2.data`: `V2_DATA_END: pd.Timestamp`, `class HoldoutLeak(RuntimeError)`, `assert_before_cutoff(panel: dict[str, pd.DataFrame], funding: dict[str, pd.DataFrame], cutoff: pd.Timestamp = V2_DATA_END) -> None`, `v2_settings(settings: Settings) -> Settings` (walkforward.holdout_months = 0), `load_v2_inputs(settings, store, account, instruments, *, assets: list[str] | None = None) -> Inputs`, `v2_windows(settings: Settings) -> list[Window]`.
  - `luna.v2.timeframe`: `Timeframe = Literal["15m", "1h", "4h", "1d"]`, `TF: dict[Timeframe, pd.Timedelta]`, `bars_for(duration: pd.Timedelta, tf: Timeframe) -> int`, `resample_causal(bars: pd.DataFrame, tf: Timeframe) -> pd.DataFrame`.
  - `tests/v2/conftest.py`: fixture `v2_panel` (dict of 6 synthetic 15m frames "BTC","ETH","AAA","BBB","CCC","DDD"; 120 days each, built with `tests.helpers.synthetic_bars`; "DDD" listed on day 60; a few `filled=True` bars) and `v2_settings_small` (train 2 months, test 1 month, step 1, holdout 0, `min_history_days=20`, `min_adv_usd=0`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/v2/test_timeframe.py
def test_bars_for_exact_and_rejects_fractions():
    assert bars_for(pd.Timedelta(days=1), "15m") == 96 and bars_for(pd.Timedelta(days=5), "4h") == 30
    with pytest.raises(ValueError):
        bars_for(pd.Timedelta(hours=1), "4h")

def test_resample_aggregates_ohlcv(v2_panel):
    out = resample_causal(v2_panel["BTC"], "1h")
    src = v2_panel["BTC"].iloc[:4]                      # first full hour
    row = out.iloc[0]
    assert out.index[0] == src.index[-1]               # stamp = open time of the LAST 15m bar
    assert row.open == src.open.iloc[0] and row.close == src.close.iloc[-1]
    assert row.high == src.high.max() and row.low == src.low.min()
    assert row.volume == pytest.approx(src.volume.sum()) and row.trades == src.trades.sum()
    assert list(out.columns) == ["open", "high", "low", "close", "volume", "trades", "filled"]

def test_resample_drops_incomplete_buckets(v2_panel):
    df = v2_panel["BTC"].iloc[:4 * 24 + 2]            # one day + 2 bars
    assert len(resample_causal(df, "1h")) == 24 and len(resample_causal(df, "1d")) == 0

def test_resample_filled_only_when_all_filled(): ...   # 3 filled + 1 real -> False; 4 filled -> True

@pytest.mark.parametrize("tf", ["1h", "4h", "1d"])
def test_resample_is_causal(v2_panel, tf):
    df = v2_panel["AAA"]; full = resample_causal(df, tf)
    for k in (5, 97, 400, 2000, len(df) - 1):
        part = resample_causal(df.iloc[:k], tf)
        pd.testing.assert_frame_equal(part, full[full.index <= df.index[k - 1]])

# tests/v2/test_data.py
def test_v2_windows_end_at_cutoff(settings):
    w = v2_windows(settings)
    assert w[0].test_start == pd.Timestamp("2023-07-01", tz="UTC")
    assert w[-1].test_end == V2_DATA_END and len(w) == 18

def test_assert_before_cutoff_raises_on_late_bar_or_funding(): ...  # bar at 2026-06-30 23:45 ok; 2026-07-01 00:00 raises; funding at cutoff raises

def test_loader_never_returns_bars_after_cutoff(tmp_path, settings):
    # BarStore with BTC bars 2026-06-29 .. 2026-07-03 and funding through 2026-07-02
    inp = load_v2_inputs(settings, store, "perp", instruments)
    assert inp.panel["BTC"].index.max() == pd.Timestamp("2026-06-30 23:45", tz="UTC")
    assert inp.funding["BTC"].index.max() < V2_DATA_END

def test_v2_package_never_references_holdout():
    src = "\n".join(p.read_text() for p in Path("luna/v2").rglob("*.py"))
    assert not re.search(r"run_holdout|holdout_report|luna holdout|holdout_registry", src)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/v2/test_timeframe.py tests/v2/test_data.py -v`
Expected: FAIL (`ModuleNotFoundError: luna.v2`).

- [ ] **Step 3: Implement `luna/v2/timeframe.py` and `luna/v2/data.py`**

`resample_causal` buckets are `index.floor(TF[tf])`, aligned to UTC midnight. A bucket is emitted only if all `TF/15min` of its rows are present. Its stamp is `bucket + TF - 15min`. `filled` = all rows filled. `"15m"` returns a copy of the input.

`load_v2_inputs` calls `load_inputs(..., data_end=V2_DATA_END)` and then `assert_before_cutoff(inp.panel, inp.funding)` (which also checks `inp.universe_panel`). The leak rule is `index > cutoff - 15min` for bars and `index >= cutoff` for funding.

`v2_windows` = `make_windows(history_start, V2_DATA_END, v2_settings(s).walkforward)[0]`; assert that the last `test_end <= V2_DATA_END`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/v2 -v` → PASS. Run `UV_SYSTEM_CERTS=1 uv run pytest -q` → 525 + new passed.

- [ ] **Step 5: Commit**

```bash
git add luna/v2 tests/v2
git commit -m "feat(v2): holdout cutoff enforcement and causal 15m resampler (V2-1)"
```

---

### Task 2 (V2-2): Time-scaled parameter grids

**Files:**
- Create: `luna/v2/params.py`
- Test: `tests/v2/test_params.py`

**Interfaces:**
- Consumes: `bars_for`, `Timeframe` (Task 1).
- Produces:
  - Constants: `MIN_BARS = 3`, `TSMOM_SPANS: tuple[tuple[pd.Timedelta, pd.Timedelta], ...]`, `STOP_ATR = (2.0, 3.0)`, `BREAKOUT_N: tuple[pd.Timedelta, ...]`, `XSEC_LOOKBACK_DAYS = (14, 30, 90)`, `XSEC_K = (1, 2)`, `RISK_GRID = ({"gross": 1.0, "vol_target": 0.10}, {"gross": 1.0, "vol_target": 0.15}, {"gross": 1.5, "vol_target": 0.10}, {"gross": 1.5, "vol_target": 0.15})`, `FAMILY_TIMEFRAMES: dict[AccountKind, dict[str, tuple[Timeframe, ...]]]` (perp: tsmom/breakout 15m, 1h, 4h, 1d; xsec 1d. Spot: tsmom/breakout 1h, 4h, 1d; xsec 1d).
  - Functions: `atr_bars(tf) -> int`, `max_hold_bars(lookback: pd.Timedelta) -> int` (15m bars), `tsmom_grid(tf) -> list[dict]` (keys `fast, slow, atr_n, stop_atr, max_hold`), `breakout_grid(tf) -> list[dict]` (keys `donchian, atr_n, k_atr, max_hold`), `xsec_grid(kind) -> list[dict]` (perp keys `lookback_days, k, gross, vol_target`; spot keys `lookback_days, k`).

- [ ] **Step 1: Write the failing tests**

```python
def test_tsmom_15m_contains_v1_naive_momentum():
    assert {"fast": 96, "slow": 480, "atr_n": 96, "stop_atr": 3.0, "max_hold": 2880} in tsmom_grid("15m")

@pytest.mark.parametrize(("tf", "n"), [("15m", 6), ("1h", 6), ("4h", 6), ("1d", 4)])
def test_grid_sizes(tf, n):
    assert len(tsmom_grid(tf)) == n and len(breakout_grid(tf)) == n

def test_atr_and_hold_rules():
    assert [atr_bars(t) for t in ("15m", "1h", "4h", "1d")] == [96, 24, 14, 14]
    assert max_hold_bars(pd.Timedelta(days=5)) == 2880 and max_hold_bars(pd.Timedelta(days=60)) == 11520

def test_xsec_grid_sizes_and_risk_grid_is_frozen():
    assert len(xsec_grid("perp")) == 24 and len(xsec_grid("spot")) == 6
    assert {(g["gross"], g["vol_target"]) for g in xsec_grid("perp")} == {(1.0, .10), (1.0, .15), (1.5, .10), (1.5, .15)}

def test_spot_has_no_15m():
    assert "15m" not in FAMILY_TIMEFRAMES["spot"]["tsmom"]
```

- [ ] **Step 2: Run tests to verify they fail** — `UV_SYSTEM_CERTS=1 uv run pytest tests/v2/test_params.py -v` → FAIL (import error).
- [ ] **Step 3: Implement `luna/v2/params.py`** with the P1–P4 values. A grid point is dropped when its shortest length is under `MIN_BARS` bars of the timeframe.
- [ ] **Step 4: Run tests to verify they pass** — same command → PASS.
- [ ] **Step 5: Commit** — `git add luna/v2/params.py tests/v2/test_params.py && git commit -m "feat(v2): time-scaled TSMOM/breakout/cross-sectional grids (V2-2)"`

---

### Task 3 (V2-3): TSMOM family (perp + spot)

**Files:**
- Create: `luna/v2/strategies.py`
- Test: `tests/v2/test_tsmom.py`

**Interfaces:**
- Consumes:
  - `resample_causal`, `TF` (Task 1); `tsmom_grid` (Task 2).
  - `GridStrategy`, `SignalFrame` (`luna.strategy.base`); `NaiveMomentum._stop`, `NaiveMomentum._stop_hit`; `ema`, `atr` (`luna.features.core`).
- Produces:
  - `make_tsmom(tf: Timeframe) -> type[GridStrategy]`, cached per tf. The class is defined at module level via `type()` with `__qualname__ = f"TSMOM_{tf}"`. It has `name = f"tsmom_{tf}"`, `GRID = tsmom_grid(tf)`, `_defaults = GRID[0]`, `TF = tf` and `params_id = f"F{fast}_S{slow}_k{stop_atr:g}"`.
  - `signals()` resamples each key to `TF`, computes the EMA cross / ATR on the resampled bars, and returns a `SignalFrame` indexed by the resampled decision stamps:
    - `entry` = sign change of `sign(EMA_fast - EMA_slow)`, as in `NaiveMomentum`;
    - `exit_long = state < 0`, `exit_short = state > 0`;
    - `stop_distance = stop_atr * ATR / close`, `stop_kind = "price"`;
    - `ExitSpec(max_hold_bars=max_hold)`, regime `"NONE"`, `ref = close`.
  - Exit hooks are the very same functions as the naive momentum ones: `_stop = staticmethod(NaiveMomentum._stop)` and `_stop_hit = staticmethod(NaiveMomentum._stop_hit)`. That way `luna.research.tradelists._exit_rule` maps them to `STOP_PRICE` without any edit.
  - `V2_STRATEGY_CLASSES(kind) -> dict[str, type]` (name → class for every family × timeframe of `FAMILY_TIMEFRAMES[kind]`); Task 4 extends it with breakout.

- [ ] **Step 1: Write the failing tests**

```python
def test_tsmom_15m_v1_point_equals_naive_momentum(v2_panel):
    T = make_tsmom("15m"); s = T({"fast": 96, "slow": 480, "atr_n": 96, "stop_atr": 3.0, "max_hold": 2880})
    a = s.signals(v2_panel, ["BTC"])["BTC"]; b = NaiveMomentum().signals(v2_panel, ["BTC"])["BTC"]
    for f in ("entry", "exit_long", "exit_short", "stop_distance", "ref"):
        pd.testing.assert_series_equal(getattr(a, f), getattr(b, f), check_names=False)

def test_tsmom_htf_signals_only_on_decision_stamps(v2_panel):
    sf = make_tsmom("4h")(None).signals(v2_panel, ["BTC"])["BTC"]
    assert ((sf.entry.index + pd.Timedelta("15min")).hour % 4 == 0).all()
    assert ((sf.entry.index + pd.Timedelta("15min")).minute == 0).all()

def test_tsmom_signals_causal(v2_panel): ...   # signals on panel cut at bar k == full signals restricted to <= cut

def test_tsmom_exit_rule_is_stop_price():
    sf = ...; assert _exit_rule(make_tsmom("1h")(None), sf)[0] == STOP_PRICE

@pytest.mark.parametrize(("tf", "kind"), [("1h", "perp"), ("4h", "perp"), ("1h", "spot"), ("1d", "spot")])
def test_tsmom_tradelist_matches_backtest(v2_panel, tf, kind):
    # V1 pattern (tests/research/test_tradelists.py::test_tradelist_matches_backtest_single_strategy):
    # permissive engine settings, AcceptAllRanker, same entry_ts/exit_ts/direction/exit_reason,
    # ret_net == engine pnl_net / gross notional (atol 1e-9), at least 5 trades
```

- [ ] **Step 2: Run tests to verify they fail** — `UV_SYSTEM_CERTS=1 uv run pytest tests/v2/test_tsmom.py -v` → FAIL.
- [ ] **Step 3: Implement `make_tsmom` / `V2_STRATEGY_CLASSES` in `luna/v2/strategies.py`.** The `use_regime` constructor argument is accepted and ignored (there is no regime gate).
- [ ] **Step 4: Run tests to verify they pass** — same command → PASS. Full suite → green.
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): multi-timeframe TSMOM strategy with engine equivalence (V2-3)"`

---

### Task 4 (V2-4): Volatility breakout family (perp + spot)

**Files:**
- Modify: `luna/v2/strategies.py`
- Test: `tests/v2/test_breakout.py`

**Interfaces:**
- Consumes:
  - Task 3 module and helpers; `breakout_grid` (Task 2).
  - `Momentum._stop`, `Momentum._stop_exit` (trailing semantics, `luna.strategy.momentum`).
- Produces:
  - `make_breakout(tf) -> type[GridStrategy]` with `name = f"breakout_{tf}"`, `params_id = f"N{donchian}_k{k_atr:g}"`.
  - Signal rules:
    - `up = close > rolling(high, N).max().shift(1)` and `dn = close < rolling(low, N).min().shift(1)` on resampled bars;
    - `entry = edge_trigger(up | dn, where(up, 1, -1))`; exit flags are never set;
    - `stop_distance = k_atr * ATR / close`;
    - `ExitSpec(max_hold_bars=max_hold, trailing_atr_mult=k_atr)`.
  - `_stop = Momentum._stop`, `_stop_exit = Momentum._stop_exit` (same function objects → `STOP_TRAIL`).
  - `V2_STRATEGY_CLASSES` now includes the `breakout_*` classes.

- [ ] **Step 1: Write the failing tests**

```python
def test_breakout_entry_on_close_break_of_previous_n(v2_panel): ...  # hand-built HTF bars, break at row j fires +1 at j only
def test_breakout_exit_rule_is_trailing(): assert _exit_rule(make_breakout("1h")(None), sf)[0] == STOP_TRAIL
@pytest.mark.parametrize(("tf", "kind"), [("1h", "perp"), ("4h", "perp"), ("4h", "spot")])
def test_breakout_tradelist_matches_backtest(v2_panel, tf, kind): ...  # same assertions as Task 3; "trailing" in exit reasons
def test_v2_strategy_classes_per_account():
    assert set(V2_STRATEGY_CLASSES("spot")) == {f"{f}_{t}" for f in ("tsmom", "breakout") for t in ("1h", "4h", "1d")}
    assert len(V2_STRATEGY_CLASSES("perp")) == 8
```

- [ ] **Step 2: Run tests to verify they fail** — `UV_SYSTEM_CERTS=1 uv run pytest tests/v2/test_breakout.py -v` → FAIL.
- [ ] **Step 3: Implement `make_breakout`.**
- [ ] **Step 4: Run tests to verify they pass** — PASS; full suite green.
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): multi-timeframe volatility breakout with engine equivalence (V2-4)"`

---

### Task 5 (V2-5): Portfolio risk engine (requires SC-1 approval)

**Files:**
- Create: `docs/superpowers/specs/2026-10-08-research-v2-spec-change-1.md` (the SC-1 text above, approval date and approver; it amends §4.2/§4.3 only)
- Create: `luna/v2/portfolio_risk.py`
- Test: `tests/v2/test_portfolio_risk.py`

**Interfaces:**
- Consumes: `RiskLimits` (`luna.config`).
- Produces:
  - Constants: `NET_CAP = 0.10`, `ASSET_CAP = {"perp": 0.30, "spot": 0.40}`, `CORR_THRESHOLD = 0.8`, `CORR_GROUP_CAP = 0.50`, `VAR_BUDGET = 0.015`, `VAR_Z95 = 1.6448536269514722`, `CAT_STOP = 0.015`, `COV_SPAN_DAYS = 30`, `CORR_DAYS = 30`, `DAYS_PER_YEAR = 365`.
  - `ewma_cov(daily_logret: pd.DataFrame) -> pd.DataFrame`: EWMA span 30, `min_periods = 30`, value at the last row.
  - `trailing_corr(daily_logret: pd.DataFrame) -> pd.DataFrame`: last 30 rows.
  - `@dataclass(frozen=True) class Projection: weights: pd.Series; vol_scale: float; var_95: float; reason: str` (`""` or `"cov_unavailable"`).
  - `project_weights(raw: pd.Series, cov: pd.DataFrame, corr: pd.DataFrame, *, controls: Literal["perp", "spot"], gross: float, vol_target: float | None) -> Projection`.
  - `catastrophic_stop_hit(leg_pnl: float, equity: float) -> bool`.
  - `xsec_limits(limits: RiskLimits) -> RiskLimits`: the SC-1 profile, a copy with `max_risk_per_trade = 1.0`, `max_open_risk = 1e9`, `max_positions = 10_000`, `max_leg_notional_frac = 1.0`; everything else unchanged.

- [ ] **Step 1: Write the SC-1 record file** (only after the user approves SC-1; copy the "Recommended SC-1" paragraph verbatim, add approval date) and commit it alone: `git commit -m "docs: Research V2 spec-change record SC-1 (cross-sectional order-level limits)"`.

- [ ] **Step 2: Write the failing tests**

```python
@given-like loop over 200 seeded random cases (numpy rng, n assets 2..10, random raw ±, random PSD cov):
def test_perp_projection_respects_every_cap():
    p = project_weights(raw, cov, corr, controls="perp", gross=g, vol_target=v)
    w = p.weights
    assert (w.abs() <= 0.30 + 1e-12).all() and w.abs().sum() <= g + 1e-12
    assert abs(w.sum()) <= 0.10 + 1e-12
    assert VAR_Z95 * sqrt(w @ cov @ w) <= 0.015 + 1e-12
    assert sqrt(w @ cov @ w) * sqrt(365) <= v + 1e-12
    for group in same-side corr>0.8 components: assert w[group].abs().sum() <= 0.50 + 1e-12

def test_projection_only_scales_down():  assert (p.weights.abs() <= raw.abs() + 1e-12).all()
def test_vol_scale_capped_at_one():      # tiny cov -> vol_scale == 1.0
def test_net_cap_shrinks_heavier_side(): # raw longs 0.3,0.3 shorts -0.1 -> net exactly 0.10, shorts untouched
def test_var_budget_binds():             # huge cov -> var_95 == 0.015
def test_spot_projection_long_only_cap_040():
    p = project_weights(pd.Series({"A": .5, "B": .5}), cov, corr, controls="spot", gross=1.0, vol_target=None)
    assert p.weights.to_dict() == {"A": .4, "B": .4}
def test_projection_fails_closed_on_nan_cov_or_bad_equity():
    assert (project_weights(raw, cov_with_nan, corr, ...).weights == 0).all() and p.reason == "cov_unavailable"
def test_catastrophic_stop_threshold():
    assert catastrophic_stop_hit(-1.5, 100.0) and not catastrophic_stop_hit(-1.49, 100.0) and not catastrophic_stop_hit(-5, 0.0)
def test_xsec_limits_only_relax_r5_r6_r7_r9(settings):
    v1 = settings.accounts["perp"]; x = xsec_limits(v1)
    for f in ("daily_loss_limit", "max_drawdown", "max_gross_leverage", "max_price_band", "max_orders_per_day",
              "liq_distance_mult", "allow_short", "live_enabled", "emergency_stop_frac"):
        assert getattr(x, f) == getattr(v1, f)
def test_section12_consistency(settings):
    assert CAT_STOP < settings.accounts["perp"].daily_loss_limit and VAR_BUDGET < settings.accounts["perp"].daily_loss_limit
```

- [ ] **Step 3: Run tests to verify they fail** — `UV_SYSTEM_CERTS=1 uv run pytest tests/v2/test_portfolio_risk.py -v` → FAIL.

- [ ] **Step 4: Implement `luna/v2/portfolio_risk.py`.** The projection order is fixed. Steps 4–5 are uniform down-scalings, so they keep steps 1–3 satisfied:

```python
# perp: 1 asset cap -> 2 correlation groups -> 3 net cap -> 4 vol target -> 5 VaR budget
w = raw.clip(-ASSET_CAP["perp"], ASSET_CAP["perp"])
for side in (1, -1):        # connected components of same-side assets with corr > 0.8 (NaN = uncorrelated)
    for g in components(side):
        s = w[g].abs().sum()
        if s > CORR_GROUP_CAP: w[g] *= CORR_GROUP_CAP / s
net = w.sum()
if abs(net) > NET_CAP:      # heavier side H, lighter L (absolute sums): scale heavier by (L + NET_CAP) / H
    ...
sd = sqrt(w @ cov @ w)      # daily; non-finite or w has assets missing from cov -> fail closed
w *= (scale := min(1.0, vol_target / sqrt(DAYS_PER_YEAR) / sd)) if sd > 0 else 1.0
var = VAR_Z95 * sqrt(w @ cov @ w)
if var > VAR_BUDGET: w *= VAR_BUDGET / var
# spot: clip at 0, normalise to gross 1.0, clip at ASSET_CAP["spot"]; no vol/VaR (spec 4.3)
```

- [ ] **Step 5: Run tests to verify they pass** — PASS; full suite green.
- [ ] **Step 6: Commit** — `git commit -m "feat(v2): portfolio risk projection, catastrophic stop and SC-1 limits profile (V2-5)"`

---

### Task 6 (V2-6): Cross-sectional momentum: targets + rebalance simulator

**Files:**
- Create: `luna/v2/xsec.py` (ranking and targets), `luna/v2/xsec_sim.py` (simulator)
- Test: `tests/v2/test_xsec.py`, `tests/v2/test_xsec_sim.py`

**Interfaces:**
- Consumes:
  - `resample_causal` (Task 1); `xsec_grid` (Task 2); `project_weights`, `ewma_cov`, `trailing_corr`, `catastrophic_stop_hit`, `xsec_limits`, `CAT_STOP` (Task 5).
  - V1: `Portfolio`, `Position`, `SimBroker`, `CostModel`, `risk.engine.check/OrderCheck/RiskContext/portfolio_actions/apply_actions`, `risk.state.roll_day/RiskState`, `planner.round_qty`, `engine.backtest._Market/_Funding/BacktestResult/TRADE_COLUMNS/BAR_STAT_COLUMNS/default_instruments`.
- Produces:
  - `luna.v2.xsec`:
    - `@dataclass(frozen=True) class XSecParams: lookback_days: int; k: int; gross: float = 1.0; vol_target: float | None = None; rebalance: Literal["weekly", "daily"] = "weekly"`, with property `params_id` = `f"L{lookback_days}_k{k}_g{gross:g}_v{vol_target or 0:g}" + ("_daily" if daily)`.
    - `rebalance_stamps(index: pd.DatetimeIndex, rebalance) -> pd.DatetimeIndex` (P5).
    - `eligible(daily_close: pd.DataFrame, real_at: dict[str, bool], universe: list[str], lookback_days: int) -> list[str]`.
    - `raw_targets(daily_close: pd.DataFrame, assets: list[str], params: XSecParams, kind) -> pd.Series`: rank by `close[-1] / close[-1 - L] - 1`, ties by name, k clipped per P4.
  - `luna.v2.xsec_sim`: `RETRY_BARS = 96`, `XSEC_TRADE_EXTRA = ["risk_precap", "period"]`, `XSEC_BAR_EXTRA = ["net_leverage", "max_asset_share", "vol_scale", "ex_ante_var"]`, and:

```python
def run_xsec(panel, funding, params: XSecParams, universe: list[str], kind: AccountKind, settings: Settings,
             start: pd.Timestamp, end: pd.Timestamp, *, name: str = "xsec", cost_mult: float = 1.0,
             funding_mode: FundingMode = "known_only",
             instruments: dict[str, Instrument] | None = None, initial_portfolio: Portfolio | None = None,
             initial_state: RiskState | None = None, flatten_at_end: bool = False) -> BacktestResult
```
  Task 7 adds `order_model: OrderModel | None = None`; until then orders use the account default (perp IOC, spot post-only).
  It returns a V1 `BacktestResult`:
  - `trades` has `TRADE_COLUMNS + XSEC_TRADE_EXTRA`, with `exit_reason` ∈ {`rebalance`, `catastrophic_stop`, `kill_switch`, `model_switch`};
  - `candidates` is empty with `CANDIDATE_COLUMNS`;
  - `bar_stats` has `BAR_STAT_COLUMNS + XSEC_BAR_EXTRA`;
  - `risk_events` holds vetoes, stops, `cov_unavailable` and `missed_rebalance`;
  - `portfolio`, `risk_state` and `initial_equity` are set as in V1; `pending_exits = None`.

  Per bar `i`, in this order:
  1. On a rebalance fill bar, re-reference same-sign carried legs: transfer fills with fee 0 and price = ref = the bar's open (P8).
  2. Broker fills.
  3. Funding at the hour boundary (perp).
  4. Mark, then `roll_day` / `portfolio_actions` / `apply_actions`. A kill sets every target to 0.
  5. Catastrophic stops (P7).
  6. At a rebalance stamp: eligible assets → `raw_targets` → `project_weights` (cov/corr from daily log returns up to the decision) → delta orders, all through `risk.check` with `xsec_limits`.
     - Intents: reduce/close for shrinking legs; `open` with `increases_abs_position=False` for new or grown legs.
     - `stop_distance_frac = CAT_STOP * equity / target_notional`.
  7. Retries of unfilled deltas (P6).
  8. Bar stats.

- [ ] **Step 1: Write the failing tests**

```python
# tests/v2/test_xsec.py
def test_weekly_stamps_are_sunday_2345(): s = rebalance_stamps(idx, "weekly"); assert (s.dayofweek == 6).all() and (s.strftime("%H:%M") == "23:45").all()
def test_raw_targets_rank_ties_and_signs(): ...   # perp k=1 of {A:+5%,B:+5%,C:-3%}: A long (tie by name), C short; weights +-gross/2
def test_k_clipped_and_empty_universe_runs_flat(v2_panel, settings):
    assert raw_targets(close, ["BTC", "ETH", "AAA"], XSecParams(30, 2, 1.0, .10), "perp").abs().gt(0).sum() == 2
    res = run_xsec(v2_panel, {}, XSecParams(30, 2, 1.0, .10), [], "perp", settings, start, end)
    assert res.trades.empty and res.equity.iloc[-1] == res.initial_equity
def test_spot_targets_long_only(): assert (raw_targets(..., "spot") >= 0).all()

# tests/v2/test_xsec_sim.py
def test_xsec_equity_identity(v2_panel, settings):
    res = run_xsec(...)
    open_unreal = sum(...)   # from res.portfolio positions marked at last close
    assert res.equity.iloc[-1] - res.initial_equity == pytest.approx(res.trades.pnl_net.sum() + open_unreal, abs=1e-6)
def test_rebalance_fills_on_next_bar_open(): ...   # every entry_ts == stamp + 15min, fill price within band of that open
def test_one_trade_per_period_leg(): ...           # n closed trades == sum over closed periods of legs held
def test_carried_leg_rereferenced_without_cost(): ...  # transfer contributes fees 0 and spread_slip 0
def test_catastrophic_stop_closes_leg_and_blocks_until_next_rebalance(): ...  # shocked asset: exit_reason catastrophic_stop, no order for it until next stamp
def test_daily_loss_blocks_rebalance_opens(): ...  # R3 vetoes opens that day; reduces still pass
def test_drawdown_kill_flattens_in_realistic_not_in_edge(): ...
def test_xsec_skips_assets_without_real_bar_or_history(): ...  # filled bar at the stamp / 10 days history -> no target, no order
def test_xsec_is_causal(v2_panel): ...   # run to T on full panel == run to T on panel cut at T (trades, equity)
def test_flatten_at_end_books_model_switch(): ...  # every open leg closed, exit_reason model_switch, portfolio flat
def test_spot_rebalance_buys_wait_for_cash(): ...   # buys vetoed by R17 cash retry on later bars after sells fill
```

- [ ] **Step 2: Run tests to verify they fail** — `UV_SYSTEM_CERTS=1 uv run pytest tests/v2/test_xsec.py tests/v2/test_xsec_sim.py -v` → FAIL.
- [ ] **Step 3: Implement `luna/v2/xsec.py`.**
- [ ] **Step 4: Implement `run_xsec` in `luna/v2/xsec_sim.py`.**
  - Each period leg is a V1 `Position` with id `f"{name}:{asset}:{period:%Y%m%d}"`, `legs = (Leg(asset, 1.0),)`, `exit = ExitSpec(max_hold_bars=10**9)` and `regime = "NONE"`.
  - Model-switch flatten prices each leg at the worse of the last bar's open and close with taker frictions and fee (`SimBroker.stop_fill`). On a forward-filled bar it falls back to the last close with frictions.
- [ ] **Step 5: Run tests to verify they pass** — PASS; full suite green.
- [ ] **Step 6: Commit** — `git commit -m "feat(v2): cross-sectional momentum targets and weekly rebalance simulator (V2-6)"`

---

### Task 7 (V2-7): Order model (perp maker, spot taker) + missed-fill cost

**Files:**
- Modify: `luna/types.py` (add `OrderModel`), `luna/planner.py:57-150` (`_make_order`, `plan_entry`, `plan_exit`), `luna/portfolio/constructor.py:139-178` (`construct` liquidity), `luna/engine/pipeline.py` (`Pipeline.__init__`, `flatten`, `entries`), `luna/engine/backtest.py:186` (`run_backtest`), `luna/research/tradelists.py` (`build_tradelist`, `build_all_tradelists`, `cache_key`)
- Modify: `luna/v2/xsec_sim.py` (use `order_model`)
- Create: `luna/v2/fills.py`
- Test: `tests/v2/test_order_model.py`

**Interfaces:**
- Consumes: Tasks 3, 4, 6; `luna.labeling` forced-entry replay pattern (`_Labeler._replay`).
- Produces:
  - `luna.types.OrderModel = Literal["taker", "maker"]`.
  - `luna.types.default_order_model(kind: AccountKind) -> OrderModel` (perp "taker", spot "maker").
  - New keyword `order_model: OrderModel | None = None` (None = account default) on:
    - `_make_order`, `plan_entry`, `plan_exit`;
    - `Pipeline(...)`, stored as `self.order_model`;
    - `construct(..., liquidity=None)`;
    - `run_backtest`;
    - `build_tradelist`, `build_all_tradelists`.

    Taker = IOC at `mid × (1 ± band)`; maker = POST_ONLY at `mid × (1 ∓ half_spread)`. Kernel `post_only = model == "maker"`, fee = `costs.fee_rate(model)`; `cache_key` includes `order_model`.
  - `luna.v2.fills`:
    - `maker_fill_rate(candidates: pd.DataFrame) -> float` = (filled + partial) / (filled + partial + unfilled), NaN if none.
    - `missed_fills(res: BacktestResult, models: list[WindowModel], inp: Inputs, settings: Settings, kind) -> pd.DataFrame`, columns `ts, strategy, key, direction, r_net_taker, exit_reason`. Each `unfilled` candidate is replayed alone with its window's strategy instance, entry forced at its bar, order model taker.

- [ ] **Step 1: Write the failing tests**

```python
def test_default_order_model_reproduces_v1_orders():  # plan_entry/plan_exit with order_model=None == explicit kind default, field by field
def test_perp_maker_orders_are_post_only(): o = plan_entry(c, 100, mids, inst, "perp", ts, .003, costs=cp, order_model="maker").orders[0]; assert o.tif == "POST_ONLY" and o.limit_price == mids["BTC"] * (1 - 1e-4)
def test_spot_taker_orders_are_ioc_within_band(): ...  # tif IOC, limit mid*(1+0.005)
def test_constructor_round_trip_follows_order_model(): ...  # perp maker sizing uses maker fee 0.0002
@pytest.mark.parametrize(("tf", "kind", "model"), [("1h", "perp", "maker"), ("1h", "spot", "taker")])
def test_tradelist_matches_backtest_alt_order_model(v2_panel, tf, kind, model): ...  # V1 equivalence assertions
def test_maker_fill_rate(): assert maker_fill_rate(pd.DataFrame({"status": ["filled", "unfilled", "partial", "rejected"]})) == pytest.approx(2 / 3)
def test_missed_fill_replay_uses_taker(v2_panel): ...  # unfilled maker candidate -> one row with finite r_net_taker equal to a taker forced-entry trade list
def test_xsec_maker_variant_posts_post_only(): ...
```

- [ ] **Step 2: Run tests to verify they fail** — `UV_SYSTEM_CERTS=1 uv run pytest tests/v2/test_order_model.py -v` → FAIL.
- [ ] **Step 3: Thread `order_model` through planner → constructor → pipeline → backtest → tradelists (default None everywhere).**
- [ ] **Step 4: Implement `luna/v2/fills.py`.**
- [ ] **Step 5: Run tests to verify they pass** — `tests/v2` PASS, **and the full V1 suite is unchanged:** `UV_SYSTEM_CERTS=1 uv run pytest -q` → all pass.
- [ ] **Step 6: Commit** — `git commit -m "feat: order model option (perp maker / spot taker) and missed-fill counterfactuals (V2-7)"`

---

### Task 8 (V2-8): Extended reporting metrics (§6)

**Files:**
- Modify: `luna/engine/backtest.py`: append `net_leverage` and `max_asset_share` to `BAR_STAT_COLUMNS` and compute them per bar. V1 readers use only `n_positions` and `gross_leverage`.
- Create: `luna/v2/metrics.py`
- Test: `tests/v2/test_metrics.py`

**Interfaces:**
- Consumes: `BacktestResult`, `metrics.daily_returns`, `stats.cvar`, `CostModel`, `RiskLimits`, `OrderModel`, `maker_fill_rate` (Task 7).
- Produces (each a pure function, unit-tested with hand-computed values):
  - `R_DEFINITIONS: dict[str, str]`: research `r_net`, engine `r_multiple`, cap-adjusted R, gross R.
  - `cap_adjusted_r(trades, equity: pd.Series, limits: RiskLimits) -> pd.Series`: `pnl_net / risk_precap` when the column exists, else `pnl_net / (risk_per_trade × equity at the signal bar = entry_ts - 15min)`.
  - `split_spread_slippage(trades, costs, order_model) -> pd.DataFrame` (columns `spread`, `slippage`). Taker: spread_slip split by `half_spread / (half_spread + slippage)`. Maker: all spread.
  - `mfe_mae_r(trades, panel) -> pd.DataFrame`: favourable / adverse excursion of 15m highs/lows within `[entry_ts, exit_ts)`, relative to the entry bar open, × gross_notional / risk_amount, per direction.
  - `winner_concentration(pnl: np.ndarray) -> dict`: `top1`, `top3`, `top5` shares of total net (None if net ≤ 0) and `gini` of winners.
  - `tail_risk(daily: pd.Series) -> dict`: `var95`, `var99`, `es95`, `es99` (historical), `worst_day`, `worst_week` (worst 7-day compounded).
  - `holding_hours(trades) -> dict` (`median`, `p10`, `p90`).
  - `annual_turnover(trades, equity) -> float`.
  - `ex_ante_var_exceedance(daily: pd.Series, ex_ante: pd.Series) -> dict` (`rate`, `n_days`; cross-sectional only).
  - `extended_metrics(res, panel, costs, kind, limits, order_model) -> dict`, which gathers all of the above:
    - `planned_r`, `cap_adjusted_r`, `gross_r`, `net_r`, `payoff_ratio`, `cost_to_gross` ("n/a – no gross edge" when gross ≤ 0);
    - `costs` {fees, spread, slippage, funding_known, funding_unknown_hours};
    - `equity_contribution` {by_asset usd/pct, top_trades};
    - `maker_fill_rate`, `exposure` {mean/max gross, mean/max |net|, max asset share, mean vol scale}.

- [ ] **Step 1: Write the failing tests** — one per function: `test_cap_adjusted_r_uses_precap_column`, `test_cap_adjusted_r_from_equity_when_absent`, `test_split_spread_slippage_taker_and_maker`, `test_mfe_mae_long_and_short`, `test_winner_concentration_and_gini`, `test_tail_risk_matches_hand_values`, `test_cost_to_gross_na_without_edge`, `test_bar_stats_have_net_leverage_and_max_share`, `test_extended_metrics_keys_complete` (asserts every §6 row has a key).
- [ ] **Step 2: Run tests to verify they fail** — `UV_SYSTEM_CERTS=1 uv run pytest tests/v2/test_metrics.py -v` → FAIL.
- [ ] **Step 3: Implement the two bar-stat columns and `luna/v2/metrics.py`.**
- [ ] **Step 4: Run tests to verify they pass** — PASS; full suite green.
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): extended research metrics incl. CVaR/ES, cap-adjusted R, cost attribution (V2-8)"`

---

### Task 9 (V2-9): Robustness engine (§5, diagnostics only)

**Files:**
- Create: `luna/v2/robustness.py`
- Test: `tests/v2/test_robustness.py`

**Interfaces:**
- Consumes: `block_bootstrap_ci`, `profit_factor` (`luna.research.stats`), `BacktestResult`.
- Produces:
  - `trade_metrics(trades) -> dict` (`n`, `mean_r`, `ci_lo`, `ci_hi`, `pf`, `net_usd`; day-block bootstrap on `exit_ts` days, 2,000 samples, 90%).
  - `ex_top(trades, *, k: int | None = None, frac: float | None = None) -> pd.DataFrame`: removes the largest winners by `pnl_net`.
  - `trimmed_mean_r(r, cut=0.05) -> float`.
  - `pnl_concentration(trades) -> dict` (`top1pct`, `top5pct`, `top10pct`, `asset_hhi`).
  - `time_loo(trades, per_window) -> pd.DataFrame` (one row per removed test window).
  - `best_period_removal(trades) -> dict` (`ex_best_month`, `ex_best_quarter` by `exit_ts`).
  - `asset_loo(run_without: Callable[[str], BacktestResult], assets: list[str]) -> pd.DataFrame` (`asset`, `n`, `mean_r`, `net_usd`).
  - `robustness(trades, per_window, *, run_without, assets) -> dict` with keys `full`, `ex_top1`, `ex_top3`, `ex_top1pct`, `trimmed_mean_r`, `median_r`, `concentration`, `time_loo`, `asset_loo`, `ex_best_month`, `ex_best_quarter`, `fragile`.
  - `fragile` = full `mean_r > 0` and (ex_top1 `mean_r < 0` or any time/asset LOO `mean_r < 0`).

- [ ] **Step 1: Write the failing tests** — hand-built trade frames: `test_ex_top_removes_largest_winners`, `test_trimmed_and_median`, `test_pnl_concentration_hhi`, `test_time_loo_drops_one_window_each`, `test_best_month_and_quarter_removal`, `test_asset_loo_calls_runner_per_asset`, `test_fragile_label_rules`, and `test_robustness_has_no_verdict_inputs` (the returned dict has no `verdict`/`passed` keys; the module does not import `luna.report.gonogo`).
- [ ] **Step 2: Run tests to verify they fail** — `UV_SYSTEM_CERTS=1 uv run pytest tests/v2/test_robustness.py -v` → FAIL.
- [ ] **Step 3: Implement `luna/v2/robustness.py`.**
- [ ] **Step 4: Run tests to verify they pass** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): robustness diagnostics (ex-top, LOO, concentration, fragile label) (V2-9)"`

---

### Task 10 (V2-10): V2 walk-forward orchestration

**Files:**
- Modify: `luna/engine/backtest.py` (`run_backtest(..., flatten_at_end: bool = False)`: P10 flatten at the last bar, exit reason `model_switch`, equity's last point re-marked)
- Modify: `luna/engine/walkforward.py:426-458` (`_simulate`):
  - optional `initial: tuple[Portfolio, RiskState, PendingExits] | None = None` and `flatten_last: bool = False`;
  - strategy names ordered `[n for n in STRATEGIES if …] + sorted(other names)`, so non-V1 names are no longer dropped;
  - V1 behaviour is unchanged.
- Create: `luna/v2/configs.py`, `luna/v2/walkforward.py`
- Modify: `luna/cli.py` (command `research-v2`)
- Test: `tests/v2/test_configs.py`, `tests/v2/test_v2_walkforward.py`, `tests/test_cli_research_v2.py`

**Interfaces:**
- Consumes: everything above. V1: `universe_at`, `_choose_params`, `_check_train`, `_known_rates`, `fit_expectancy`, `ExpectancyRanker`, `WindowModel`, `WalkForwardResult`, `_simulate`, `_stitch`, `_open_at_end`, `_row`, `build_all_tradelists`, `CostModel`.
- Produces:
  - `luna.v2.configs`:
    - `@dataclass(frozen=True) class V2Config: account; family: Literal["tsmom", "breakout", "xsec"]; timeframe: Timeframe; universe_mode: Literal["btc_eth", "broad"]; rebalance: Literal["weekly", "daily"] = "weekly"`, with property `id` = `f"{account}|{family}|{timeframe}|{universe_mode}" + ("|daily" if daily)`.
    - `primary_configs(account) -> list[V2Config]` (perp 18, spot 14).
    - `sensitivity_configs(account) -> list[V2Config]` (cross-sectional daily, 2 per account).
    - `restrict_universe(universe, mode) -> list[str]`.
  - `luna.v2.walkforward`:
    - `@dataclass class ConfigWindowModel: config: V2Config; window: Window; universe: list[str]; params_id: str | None; objective: float; accepted: bool; signal: WindowModel | None; xsec: tuple[XSecParams, list[str]] | None`.
    - `build_v2_tradelists(inp, settings, account, *, order_model=None, cache_dir) -> pd.DataFrame`: every V2 signal class on the union of window universes.
    - `fit_signal_config(cfg, w, tl, inp, settings) -> ConfigWindowModel`.
    - `fit_xsec_config(cfg, w, inp, settings) -> ConfigWindowModel`: train-window `run_xsec` per grid point, edge limits, fresh account; acceptance = `fit_expectancy` with key `"portfolio"`.
    - `fit_all(configs, inp, settings, tl, *, jobs) -> dict[str, list[ConfigWindowModel]]`.
    - `select_candidate(fitted: dict[str, list[ConfigWindowModel]]) -> list[ConfigWindowModel | None]` (P9).
    - `run_config_walkforward(fitted: list[ConfigWindowModel], inp, settings, **variant) -> WalkForwardResult`.
    - `run_selected_walkforward(selected: list[ConfigWindowModel | None], inp, settings, **variant) -> WalkForwardResult` (P10 switches).
    - `v2_variants(kind, *, candidate: bool) -> dict[str, dict]` (P11).
    - `count_trials(configs) -> dict` (`grid_points`, `strategy_params_key_combinations`, `configs`).
  - CLI: `luna research-v2 --account perp|spot [--jobs 2] [--assets A,B] [--resume DIR]`.
    - Writes `var/reports/research_v2_<account>_<ts>/` with per-unit checkpoints `units/<unit>.pkl` (resume skips finished units).
    - Never calls anything holdout.

  The nested selection, for reference:

```python
def select_candidate(fitted):
    out = []
    for k in range(n_windows):
        ok = [cw[k] for cw in fitted.values() if cw[k].accepted and math.isfinite(cw[k].objective)]
        out.append(min(ok, key=lambda m: (-m.objective, m.config.id)) if ok else None)  # ties -> smaller id
    return out
```

- [ ] **Step 1: Write the failing tests**

```python
def test_primary_config_counts(): assert len(primary_configs("perp")) == 18 and len(primary_configs("spot")) == 14
def test_btc_eth_universe_mode(): assert restrict_universe(["SOL", "ETH", "BTC"], "btc_eth") == ["ETH", "BTC"]
def test_fit_signal_config_uses_train_only(v2_panel, v2_settings_small): ...   # moving test-window bars changes nothing in the fit
def test_fit_xsec_config_selects_on_train_only(): ...
def test_select_candidate_argmax_ties_and_none(): ...
def test_run_config_walkforward_signal_matches_v1_simulate(): ...   # TSMOM 15m V1 point vs V1 _simulate with the same models: identical trades
def test_selected_walkforward_engine_switch_flattens(v2_panel, v2_settings_small):
    res = run_selected_walkforward([sig_model, xsec_model, sig_model], ...)
    assert "model_switch" in set(res.oos.trades.exit_reason)
    assert res.oos.equity.iloc[-1] - res.oos.initial_equity == pytest.approx(res.oos.trades.pnl_net.sum() + open_unreal, abs=1e-6)
def test_v1_simulate_unchanged_by_extensions(): ...   # V1 fixture: _simulate default args -> same result as before (golden from tests/engine/test_walkforward.py)
def test_flatten_at_end_default_false_keeps_v1(): ...
def test_count_trials():
    # perp: (tsmom 22 + breakout 22) x 2 universes + xsec 24 x 2 = 136; spot: (16 + 16) x 2 + 6 x 2 = 76
    assert count_trials(primary_configs("perp"))["grid_points"] == 136
    assert count_trials(primary_configs("spot"))["grid_points"] == 76
def test_cli_research_v2_smoke(tmp_path, monkeypatch): ...   # tiny store, --assets BTC,ETH, writes summary.json + report.html, no holdout registry created
def test_cli_resume_skips_finished_units(): ...
```

- [ ] **Step 2: Run tests to verify they fail** — `UV_SYSTEM_CERTS=1 uv run pytest tests/v2/test_configs.py tests/v2/test_v2_walkforward.py tests/test_cli_research_v2.py -v` → FAIL.
- [ ] **Step 3: Implement the two V1 extensions** (`flatten_at_end`, `_simulate` initial/flatten_last/names); run `UV_SYSTEM_CERTS=1 uv run pytest tests/engine -q` → all V1 engine tests pass.
- [ ] **Step 4: Implement `luna/v2/configs.py`, `luna/v2/walkforward.py`, the CLI command.** `--jobs` uses `concurrent.futures.ProcessPoolExecutor` over fit/simulation units; `jobs=1` runs inline.
- [ ] **Step 5: Run tests to verify they pass** — PASS; full suite green.
- [ ] **Step 6: Commit** — `git commit -m "feat(v2): config grid, train-only fitting, nested candidate selection, research-v2 CLI (V2-10)"`

---

### Task 11 (V2-11): V2 GO/NO-GO report (per account)

**Files:**
- Create: `luna/v2/report.py`
- Modify: `luna/cli.py` (`research-v2` writes the report)
- Test: `tests/v2/test_report.py`

**Interfaces:**
- Consumes: `luna.report.gonogo.evaluate`, `CHECKS`; `luna.report.walkforward.run_naive`, `benchmarks`; `extended_metrics` (Task 8); `robustness` (Task 9); Task 10 results.
- Produces:
  - `@dataclass class V2Report: account; span; candidate_windows: pd.DataFrame (window, config id, params, objective, accepted); variants: dict[str, WalkForwardResult]; configs: dict[str, dict[str, WalkForwardResult]]; naive_momentum: BacktestResult; gonogo: GoNoGo; robustness: dict[str, dict]; extended: dict[str, dict]; missed_fills: pd.DataFrame; trials: dict; caveats: list[str]`.
  - `build_v2_report(...) -> V2Report`. The verdict is `evaluate(edge, realistic, cost_2x_edge, edge.per_window, naive_momentum_expectancy, known_from)`, the same call as V1.
  - `summary_dict_v2(rep) -> dict`, `write_v2_report(rep, out_dir) -> Path` (writes `report.html` + `summary.json`).
  - Report sections, in order:
    1. verdict and checks;
    2. "Development walk-forward OOS 2024-09 → 2026-07" statement and the untouched-holdout statement;
    3. candidate per window;
    4. variants table;
    5. naive momentum bar;
    6. configurations table (realistic/edge per config);
    7. extended metrics (§6) with R definitions;
    8. robustness (§5) with "kırılgan" tags, explicitly "diagnostic, not part of the verdict";
    9. cost attribution;
    10. trial counts;
    11. caveats.

- [ ] **Step 1: Write the failing tests**

```python
def test_verdict_uses_v1_evaluate_and_naive_momentum_only(): ...  # naive mean reversion better than momentum must not raise the bar
def test_fragile_label_never_changes_verdict(): ...               # same inputs, fragile forced True/False -> identical gonogo
def test_report_contains_no_holdout_metrics(tmp_path):
    js = json.loads(write_and_read_summary(...)); flat = json.dumps(js)
    assert "2026-07-01" in js["span"][1] and not re.search(r"holdout_(metric|result|verdict)", flat)
def test_reports_are_per_account(): ...   # summary has one "account"; no merged totals key
def test_aday_yok_when_not_go(): assert summary["candidate"] == "aday yok"
def test_html_has_all_sections(tmp_path): ...  # section ids present in order
```

- [ ] **Step 2: Run tests to verify they fail** — `UV_SYSTEM_CERTS=1 uv run pytest tests/v2/test_report.py -v` → FAIL.
- [ ] **Step 3: Implement `luna/v2/report.py` and hook it into the CLI.**
- [ ] **Step 4: Run tests to verify they pass** — PASS; then full suite `UV_SYSTEM_CERTS=1 uv run pytest -q` and `uv run ruff check .` → clean.
- [ ] **Step 5: Commit** — `git commit -m "feat(v2): per-account V2 GO/NO-GO report with robustness and extended metrics (V2-11)"`

---

### Task 12: Run Research V2 and write the results document

**Files:**
- Create: `docs/results/research-v2.md`

**Interfaces:**
- Consumes: `luna research-v2` (Tasks 10–11). Data: `var/bars`, `var/funding` (store as of 2026-10-08, cut at `V2_DATA_END` by the loader).

- [ ] **Step 1: Pre-flight.**
  - `git status` is clean on `feat/research-v2`.
  - `var/holdout_registry.json` does not exist.
  - `docker compose ps` shows the paper bots healthy (they are not touched).
  - Free memory is at least 6 GB.
- [ ] **Step 2: Run perp.** `UV_SYSTEM_CERTS=1 uv run luna research-v2 --account perp --jobs 2`, as a detached process with its log in `var/research_v2_perp.log`. Expected: exit 0, `summary.json` + `report.html`, a run of several hours (asset leave-one-out re-runs dominate). If it is interrupted, re-run with `--resume <dir>`.
- [ ] **Step 3: Run spot.** Same command with `--account spot`.
- [ ] **Step 4: Verify isolation.**
  - `var/holdout_registry.json` is still absent.
  - In both summaries `span == ["2024-09-01…", "2026-07-01…"]`.
  - The last bar used is < `V2_DATA_END`; the loader assertion was logged.
- [ ] **Step 5: Write `docs/results/research-v2.md`** (Turkish, same style as `docs/results/walkforward.md`):
  - per-account verdict and checks;
  - candidate per window;
  - trial counts;
  - configurations table;
  - robustness summary with "kırılgan" tags;
  - cost attribution;
  - "aday" or "aday yok";
  - the statement that holdout use is a separate user decision (D7).

  No criterion or value is changed after seeing the results.
- [ ] **Step 6: Commit** — `git add docs/results/research-v2.md && git commit -m "docs: Research V2 results (development walk-forward OOS 2024-09..2026-07)"`
