"""Research V2 walk-forward (spec §3, plan P9-P11): train-only fitting per configuration and window,
the nested account candidate, and the out-of-sample simulations.

Every fit reads only trades with ``entry_ts >= train_start`` and ``exit_ts < train_end`` (signal
families: research trade lists; cross-sectional: train-window simulations from a fresh account with
the kill switch off). Universes follow V1's rule at ``train_end`` (``universe_at``), restricted by the
configuration's universe mode.

Signal configurations reuse V1's window machinery (``WindowModel``, ``_simulate``: carried positions
keep their owning strategy). Cross-sectional configurations run ``run_xsec`` window by window,
carrying the portfolio and risk state. The account candidate (P9) picks, per window, the accepted
configuration with the best train objective; consecutive candidate windows that change engine
(signal <-> cross-sectional) flatten at the outgoing window's last bar (P10, ``model_switch``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from luna.config import Settings
from luna.engine.backtest import FundingMode, PendingExits
from luna.engine.walkforward import (
    FUNDING_FLOOR,
    WalkForwardResult,
    Window,
    WindowModel,
    _check_train,
    _choose_params,
    _funding_cost_r,
    _grid_params,
    _known_rates,
    _open_at_end,
    _simulate,
    _stitch,
    _variant,
    universe_at,
)
from luna.execution.costs import CostModel
from luna.portfolio.portfolio import Portfolio
from luna.ranking.expectancy import ANY, ExpectancyRanker, fit_expectancy
from luna.report.walkforward import Inputs
from luna.research.pairs_select import PairSelection
from luna.research.tradelists import build_all_tradelists
from luna.risk.state import RiskState
from luna.types import AccountKind, OrderModel
from luna.v2.configs import V2Config, restrict_universe
from luna.v2.strategies import V2_STRATEGY_CLASSES
from luna.v2.xsec import XSecParams
from luna.v2.xsec_sim import run_xsec

EDGE = 1.0
NO_PAIRS = PairSelection(0, 0, [], [], [], pd.DataFrame())
WINDOW_COLUMNS = ["test_start", "test_end", "status", "config", "universe", "params", "accepted",
                  "objective", "n_trades", "expectancy_r", "net_pnl"]


@dataclass
class ConfigWindowModel:
    config: V2Config
    window: Window
    universe: list[str]
    params_id: str | None
    objective: float
    accepted: bool
    signal: WindowModel | None = None
    xsec: tuple[XSecParams, list[str]] | None = None
    note: str = ""


@dataclass
class _Carry:
    pf: Portfolio | None = None
    state: RiskState | None = None
    exiting: PendingExits = field(default_factory=PendingExits)


# --- trade lists ----------------------------------------------------------------------------------


def build_v2_tradelists(
    inp: Inputs, settings: Settings, account: AccountKind, windows: list[Window], *,
    order_model: OrderModel | None = None, cache_dir: Path | None = None,
) -> pd.DataFrame:
    """Trade lists of every V2 signal class's grid on every asset any window's universe holds."""
    assets: set[str] = set()
    for w in windows:
        assets |= set(universe_at(inp.panel, w.train_end, settings, inp.universe_panel))
    classes = V2_STRATEGY_CLASSES(account)
    keys = {name: sorted(assets) for name in classes}
    return build_all_tradelists(
        classes, inp.panel, inp.funding, CostModel(settings.costs[account]), account, keys,
        band=settings.accounts[account].max_price_band, use_regime=True, cache_dir=cache_dir,
        order_model=order_model,
    )


# --- fitting --------------------------------------------------------------------------------------


def _subset(inp: Inputs, assets: set[str]) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Panel / funding restricted to ``assets`` plus BTC (benchmark): the cross-sectional simulator
    rebuilds its market view per call, so it only gets the assets it can trade or holds."""
    keep = (set(assets) | {"BTC"}) & set(inp.panel)
    return ({a: inp.panel[a] for a in sorted(keep)},
            {a: f for a, f in inp.funding.items() if a in keep})


def _config_universe(cfg: V2Config, w: Window, inp: Inputs, settings: Settings,
                     exclude: tuple[str, ...]) -> list[str]:
    u = universe_at(inp.panel, w.train_end, settings, inp.universe_panel)
    return [a for a in restrict_universe(u, cfg.universe_mode) if a not in exclude]


def empty_model(w: Window, note: str = "flat") -> WindowModel:
    return WindowModel(w, [], NO_PAIRS, {}, {}, note, ExpectancyRanker({}, {}), [], {})


