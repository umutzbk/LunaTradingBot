"""Walk-forward engine (spec §4, §5, §8) and the one-shot holdout guard.

Layout. ``make_windows`` lays windows out from ``data_start``: train ``train_months``, test
``test_months``, step ``step_months``; the last ``holdout_months`` before ``data_end`` are the holdout
and never part of a window (only whole test windows ending at or before the holdout start are used).
With ``step == test`` the test windows are contiguous.

Fitting a window (``fit_window``), using only data before ``train_end``:

1. universe = ``select_universe`` at ``train_end`` (ADV and the history rule look strictly before it);
2. pairs = ``select_pairs`` on ``[train_start, train_end)`` (perp only; spot never trades pairs);
3. keys: momentum / mean reversion = the universe, pairs = the selected pairs;
4. train trades = trade-list rows with ``entry_ts >= train_start`` and ``exit_ts < train_end``
   (asserted), restricted to each strategy's keys;
5. params per strategy = argmax of the mean ``r_net`` over its train trades, ties broken by the
   smaller ``params_id``. Perp funding rule (§4): if any of the strategy's train trades has
   ``funding_unknown_bars > 0`` the objective is ``min(mean r_net with unknown funding at 0x,
   mean r_net with unknown funding at 2x the estimate)`` (``_funding_estimate_r``);
6. expectancy table = ``fit_expectancy`` on the selected params' train trades; ranker =
   ``ExpectancyRanker`` (ablation ``random_ranker``: ``RandomRanker``); strategies instantiated with
   the chosen params and ``use_regime`` (ablation ``no_regime``: False).

Trade lists are built once for the whole period (``build_walkforward_tradelists``) and only
filtered per window. For the ``no_regime`` ablation they must be built with ``use_regime=False``.

OOS simulation (``run_walkforward``). Test windows are simulated sequentially with
``run_backtest``; the Portfolio, RiskState and pending exits of one window are the initial state of
the next. Models switch at window boundaries: entries use the new window's strategies, keys and
ranker; positions carried over keep being exited by the strategy instance (params) that opened them,
even when their key left the universe (``WindowStrategy``). Leading windows whose universe is empty
(the 18-month history rule) are skipped and reported; a later window with an empty universe runs
with no entries (carried positions still exit).
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from luna.config import Settings, WalkForwardConfig
from luna.engine.backtest import BacktestResult, FundingMode, PendingExits, run_backtest
from luna.execution.costs import CostModel
from luna.ranking.expectancy import ANY, CellStat, ExpectancyRanker, fit_expectancy
from luna.ranking.ranker import RandomRanker, Ranker
from luna.research.pairs_select import PairSelection, select_pairs
from luna.research.tradelists import build_all_tradelists
from luna.strategy.base import GridStrategy, SignalFrame
from luna.strategy.mean_reversion import MeanReversion
from luna.strategy.momentum import Momentum
from luna.strategy.pairs import Pairs
from luna.types import AccountKind, Instrument
from luna.universe import select_universe

Ablation = Literal["none", "no_regime", "random_ranker"]
STRATEGIES: dict[str, type[GridStrategy]] = {
    "momentum": Momentum, "mean_reversion": MeanReversion, "pairs": Pairs,
}
BAR = pd.Timedelta("15min")
_ROOT = Path(__file__).resolve().parents[2]


class HoldoutAlreadyRun(RuntimeError):
    """The holdout of this account has already been run (spec §8: exactly once)."""


@dataclass(frozen=True)
class Window:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


@dataclass
class WindowModel:
    window: Window
    universe: list[str]
    pairs: PairSelection
    params: dict[str, str]  # strategy -> chosen params_id (strategies without train trades absent)
    table: dict[tuple[str, str, str], CellStat]
    funding_rule: str
    ranker: Ranker
    strategies: list[GridStrategy] = field(default_factory=list)
    keys: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class WalkForwardResult:
    windows: list[WindowModel]
    oos: BacktestResult
    per_window: pd.DataFrame


# --- windows --------------------------------------------------------------------------------------


def _utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def make_windows(
    data_start, data_end, cfg: WalkForwardConfig
) -> tuple[list[Window], tuple[pd.Timestamp, pd.Timestamp]]:
    """(windows, (holdout_start, holdout_end)); every test window ends at or before holdout_start."""
    start, end = _utc(data_start), _utc(data_end)
    holdout_start = end - pd.DateOffset(months=cfg.holdout_months)
    windows = []
    k = 0
    while True:
        tr0 = start + pd.DateOffset(months=k * cfg.step_months)
        tr1 = tr0 + pd.DateOffset(months=cfg.train_months)
        te1 = tr1 + pd.DateOffset(months=cfg.test_months)
        if te1 > holdout_start:
            break
        windows.append(Window(tr0, tr1, tr1, te1))
        k += 1
    return windows, (holdout_start, end)


# --- fitting --------------------------------------------------------------------------------------


def _grid_params(cls: type[GridStrategy]) -> dict[str, dict]:
    return {cls(dict(g)).params_id: dict(g) for g in cls.GRID}


def _known_rates(funding: dict[str, pd.DataFrame] | None, before: pd.Timestamp) -> dict[str, float]:
    """Mean known hourly funding rate per asset over rates stamped before ``before`` (causal)."""
    out: dict[str, float] = {}
    for a, f in (funding or {}).items():
        if f is None or not len(f):
            continue
        s = f["rate"].dropna().astype(float)
        ix = pd.DatetimeIndex(s.index)
        ix = ix.tz_localize("UTC") if ix.tz is None else ix.tz_convert("UTC")
        s = s[np.asarray(ix < before)]
        if len(s):
            out[a] = float(s.mean())
    return out


def _funding_estimate_r(trades: pd.DataFrame, rates: dict[str, float]) -> pd.Series:
    """Per trade, the unknown-hours funding at 2x the estimated rate, in R.

    Estimate per leg l: ``-direction * s_l * f_l * 2 * rate_l * hours_l`` per unit of gross notional
    (the trade lists' convention: long pays a positive rate), divided by the trade's stop distance.
    Single asset: s = +1, f = 1, hours = funding_unknown_bars. Pair "A/B" (long = long A, short B):
    the trade list keeps no beta, so beta ~ 1 is assumed: f = 1/2 per leg, s = (+1, -1), and the
    unknown hours (counted per held leg) are split evenly between the two legs. ``rate_l`` is the
    asset's mean known rate before train_end, else the pooled mean of the known assets, else 0.
    """
    pooled = float(np.mean(list(rates.values()))) if rates else 0.0
    out = np.zeros(len(trades))
    for n, (key, d, unk, dist) in enumerate(zip(
        trades["key"], trades["direction"], trades["funding_unknown_bars"], trades["stop_distance"],
        strict=True,
    )):
        if not unk:
            continue
        legs = str(key).split("/")
        signs = (1.0, -1.0) if len(legs) == 2 else (1.0,)
        frac = 1.0 / len(legs)
        hours = float(unk) / len(legs)
        per_unit = sum(s * frac * 2.0 * rates.get(a, pooled) * hours
                       for a, s in zip(legs, signs, strict=True))
        out[n] = -float(d) * per_unit / float(dist)
    return pd.Series(out, index=trades.index)


def _choose_params(
    trades: pd.DataFrame, kind: AccountKind, rates: dict[str, float]
) -> tuple[str, bool]:
    """(params_id, worst-case funding rule applied) for one strategy's train trades."""
    worst = kind == "perp" and bool((trades["funding_unknown_bars"] > 0).any())
    r0 = trades["r_net"].astype(float)
    if worst:
        r2 = r0 + _funding_estimate_r(trades, rates)
        m0 = r0.groupby(trades["params_id"]).mean()
        m2 = r2.groupby(trades["params_id"]).mean()
        obj = np.minimum(m0, m2)
    else:
        obj = r0.groupby(trades["params_id"]).mean()
    best = obj.max()
    return min(str(p) for p, v in obj.items() if v == best), worst


def fit_window(
    w: Window,
    tradelists: pd.DataFrame,
    panel: dict[str, pd.DataFrame],
    settings: Settings,
    kind: AccountKind,
    *,
    funding: dict[str, pd.DataFrame] | None = None,
    ablation: Ablation = "none",
    seed: int = 0,
    universe_panel: dict[str, pd.DataFrame] | None = None,
) -> WindowModel:
    """Fit window ``w`` from data before ``w.train_end`` only (module docstring).

    ``funding``: perp funding frames (rates before train_end feed the worst-case estimate).
    ``universe_panel``: bars the universe is selected from (default ``panel``; e.g. the perp panel
    for a spot run), restricted to assets present in ``panel``."""
    if ablation not in ("none", "no_regime", "random_ranker"):
        raise ValueError(f"unknown ablation {ablation!r}")
    cfg = settings.universe
    universe = [a for a in select_universe(universe_panel or panel, w.train_end, cfg.min_adv_usd,
                                           cfg.min_history_days) if a in panel]
    pairs = select_pairs(panel, universe if kind == "perp" else [], w.train_start, w.train_end,
                         settings.pairs)
    keys = {"momentum": list(universe), "mean_reversion": list(universe), "pairs": list(pairs.selected)}

    tl = tradelists
    train = tl[(tl["entry_ts"] >= w.train_start) & (tl["exit_ts"] < w.train_end)]
    assert (train["entry_ts"] < w.train_end).all() and (train["exit_ts"] < w.train_end).all()

    rates = _known_rates(funding, w.train_end) if kind == "perp" else {}
    params: dict[str, str] = {}
    worst_for: list[str] = []
    chosen: list[pd.DataFrame] = []
    use_regime = ablation != "no_regime"
    strategies: list[GridStrategy] = []
    for name, cls in STRATEGIES.items():
        st = train[(train["strategy"] == name) & train["key"].isin(keys[name])]
        if st.empty:
            continue
        pid, worst = _choose_params(st, kind, rates)
        grid = _grid_params(cls)
        if pid not in grid:
            raise ValueError(f"{name}: params_id {pid!r} is not a GRID point")
        params[name] = pid
        if worst:
            worst_for.append(name)
        chosen.append(st[st["params_id"] == pid])
        strategies.append(cls(grid[pid], use_regime=use_regime))

    rk = settings.ranking
    table = (fit_expectancy(pd.concat(chosen, ignore_index=True), rk.n0, rk.n_min,
                            rk.bootstrap_samples, rk.ci) if chosen else {})
    ranker: Ranker = RandomRanker(seed) if ablation == "random_ranker" else ExpectancyRanker(table, params)
    if kind != "perp":
        rule = "r_net (spot: no funding)"
    elif worst_for:
        rule = "worst_case(0x, 2x est) for " + ",".join(worst_for)
    else:
        rule = "r_net (no unknown funding)"
    return WindowModel(w, universe, pairs, params, table, rule, ranker, strategies, keys)


# --- OOS simulation -------------------------------------------------------------------------------


class WindowStrategy:
    """One window's strategy for ``name``: entries from ``inner`` (None = no entries this window);
    exits of positions carried in from earlier windows (``carried``: position id -> (strategy
    instance that opened it, key)) are evaluated with that owner instance, on its own signal frame,
    even if the key is no longer traded. Carried keys outside ``keys`` get a zero-entry frame."""

    def __init__(self, name: str, inner: GridStrategy | None,
                 carried: dict[str, tuple[object, str]]) -> None:
        self.name = name
        self.inner = inner
        self.params = inner.params if inner is not None else {}
        self.carried = carried
        self._exit_sf: dict[str, SignalFrame] = {}

    def signals(self, panel: dict[str, pd.DataFrame], keys: list[str]) -> dict[str, SignalFrame]:
        out = self.inner.signals(panel, keys) if self.inner is not None and keys else {}
        cache: dict[tuple[int, str], SignalFrame] = {}
        for pid, (owner, key) in self.carried.items():
            ck = (id(owner), key)
            if ck not in cache:
                cache[ck] = owner.signals(panel, [key])[key]
            sf = cache[ck]
            self._exit_sf[pid] = sf
            if key not in out:
                out[key] = dataclasses.replace(sf, entry=pd.Series(0, index=sf.entry.index, dtype="int8"))
        return out

    def candidate_at(self, sf, i, mids):
        return self.inner.candidate_at(sf, i, mids) if self.inner is not None else None

    def should_exit(self, sf, i, pos, bar_close):
        osf = self._exit_sf.get(pos.id)
        if osf is None:
            return self.inner.should_exit(sf, i, pos, bar_close) if self.inner is not None else None
        owner = self.carried[pos.id][0]
        j = int(osf.entry.index.get_indexer([sf.entry.index[i]])[0])
        return owner.should_exit(osf, j, pos, bar_close) if j >= 0 else None


def _variant(settings: Settings, kind: AccountKind, max_drawdown_override: float | None) -> Settings:
    if max_drawdown_override is None:
        return settings
    accounts = dict(settings.accounts)
    accounts[kind] = accounts[kind].model_copy(update={"max_drawdown": float(max_drawdown_override)})
    return settings.model_copy(update={"accounts": accounts})


def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [f for f in frames if len(f)]
    if not nonempty:
        return frames[0].copy()
    return pd.concat(nonempty, ignore_index=True) if len(nonempty) > 1 else nonempty[0].copy()


def _stitch(results: list[BacktestResult]) -> BacktestResult:
    first, last = results[0], results[-1]
    bench = [r.benchmark for r in results]
    return BacktestResult(
        equity=pd.concat([r.equity for r in results]),
        trades=_concat([r.trades for r in results]),
        candidates=_concat([r.candidates for r in results]),
        risk_events=_concat([r.risk_events for r in results]),
        funding_known_from=dict(last.funding_known_from),
        bar_stats=pd.concat([r.bar_stats for r in results]),
        benchmark=pd.concat(bench) if all(b is not None for b in bench) else None,
        portfolio=last.portfolio,
        risk_state=last.risk_state,
        pending_exits=last.pending_exits,
        initial_equity=first.initial_equity,
    )


def _row(w: Window, status: str, m: WindowModel | None, res: BacktestResult | None) -> dict:
    row: dict = {"train_start": w.train_start, "train_end": w.train_end, "test_start": w.test_start,
                 "test_end": w.test_end, "status": status}
    if m is None:
        return row
    row.update({
        "universe": ",".join(m.universe),
        "pairs_tested": m.pairs.tested,
        "pairs_fdr_pass": len(m.pairs.fdr_pass),
        "pairs_stable": len(m.pairs.stable),
        "pairs_selected": ",".join(m.pairs.selected),
        **{f"params_{n}": m.params.get(n) for n in STRATEGIES},
        "accepted_cells": sum(1 for (_, k, r), c in m.table.items() if k != ANY and r == ANY and c.accepted),
        "funding_rule": m.funding_rule,
    })
    if res is not None:
        t = res.trades
        row.update({
            "n_trades": len(t),
            "expectancy_r": float(t["r_multiple"].mean()) if len(t) else float("nan"),
            "net_pnl": float(t["pnl_net"].sum()) if len(t) else 0.0,
        })
    return row


def _simulate(
    models: list[WindowModel],
    panel, funding, settings: Settings, kind: AccountKind, cost_mult: float,
    funding_mode: FundingMode, instruments: dict[str, Instrument] | None,
) -> list[BacktestResult]:
    """Sequential test windows, carrying Portfolio / RiskState / PendingExits."""
    results: list[BacktestResult] = []
    owners: dict[str, tuple[object, str]] = {}  # open position id -> (opening strategy, key)
    pf = state = exiting = None
    for m in models:
        inner = {s.name: s for s in m.strategies}
        carried: dict[str, dict[str, tuple[object, str]]] = {}
        for pid, (owner, key) in owners.items():
            carried.setdefault(pf.positions[pid].strategy, {})[pid] = (owner, key)
        names = [n for n in STRATEGIES if n in inner or n in carried]
        strats = [WindowStrategy(n, inner.get(n), carried.get(n, {})) for n in names]
        res = run_backtest(
            panel, funding, strats, m.keys, m.ranker, kind, settings,
            m.window.test_start, m.window.test_end, cost_mult, funding_mode,
            initial_portfolio=pf, instruments=instruments, initial_state=state,
            initial_exiting=exiting,
        )
        results.append(res)
        pf, state = res.portfolio, res.risk_state
        exiting = res.pending_exits if res.pending_exits is not None else PendingExits()
        nxt: dict[str, tuple[object, str]] = {}
        for pid, pos in pf.positions.items():
            nxt[pid] = owners.get(pid) or (inner[pos.strategy], pos.key)
        owners = nxt
    return results


def run_walkforward(
    panel: dict[str, pd.DataFrame],
    funding: dict[str, pd.DataFrame],
    tradelists: pd.DataFrame,
    settings: Settings,
    kind: AccountKind,
    ablation: Ablation = "none",
    cost_mult: float = 1.0,
    funding_mode: FundingMode = "known_only",
    *,
    max_drawdown_override: float | None = None,
    data_start=None,
    data_end=None,
    instruments: dict[str, Instrument] | None = None,
    universe_panel: dict[str, pd.DataFrame] | None = None,
    seed: int = 0,
) -> WalkForwardResult:
    """Fit every window and simulate the test windows sequentially (module docstring).

    ``max_drawdown_override``: None = realistic config; e.g. 1.0 = "edge" variant (drawdown kill
    switch disabled). ``data_start`` defaults to ``settings.universe.history_start``, ``data_end``
    to the last bar of the panel + 15 min. ``panel`` must hold the full history (the universe's
    history rule looks back ``min_history_days``)."""
    data_start, data_end = _bounds(panel, settings, data_start, data_end)
    windows, _ = make_windows(data_start, data_end, settings.walkforward)
    sim_settings = _variant(settings, kind, max_drawdown_override)
    models: list[WindowModel] = []
    rows: list[tuple[Window, str, WindowModel | None]] = []
    for k, w in enumerate(windows):
        m = fit_window(w, tradelists, panel, settings, kind, funding=funding, ablation=ablation,
                       seed=seed + k, universe_panel=universe_panel)
        if not m.universe and not models:
            rows.append((w, "skipped: empty universe", None))
            continue
        models.append(m)
        rows.append((w, "ok" if m.universe else "empty universe: no entries", m))
    if not models:
        raise ValueError(f"no walk-forward window with a non-empty universe in {data_start}..{data_end}")
    results = _simulate(models, panel, funding, sim_settings, kind, cost_mult, funding_mode, instruments)
    by_window = dict(zip([m.window for m in models], results, strict=True))
    per_window = pd.DataFrame([_row(w, s, m, by_window.get(w)) for w, s, m in rows])
    return WalkForwardResult(models, _stitch(results), per_window)


def _bounds(panel, settings: Settings, data_start, data_end) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = _utc(data_start if data_start is not None else settings.universe.history_start)
    if data_end is None:
        data_end = max(df.index[-1] for df in panel.values() if len(df)) + BAR
    return start, _utc(data_end)


# --- trade lists for the whole period -------------------------------------------------------------


def candidate_keys(
    panel: dict[str, pd.DataFrame],
    settings: Settings,
    kind: AccountKind,
    windows: list[Window],
    *,
    universe_panel: dict[str, pd.DataFrame] | None = None,
) -> dict[str, list[str]]:
    """Every key any of ``windows`` can trade: union of universes and of selected pairs."""
    cfg = settings.universe
    assets: set[str] = set()
    pairs: set[str] = set()
    for w in windows:
        u = [a for a in select_universe(universe_panel or panel, w.train_end, cfg.min_adv_usd,
                                        cfg.min_history_days) if a in panel]
        assets |= set(u)
        if kind == "perp" and u:
            pairs |= set(select_pairs(panel, u, w.train_start, w.train_end, settings.pairs).selected)
    return {"momentum": sorted(assets), "mean_reversion": sorted(assets), "pairs": sorted(pairs)}


def build_walkforward_tradelists(
    panel: dict[str, pd.DataFrame],
    funding: dict[str, pd.DataFrame],
    settings: Settings,
    kind: AccountKind,
    *,
    use_regime: bool = True,
    data_start=None,
    data_end=None,
    include_holdout: bool = True,
    universe_panel: dict[str, pd.DataFrame] | None = None,
    cache_dir: Path | None = Path("var/research"),
) -> pd.DataFrame:
    """Trade lists of every grid point on every candidate key of every window (and of the holdout
    fit window), built once with the account's price band."""
    data_start, data_end = _bounds(panel, settings, data_start, data_end)
    windows, holdout = make_windows(data_start, data_end, settings.walkforward)
    if include_holdout:
        windows = [*windows, _holdout_window(holdout, settings.walkforward)]
    keys = candidate_keys(panel, settings, kind, windows, universe_panel=universe_panel)
    return build_all_tradelists(
        STRATEGIES, panel, funding, CostModel(settings.costs[kind]), kind, keys,
        band=settings.accounts[kind].max_price_band, use_regime=use_regime, cache_dir=cache_dir,
    )


# --- holdout --------------------------------------------------------------------------------------


def _holdout_window(holdout: tuple[pd.Timestamp, pd.Timestamp], cfg: WalkForwardConfig) -> Window:
    h0, h1 = holdout
    return Window(h0 - pd.DateOffset(months=cfg.train_months), h0, h0, h1)


def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_ROOT, capture_output=True,
                             text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else "unknown"


def _read_registry(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8") or "{}")


def run_holdout(
    panel: dict[str, pd.DataFrame],
    funding: dict[str, pd.DataFrame],
    tradelists: pd.DataFrame,
    settings: Settings,
    kind: AccountKind,
    *,
    registry: Path,
    ablation: Ablation = "none",
    cost_mult: float = 1.0,
    funding_mode: FundingMode = "known_only",
    max_drawdown_override: float | None = None,
    data_end=None,
    instruments: dict[str, Instrument] | None = None,
    universe_panel: dict[str, pd.DataFrame] | None = None,
    seed: int = 0,
) -> WalkForwardResult:
    """The one-shot holdout: fit on the ``train_months`` before the holdout, simulate the holdout
    (last ``holdout_months``) from a fresh account. Raises ``HoldoutAlreadyRun`` if ``registry``
    (JSON ``{account: {run_at, git_sha, window}}``) already records this account; the run is
    recorded after it completes (a crash does not consume the holdout)."""
    registry = Path(registry)
    if kind in _read_registry(registry):
        raise HoldoutAlreadyRun(f"holdout for {kind} already run: {_read_registry(registry)[kind]}")
    _, data_end = _bounds(panel, settings, settings.universe.history_start, data_end)
    _, holdout = make_windows(data_end, data_end, settings.walkforward)
    w = _holdout_window(holdout, settings.walkforward)
    m = fit_window(w, tradelists, panel, settings, kind, funding=funding, ablation=ablation,
                   seed=seed, universe_panel=universe_panel)
    if not m.universe:
        raise ValueError(f"empty {kind} universe at the holdout start {w.train_end}")
    (res,) = _simulate([m], panel, funding, _variant(settings, kind, max_drawdown_override), kind,
                       cost_mult, funding_mode, instruments)
    out = WalkForwardResult([m], res, pd.DataFrame([_row(w, "holdout", m, res)]))

    reg = _read_registry(registry)
    if kind in reg:  # recorded by a concurrent run meanwhile
        raise HoldoutAlreadyRun(f"holdout for {kind} already run: {reg[kind]}")
    reg[kind] = {
        "run_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "git_sha": _git_sha(),
        "window": {f.name: getattr(w, f.name).isoformat() for f in dataclasses.fields(w)},
    }
    registry.parent.mkdir(parents=True, exist_ok=True)
    tmp = registry.with_name(f"{registry.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(reg, indent=1), encoding="utf-8")
    os.replace(tmp, registry)
    return out
