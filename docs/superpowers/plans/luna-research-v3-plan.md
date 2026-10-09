# Luna Research V3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the three frozen V3 alpha families (perp premium cross-section, low-vol cross-section, vol-managed spot exposure) through the V2 walk-forward, risk engine and GO/NO-GO, against family null baselines, with no data at or after 2026-07-01.

**Architecture:** V3 reuses V2's cross-sectional simulator (`run_xsec`), portfolio risk projection, nested train-only selection, robustness and report. V2 gets four default-preserving hooks (a `signal` field on `XSecParams`, a score dispatcher, spot vol targeting when a vol target is given, an optional baseline expectancy in the report); every V2 default path is pinned by golden tests and the full V2 suite. All V3 logic lives in `luna/v3/` (signals, data, configs, runner, report) plus the `luna research-v3` CLI.

**Tech Stack:** Python 3.12, pandas/numpy, typer, pytest (uv).

**Spec:** `docs/superpowers/specs/luna-research-v3-design.md` (FROZEN, tag `research-v3-spec-frozen`).

## Global Constraints

- `V3_DATA_END = V2_DATA_END = 2026-07-01T00:00:00Z`; every V3 input passes `assert_before_cutoff`; `luna holdout` is never called; summary/report text never contains the word "holdout" (V2 test convention).
- V1/V2 behaviour unchanged: V2 grids, ids (`params_id`, config ids), simulations and reports are identical when no V3 field is set; the full suite must stay green (672 passed, 3 deselected before V3 tests).
- Weekly rebalance only. Grids exactly as spec §3/§6: L ∈ {14, 30, 90}, k ∈ {1, 2}, perp risk grid gross {1.0, 1.5} × vol {0.10, 0.15}; C: vol ∈ {0.10, 0.15}, k = 99 (all eligible). Trials: 10 configs (perp 4, spot 6), 124 grid points.
- Null baselines: A/B `shuffle<seed>:<signal>` (draws seeded by seed, stamp, asset name; only on assets the real signal can rank), seeds 1..5; C: same plan with `vol_target=None` (run once when no window is shuffled).
- GO/NO-GO: criteria 1-7 from V1 `evaluate()` unchanged; criterion 8 = candidate daily Sharpe (edge) > mean null daily Sharpe over the seeds that traded (cash 0 if none); verdict re-derived with the V1 rule; V1 naive momentum and R comparison reported for information.
- SC-1 limits and SC-2 fees unchanged. No live orders.
- Every report states the dataset-reuse caveat (spec E2).

## Review Focus

1. Premium on a day where the spot (or perp) bar is forward-filled / missing → must be NaN, never a stale ratio (test in Task 2).
2. Post-cutoff bar in the *other* venue's panel used only for the premium → must raise `HoldoutLeak` (test in Task 3).
3. A shuffled null that is not deterministic across processes/resume → same seed + same stamp must give the same permutation (test in Task 2).
4. Spot vol targeting must never scale weights up (cap 1.0), must fail closed (`cov_unavailable`, flat) when the covariance cannot measure a held asset, and must leave V2 spot (vol_target None) byte-identical (tests in Task 1).
5. A V3 config id or `params_id` colliding with a V2 id (trade-list cache, checkpoints) → V3 ids are prefixed `v3_` and carry the signal (test in Task 4).

---

### Task 1: V2 hooks with golden tests (default behaviour unchanged)

**Files:**
- Modify: `luna/v2/xsec.py` (XSecParams.signal, `scores()`, `eligible`/`raw_targets` use `scores`)
- Modify: `luna/v2/xsec_sim.py:381-395` (pass `aux`; spot `vol_target=params.vol_target`)
- Modify: `luna/v2/portfolio_risk.py:138-155` (spot branch: optional vol targeting)
- Modify: `luna/v2/walkforward.py:217` (`XSecParams(..., signal=g.get("signal", "momentum"))`)
- Modify: `luna/v2/runner.py:229` (trade lists only when a signal-engine config exists)
- Modify: `luna/v2/report.py:141` (`build_v2_report(..., baseline_expectancy: float | None = None)`)
- Test: `tests/v3/test_v2_hooks.py`