def fit_signal_config(cfg: V2Config, w: Window, tl: pd.DataFrame, inp: Inputs, settings: Settings,
                      exclude: tuple[str, ...] = ()) -> ConfigWindowModel:
    kind = cfg.account
    name = cfg.strategy_name
    universe = _config_universe(cfg, w, inp, settings, exclude)
    st = tl[(tl["strategy"] == name) & tl["key"].isin(universe) & (tl["entry_ts"] >= w.train_start)
            & (tl["exit_ts"] < w.train_end)]
    if st.empty:
        m = empty_model(w, "no train trades")
        m.universe, m.keys = universe, {name: universe}
        return ConfigWindowModel(cfg, w, universe, None, math.nan, False, m, note="no train trades")
    _check_train(st, w, f"{cfg.id} param selection")
    rates = _known_rates(inp.funding, w.train_end) if kind == "perp" else {}
    pid, worst = _choose_params(st, kind, rates)
    chosen = st[st["params_id"] == pid]
    r = chosen["r_net"].astype(float)
    objective = float(r.mean())
    if worst:
        objective = min(objective, float((r - _funding_cost_r(chosen, rates)).mean()))
    cls = V2_STRATEGY_CLASSES(kind)[name]
    grid = _grid_params(cls)
    rk = settings.ranking
    table = fit_expectancy(chosen, rk.n0, rk.n_min, rk.bootstrap_samples, rk.ci)
    accepted = any(c.accepted for (_, k, reg), c in table.items() if k != ANY and reg == ANY)
    rule = ("r_net (spot: no funding)" if kind != "perp" else
            "worst_case(0x, 2x max(|est|, floor) cost)" if worst else "r_net (no unknown funding)")
    ranker = ExpectancyRanker(table, {name: pid})
    model = WindowModel(w, universe, NO_PAIRS, {name: pid}, table, rule, ranker, [cls(grid[pid])],
                        {name: universe})
    return ConfigWindowModel(cfg, w, universe, pid, objective, accepted, model)


def _xsec_objective(trades: pd.DataFrame, kind: AccountKind, rates: dict[str, float]) -> float:
    r = trades["r_multiple"].astype(float)
    obj = float(r.mean())
    if kind == "perp" and bool((trades["funding_unknown_bars"] > 0).any()):
        pooled = float(np.mean(list(rates.values()))) if rates else 0.0
        cols = (trades["key"], trades["funding_unknown_bars"], trades["gross_notional"],
                trades["risk_amount"])
        cost = [2.0 * max(abs(rates.get(str(k), pooled)), FUNDING_FLOOR) * float(h) * float(g) / float(ra)
                for k, h, g, ra in zip(*cols, strict=True)]
        obj = min(obj, float((r - np.asarray(cost)).mean()))
    return obj


def fit_xsec_config(cfg: V2Config, w: Window, inp: Inputs, settings: Settings,
                    exclude: tuple[str, ...] = ()) -> ConfigWindowModel:
    kind = cfg.account
    universe = _config_universe(cfg, w, inp, settings, exclude)
    if not universe:
        return ConfigWindowModel(cfg, w, universe, None, math.nan, False, note="empty universe")
    edge = _variant(settings, kind, EDGE)
    rates = _known_rates(inp.funding, w.train_end) if kind == "perp" else {}
    best: tuple[float, str, XSecParams, pd.DataFrame] | None = None
    for g in cfg.grid():
        p = XSecParams(g["lookback_days"], g["k"], g.get("gross", 1.0), g.get("vol_target"), cfg.rebalance)
        panel, funding = _subset(inp, set(universe))
        res = run_xsec(panel, funding, p, universe, kind, edge, w.train_start, w.train_end,
                       name=cfg.strategy_name, instruments=inp.instruments)
        tr = res.trades
        if tr.empty:
            continue
        _check_train(tr, w, f"{cfg.id} {p.params_id}")
        obj = _xsec_objective(tr, kind, rates)
        if best is None or obj > best[0] or (obj == best[0] and p.params_id < best[1]):
            best = (obj, p.params_id, p, tr)
    if best is None:
        return ConfigWindowModel(cfg, w, universe, None, math.nan, False, note="no train trades")
    obj, pid, p, tr = best
    cells = tr.assign(strategy=cfg.strategy_name, key="portfolio", regime="NONE",
                      r_net=tr["r_multiple"].astype(float), day=pd.DatetimeIndex(tr["exit_ts"]).date)
    rk = settings.ranking
    table = fit_expectancy(cells, rk.n0, rk.n_min, rk.bootstrap_samples, rk.ci)
    accepted = bool(table[(cfg.strategy_name, "portfolio", ANY)].accepted)
    return ConfigWindowModel(cfg, w, universe, pid, obj, accepted, xsec=(p, universe))


def fit_config(cfg: V2Config, windows: list[Window], tl: pd.DataFrame, inp: Inputs, settings: Settings,
               exclude: tuple[str, ...] = ()) -> list[ConfigWindowModel]:
    if cfg.engine == "xsec":
        return [fit_xsec_config(cfg, w, inp, settings, exclude) for w in windows]
    return [fit_signal_config(cfg, w, tl, inp, settings, exclude) for w in windows]


def refit_without(cfg: V2Config, original: list[ConfigWindowModel], asset: str, tl: pd.DataFrame,
                  inp: Inputs, settings: Settings) -> list[ConfigWindowModel]:
    """Asset leave-one-out fit: windows whose universe held ``asset`` are re-fitted without it; the
    others keep their original fit (removing an absent asset changes nothing)."""
    fit = fit_xsec_config if cfg.engine == "xsec" else (
        lambda c, w, i, s, exclude: fit_signal_config(c, w, tl, i, s, exclude))
    return [fit(cfg, cw.window, inp, settings, exclude=(asset,)) if asset in cw.universe else cw
            for cw in original]


def first_active(fitted: dict[str, list[ConfigWindowModel]]) -> int:
    """Index of the first window in which some configuration has a non-empty universe (V1: leading
    windows with an empty universe are skipped)."""
    n = len(next(iter(fitted.values())))
    for k in range(n):
        if any(cws[k].universe for cws in fitted.values()):
            return k
    raise ValueError("no window with a non-empty universe")


def select_candidate(fitted: dict[str, list[ConfigWindowModel]]) -> list[ConfigWindowModel | None]:
    """Per window: the accepted configuration with the highest train objective (ties: smaller config
    id); None (flat) when none is accepted (P9)."""
    n = len(next(iter(fitted.values())))
    out: list[ConfigWindowModel | None] = []
    for k in range(n):
        ok = [cws[k] for cws in fitted.values() if cws[k].accepted and math.isfinite(cws[k].objective)]
        out.append(min(ok, key=lambda m: (-m.objective, m.config.id)) if ok else None)
    return out


# --- simulation -----------------------------------------------------------------------------------


def v2_variants(kind: AccountKind, *, candidate: bool) -> dict[str, dict]:
    """P11 variant name -> simulation keyword arguments."""
    edge = {"max_drawdown_override": EDGE}
    v: dict[str, dict] = {"realistic": {}, "edge": dict(edge)}
    if candidate:
        v["cost_2x"] = {"cost_mult": 2.0}
        v["cost_2x_edge"] = {"cost_mult": 2.0, **edge}
        v["order_alt_edge"] = {"order_model": "maker" if kind == "perp" else "taker", **edge}
        if kind == "perp":
            for mode in ("zero", "estimated", "estimated_x2"):
                v[f"funding_{mode}"] = {"funding_mode": mode, **edge}
    return v


def _row(w: Window, status: str, cw: ConfigWindowModel | None, res) -> dict:
    row = {"test_start": w.test_start, "test_end": w.test_end, "status": status,
           "config": cw.config.id if cw else None, "universe": ",".join(cw.universe) if cw else "",
           "params": cw.params_id if cw else None, "accepted": bool(cw.accepted) if cw else False,
           "objective": cw.objective if cw else math.nan}
    if res is not None:
        t = res.trades
        row.update({"n_trades": len(t),
                    "expectancy_r": float(t["r_multiple"].mean()) if len(t) else math.nan,
                    "net_pnl": float(t["pnl_net"].sum()) if len(t) else 0.0})
    return row


def _run_segment_signal(cws, windows, inp, settings, kind, carry: _Carry, flatten_last, *,
                        cost_mult, funding_mode, order_model):
    models = [cw.signal if cw is not None and cw.signal is not None else empty_model(w)
              for cw, w in zip(cws, windows, strict=True)]
    initial = (carry.pf, carry.state, carry.exiting) if carry.pf is not None else None
    results = _simulate(models, inp.panel, inp.funding, settings, kind, cost_mult, funding_mode,
                        inp.instruments, initial=initial, flatten_last=flatten_last, order_model=order_model)
    last = results[-1]
    carry.pf, carry.state = last.portfolio, last.risk_state
    carry.exiting = last.pending_exits if last.pending_exits is not None else PendingExits()
    return results