**Interfaces:**
- Produces: `XSecParams(lookback_days, k, gross=1.0, vol_target=None, rebalance="weekly", signal="momentum")`; `params_id` unchanged when `signal == "momentum"`, else `+ f"_{signal}"`.
- Produces: `scores(daily_close: pd.DataFrame, t: pd.Timestamp, params: XSecParams, aux: pd.DataFrame | None = None) -> pd.Series` (momentum → `lookback_returns`; otherwise `luna.v3.signals.score`).
- Produces: `eligible(daily_close, real_at, universe, lookback_days, t, *, params=None, aux=None)`; `raw_targets(daily_close, assets, params, kind, t, aux=None)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/v3/test_v2_hooks.py
import numpy as np
import pandas as pd
from luna.v2.portfolio_risk import project_weights
from luna.v2.xsec import XSecParams, daily_closes, lookback_returns, raw_targets, scores


def test_default_params_id_unchanged():
    assert XSecParams(30, 1).params_id == "L30_k1_g1_v0"
    assert XSecParams(30, 1, 1.5, 0.1).params_id == "L30_k1_g1.5_v0.1"
    assert XSecParams(30, 1, signal="lowvol").params_id == "L30_k1_g1_v0_lowvol"


def test_momentum_scores_are_lookback_returns(v2_panel):
    dc = daily_closes(v2_panel)
    t = dc.index[100]
    pd.testing.assert_series_equal(scores(dc, t, XSecParams(30, 1)), lookback_returns(dc, t, 30))


def test_raw_targets_default_matches_v2(v2_panel):
    dc = daily_closes(v2_panel)
    t = dc.index[100]
    assets = ["BTC", "ETH", "AAA", "BBB"]
    got = raw_targets(dc, assets, XSecParams(30, 1), "perp", t)
    rets = lookback_returns(dc, t, 30)[assets]
    top, bottom = rets.idxmax(), rets.idxmin()
    assert got[top] == 0.5 and got[bottom] == -0.5 and got.abs().sum() == 1.0


def test_spot_projection_without_vol_target_is_unchanged():
    raw = pd.Series({"BTC": 0.5, "ETH": 0.5})
    cov = pd.DataFrame(np.eye(2) * 1e-3, index=raw.index, columns=raw.index)
    p = project_weights(raw, cov, cov, controls="spot", gross=1.0, vol_target=None)
    assert p.weights.tolist() == [0.4, 0.4] and p.vol_scale == 1.0


def test_spot_vol_target_scales_down_only():
    raw = pd.Series({"BTC": 0.5, "ETH": 0.5})
    hi = pd.DataFrame(np.eye(2) * 4e-3, index=raw.index, columns=raw.index)  # ~120% annual vol
    p = project_weights(raw, hi, hi, controls="spot", gross=1.0, vol_target=0.10)
    assert 0 < p.vol_scale < 1 and (p.weights < 0.4).all()
    lo = pd.DataFrame(np.eye(2) * 1e-8, index=raw.index, columns=raw.index)
    q = project_weights(raw, lo, lo, controls="spot", gross=1.0, vol_target=0.10)
    assert q.vol_scale == 1.0 and q.weights.tolist() == [0.4, 0.4]
```

- [ ] **Step 2: Run, verify FAIL** — `uv run pytest tests/v3/test_v2_hooks.py -q` → fails (`signal` unknown, `scores` missing, spot ignores vol target).

- [ ] **Step 3: Implement**
  - `XSecParams`: add `signal: str = "momentum"`; `params_id` appends `_{signal}` only when not momentum.
  - `scores()`: `if params.signal == "momentum": return lookback_returns(daily_close, t, params.lookback_days)`; else `from luna.v3.signals import score; return score(daily_close, t, params, aux)` (lazy import: worker processes need no extra initializer).
  - `eligible(...)`: when `params` is given, the finiteness test uses `scores(...)` instead of `lookback_returns`; history rule unchanged. `raw_targets` ranks by `scores(...)`.
  - `xsec_sim.decide`: `aux = premium_daily(panel)` computed once per `run_xsec` call **only** when `params.signal == "premium"` (import inside the branch); pass `params=params, aux=aux` to `eligible`/`raw_targets`; spot projection gets `vol_target=params.vol_target`.
  - `project_weights` spot branch: after the 40% cap, if `vol_target is not None` and every traded asset is in `cov`: `sd = sqrt(w @ cov @ w)`; `scale = min(1.0, (vol_target / sqrt(365)) / sd)` when `sd > 0`; return `Projection(w * scale, scale, nan, "")`.
  - `fit_xsec_config`: `XSecParams(..., cfg.rebalance, signal=g.get("signal", "momentum"))`.
  - `run_research`: `tl = build_v2_tradelists(...) if any(c.engine == "signal" for c in configs) else pd.DataFrame(columns=["strategy", "params_id", "key"])`.
  - `build_v2_report(r, inp, settings, baseline_expectancy=None)`: `naive_momentum_expectancy(r.naive) if baseline_expectancy is None else baseline_expectancy`.

- [ ] **Step 4: Verify** — `uv run pytest tests/v3/test_v2_hooks.py tests/v2 -q` → all pass (V2 suite unchanged).

- [ ] **Step 5: Commit** — `git commit -m "feat(v3): default-preserving V2 hooks (xsec signal, spot vol target, report baseline)"`

### Task 2: V3 signals (premium, low-vol, shuffle)

**Files:**
- Create: `luna/v3/__init__.py`, `luna/v3/signals.py`
- Test: `tests/v3/test_signals.py`

**Interfaces:**
- Produces: `premium_daily(panel) -> pd.DataFrame` (index = 23:45 decision stamps, value = `premium` column at that bar; NaN when the bar is filled or `premium` is NaN; empty frame if no panel has the column).
- Produces: `score(daily_close, t, params, aux) -> pd.Series` dispatching `"premium"` (−mean of `aux` over (t − L days, t], NaN if < ceil(L/2) finite), `"lowvol"` (−std(ddof=1) of daily log returns of `daily_close` over (t − L days, t], NaN if < ceil(L/2) finite), `"shuffleN"` (deterministic normal draws from `np.random.default_rng([N, t.value // 10**9])`, assets in sorted order; NaN where the momentum score is NaN). Unknown signal → `ValueError`.

- [ ] **Step 1: Failing tests**

```python
# tests/v3/test_signals.py
import math
import numpy as np
import pandas as pd
import pytest
from luna.v2.xsec import XSecParams, daily_closes
from luna.v3.signals import premium_daily, score


def _with_premium(panel, value=0.01):
    return {a: df.assign(premium=value) for a, df in panel.items()}


def test_premium_daily_uses_decision_bar_and_drops_filled(v2_panel):
    p = _with_premium(v2_panel)
    a = "BTC"
    p[a] = p[a].copy()
    stamp = p[a].index[(p[a].index.hour == 23) & (p[a].index.minute == 45)][3]
    p[a].loc[stamp, "filled"] = True
    d = premium_daily(p)
    assert math.isnan(d.loc[stamp, a]) and d[a].dropna().eq(0.01).all()


def test_premium_score_is_negative_mean_and_causal(v2_panel):
    p = _with_premium(v2_panel)
    dc, aux = daily_closes(p), premium_daily(p)
    t = dc.index[60]
    s = score(dc, t, XSecParams(14, 1, signal="premium"), aux)
    assert s["BTC"] == pytest.approx(-0.01)
    later = aux.copy(); later.loc[later.index > t] = 99.0
    pd.testing.assert_series_equal(s, score(dc, t, XSecParams(14, 1, signal="premium"), later))


def test_lowvol_score_is_negative_std_and_causal(v2_panel):
    dc = daily_closes(v2_panel)
    t = dc.index[60]
    s = score(dc, t, XSecParams(30, 1, signal="lowvol"), None)
    win = np.log(dc.loc[(dc.index > t - pd.Timedelta(days=30)) & (dc.index <= t)]).diff()
    assert s["ETH"] == pytest.approx(-win["ETH"].std(ddof=1))
    bumped = dc.copy(); bumped.loc[bumped.index > t] *= 3
    pd.testing.assert_series_equal(s, score(bumped, t, XSecParams(30, 1, signal="lowvol"), None))


def test_shuffle_is_deterministic_and_seed_dependent(v2_panel):
    dc = daily_closes(v2_panel)
    t = dc.index[60]
    a = score(dc, t, XSecParams(30, 1, signal="shuffle1"), None)
    assert a.equals(score(dc, t, XSecParams(30, 1, signal="shuffle1"), None))
    assert not a.equals(score(dc, t, XSecParams(30, 1, signal="shuffle2"), None))


def test_unknown_signal_raises(v2_panel):
    dc = daily_closes(v2_panel)
    with pytest.raises(ValueError):
        score(dc, dc.index[60], XSecParams(30, 1, signal="nope"), None)
```