def _run_segment_xsec(cws, windows, inp, settings, kind, carry: _Carry, flatten_last, *,
                      cost_mult, funding_mode, order_model):
    results = []
    for n, (cw, w) in enumerate(zip(cws, windows, strict=True)):
        if cw is not None and cw.xsec is not None and cw.accepted:
            params, universe = cw.xsec
        else:
            params, universe = XSecParams(30, 1), []
        name = cw.config.strategy_name if cw is not None else "xsec"
        held = set(carry.pf.holdings) if carry.pf is not None else set()
        panel, funding = _subset(inp, set(universe) | held)
        res = run_xsec(panel, funding, params, universe, kind, settings, w.test_start, w.test_end,
                       name=name, cost_mult=cost_mult, funding_mode=funding_mode,
                       instruments=inp.instruments, initial_portfolio=carry.pf, initial_state=carry.state,
                       flatten_at_end=flatten_last and n == len(cws) - 1, order_model=order_model)
        carry.pf, carry.state = res.portfolio, res.risk_state
        results.append(res)
    carry.exiting = PendingExits()
    return results


def _simulate_plan(plan: list[ConfigWindowModel | None], windows: list[Window], engines: list[str],
                   inp: Inputs, settings: Settings, kind: AccountKind, *, cost_mult: float = 1.0,
                   funding_mode: FundingMode = "known_only", max_drawdown_override: float | None = None,
                   order_model: OrderModel | None = None):
    sim = _variant(settings, kind, max_drawdown_override)
    carry = _Carry()
    results = []
    k = 0
    while k < len(plan):
        j = k
        while j + 1 < len(plan) and engines[j + 1] == engines[k]:
            j += 1
        run = _run_segment_xsec if engines[k] == "xsec" else _run_segment_signal
        results += run(plan[k:j + 1], windows[k:j + 1], inp, sim, kind, carry, j + 1 < len(plan),
                       cost_mult=cost_mult, funding_mode=funding_mode, order_model=order_model)
        k = j + 1
    return results


def _result(plan, windows, skipped: list[Window], results, signal_models) -> WalkForwardResult:
    rows = [_row(w, "skipped: empty universe", None, None) for w in skipped]
    rows += [_row(w, "ok" if cw is not None else "flat: no accepted configuration", cw, r)
             for cw, w, r in zip(plan, windows, results, strict=True)]
    oos = _stitch(results)
    return WalkForwardResult(signal_models, oos, pd.DataFrame(rows), _open_at_end(oos.portfolio))


def run_config_walkforward(fitted: list[ConfigWindowModel], start_k: int, inp: Inputs, settings: Settings,
                           **variant) -> WalkForwardResult:
    """One configuration's OOS walk-forward from window ``start_k`` (earlier windows are skipped)."""
    plan = fitted[start_k:]
    windows = [cw.window for cw in plan]
    engine = plan[0].config.engine
    results = _simulate_plan(plan, windows, [engine] * len(plan), inp, settings, plan[0].config.account,
                             **variant)
    models = [cw.signal for cw in plan if cw.signal is not None]
    return _result(plan, windows, [cw.window for cw in fitted[:start_k]], results, models)


def run_selected_walkforward(selected: list[ConfigWindowModel | None], windows: list[Window], start_k: int,
                             inp: Inputs, settings: Settings, kind: AccountKind,
                             **variant) -> WalkForwardResult:
    """The account candidate's OOS walk-forward (P9, P10); flat windows run the signal engine with
    no entries."""
    plan = selected[start_k:]
    wins = windows[start_k:]
    engines = [cw.config.engine if cw is not None else "signal" for cw in plan]
    results = _simulate_plan(plan, wins, engines, inp, settings, kind, **variant)
    models = [cw.signal for cw in plan if cw is not None and cw.signal is not None]
    return _result(plan, wins, windows[:start_k], results, models)


def count_trials(configs: list[V2Config]) -> dict:
    """Configurations and grid points tried (§3.3); every grid point of every configuration counts."""
    return {"configs": len(configs), "grid_points": sum(len(c.grid()) for c in configs)}