- [ ] **Step 2: Run, verify FAIL** (module missing). **Step 3: Implement** per Interfaces. **Step 4:** `uv run pytest tests/v3 -q` → pass. **Step 5: Commit** `feat(v3): premium, low-vol and shuffled-null scores`.

### Task 3: V3 inputs (premium column, cutoff)

**Files:**
- Create: `luna/v3/data.py`
- Test: `tests/v3/test_data.py`

**Interfaces:**
- Produces: `add_premium(panel, other, account) -> dict[str, DataFrame]`: perp account: `premium = close / other_close − 1`; spot account: `premium = other_close / close − 1`; aligned on the 15m index; NaN where either bar is missing or `filled`.
- Produces: `load_v3_inputs(settings, store, account, instruments, other_instruments, *, assets=None) -> Inputs` = `load_v2_inputs` for both accounts, `assert_before_cutoff` on the other panel, panel replaced by `add_premium(...)`.

- [ ] **Step 1: Failing tests**

```python
# tests/v3/test_data.py
import math
import pandas as pd
import pytest
from luna.v2.data import HoldoutLeak, V2_DATA_END, assert_before_cutoff
from luna.v3.data import add_premium


def test_perp_and_spot_premium_definitions(v2_panel):
    perp = {"BTC": v2_panel["BTC"]}
    spot = {"BTC": v2_panel["BTC"].assign(close=v2_panel["BTC"]["close"] * 0.99)}
    p = add_premium(perp, spot, "perp")["BTC"]["premium"].dropna()
    s = add_premium(spot, perp, "spot")["BTC"]["premium"].dropna()
    assert p.iloc[10] == pytest.approx(1 / 0.99 - 1) and s.iloc[10] == pytest.approx(1 / 0.99 - 1)


def test_premium_nan_where_other_bar_filled_or_missing(v2_panel):
    perp = {"BTC": v2_panel["BTC"]}
    other = v2_panel["BTC"].copy()
    other.iloc[100, other.columns.get_loc("filled")] = True
    other = other.drop(other.index[200])
    out = add_premium(perp, {"BTC": other}, "perp")["BTC"]
    assert math.isnan(out["premium"].iloc[100]) and math.isnan(out["premium"].iloc[200])


def test_asset_without_other_venue_gets_nan_premium(v2_panel):
    out = add_premium({"AAA": v2_panel["AAA"]}, {}, "perp")
    assert out["AAA"]["premium"].isna().all()


def test_other_panel_after_cutoff_is_a_leak(v2_panel):
    late = v2_panel["BTC"].copy()
    late.index = pd.date_range(V2_DATA_END - pd.Timedelta("1D"), periods=len(late), freq="15min")
    with pytest.raises(HoldoutLeak):
        assert_before_cutoff({"BTC": late}, {})
```

- [ ] **Steps 2–5** as above; commit `feat(v3): premium inputs from the other venue under the V2 cutoff`.

### Task 4: V3 configurations and trial count

**Files:**
- Create: `luna/v3/configs.py`
- Test: `tests/v3/test_configs.py`

**Interfaces:**
- Produces: `V3Config(V2Config)` with field `signal: Literal["premium", "lowvol", "volmanaged"]`; `id = f"{account}|v3_{signal}|1d|{universe_mode}"`; `strategy_name = f"v3_{signal}"`; `engine = "xsec"`; `grid()` per spec.
- Produces: `v3_configs(account) -> list[V3Config]` (perp: premium, lowvol × btc_eth/broad; spot: premium, lowvol, volmanaged × btc_eth/broad); `v3_trials(account) -> dict` via V2 `count_trials`.

- [ ] **Step 1: Failing tests**

```python
# tests/v3/test_configs.py
from luna.v2.walkforward import count_trials
from luna.v3.configs import v3_configs


def test_trial_counts_match_spec():
    perp, spot = count_trials(v3_configs("perp")), count_trials(v3_configs("spot"))
    assert (perp["configs"], perp["grid_points"]) == (4, 96)
    assert (spot["configs"], spot["grid_points"]) == (6, 28)


def test_ids_are_prefixed_and_grids_carry_the_signal():
    for c in v3_configs("perp") + v3_configs("spot"):
        assert "|v3_" in c.id and c.engine == "xsec" and c.rebalance == "weekly"
        sig = "momentum" if c.signal == "volmanaged" else c.signal
        assert all(g["signal"] == sig for g in c.grid())


def test_volmanaged_grid_is_all_assets_with_vol_target():
    vm = [c for c in v3_configs("spot") if c.signal == "volmanaged"][0]
    assert vm.grid() == [
        {"lookback_days": 30, "k": 99, "vol_target": 0.10, "signal": "momentum"},
        {"lookback_days": 30, "k": 99, "vol_target": 0.15, "signal": "momentum"},
    ]
```

- [ ] **Steps 2–5**; commit `feat(v3): configuration grid and trial count`.

### Task 5: V3 runner (V2 research + null baselines)

**Files:**
- Create: `luna/v3/runner.py`
- Test: `tests/v3/test_runner.py`

**Interfaces:**
- Consumes: `run_research`, `run_selected_walkforward`, `v2_variants`, `v2_settings`, `metrics_with_ci`.
- Produces: `null_model(cw, seed) -> ConfigWindowModel | None` (premium/lowvol → `signal=f"shuffle{seed}"`; volmanaged → `vol_target=None`; flat stays None).
- Produces: `run_v3(settings, account, inp, out_dir, *, windows=None, jobs=1, resume=False, loo=True) -> tuple[ResearchResults, dict]` where the dict is `{"null_runs": {seed: WalkForwardResult}, "null_expectancy_r": float}` (mean of finite seed expectancies, edge variant; NaN if none).

- [ ] **Step 1: Failing tests** (small fixture like `tests/v2/test_runner.py::small`, spot account, `V3Config("spot","xsec","1d","broad",signal="lowvol")` and volmanaged, 3 windows, `loo=False`):

```python
def test_run_v3_produces_nulls(small, tmp_path):
    settings, inp, windows = small
    res, null = run_v3(settings, "spot", inp, tmp_path, windows=windows, configs=CFGS, loo=False)
    assert set(null["null_runs"]) == {1, 2, 3, 4, 5}
    assert set(res.config_runs) == {c.id for c in CFGS}


def test_null_model_mapping():
    cw = _cw(XSecParams(30, 1, signal="lowvol"))
    assert null_model(cw, 3).xsec[0].signal == "shuffle3"
    vm = _cw(XSecParams(30, 99, vol_target=0.1))
    assert null_model(vm, 3).xsec[0].vol_target is None
    assert null_model(None, 1) is None
```

- [ ] **Steps 2–5**; commit `feat(v3): runner with family null baselines`.

### Task 6: V3 report + `luna research-v3` CLI

**Files:**
- Create: `luna/v3/report.py`
- Modify: `luna/cli.py` (new command `research-v3`, options `--account`, `--jobs`, `--out`, `--resume/--no-resume`)
- Test: `tests/v3/test_report.py`

**Interfaces:**
- Produces: `write_v3_report(res, null, inp, settings, out_dir) -> Path`: `build_v2_report(res, inp, settings, baseline_expectancy=null["null_expectancy_r"])`, appends V3 caveats (dataset reuse, null definition, families), writes via `write_v2_report`, then adds to `summary.json`: `"v3": {"dataset_note", "families", "null_expectancy_r", "null_seeds", "cumulative_trials": {"v1": 208, "v2": {"perp": 184, "spot": 88}, "v3": trials}}` and prefixes the criterion-8 detail with `"[V3 null baseline] "`.

- [ ] **Step 1: Failing test**: summary has the `v3` block, criterion-8 prefix, the dataset caveat, and `"holdout"` does not appear in the summary text.
- [ ] **Steps 2–5**; CLI smoke test via typer `CliRunner` with `--help`; commit `feat(v3): report and research-v3 CLI`.

### Task 7: Isolation, full suite, review, run

- [ ] `uv run pytest -q` (full) → all pass.
- [ ] Independent whole-branch review (most capable model) on `feat/research-v3` vs the spec; fix Critical/Important.
- [ ] Request the V3 runs through the overnight controller (spot then perp, `--jobs 2`, fixed `--out`), only before 10:00 UK.
- [ ] `docs/results/research-v3.md` from both summaries (preliminary if a run is incomplete; dataset-reuse caveat first).


## Execution log (2026-10-09 overnight)

- Tasks 1-6 implemented with TDD in a worktree while the V2 perp run finished (no commits before the spec freeze).
- Pre-freeze independent review (most capable model): ready with fixes; fixes applied before any V3 result existed: null eligibility, spot fail-closed, criterion 8 on daily Sharpe, seeds-traded count, single null run without shuffle.
