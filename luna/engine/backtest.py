"""Bar-by-bar portfolio backtest (spec §3.3, §6, §7, §8): ranker -> constructor -> planner -> risk
-> SimBroker -> Portfolio.

Timing. Bars are indexed by their open time ``t``. All decisions for bar ``t`` are taken at its
close and use only bars ``<= t`` (signals are causal and are read at row ``t``). Orders are stamped
``t`` and SimBroker fills them on bar ``t + 1`` (open for IOC, through-the-limit for post-only).
The risk engine's clock is the bar-open time too: ``roll_day``/``apply_actions`` get ``now = t``
and ``RiskContext(now=t, last_bar_ts=t)``, so the bar opening 23:45 (closing at midnight) still
belongs to the old UTC day and R16 (stale data) never fires on the backtest's own bars.

Window continuity (walk-forward): the result carries ``portfolio``, ``risk_state`` (kill switch,
day start, peak, orders today) and ``pending_exits`` (positions being flattened and their exit
orders still waiting for the next bar). Passing them as ``initial_portfolio`` / ``initial_state``
/ ``initial_exiting`` continues the run as if it had not been split, except that entry orders
decided on the last bar of a window are dropped (the next window decides afresh).

Per bar ``t`` of the union index inside ``[start, end)`` (steps 1, 4-6 are the shared
``luna.engine.pipeline.Pipeline`` steps, also run by the live engine):

1. fills of the orders decided at the previous bar (``SimBroker.on_bar``). The first entry fill
   registers the position; a pairs entry with only some legs filled is a ``leg_failure`` and the
   filled legs are flattened at this bar's decision.
2. emergency stops (§6.2): worst intrabar P&L of each open position from the bar's high/low; if the
   loss reaches ``emergency_stop_frac * equity`` the position is closed via ``SimBroker.stop_fill``
   at the level where the loss equals the threshold (the bar open if it gapped through).
3. funding (perp) at the hour boundary that ends the bar (``t + 15min`` on the hour).
4. mark at close, ``roll_day`` -> ``portfolio_actions`` -> ``apply_actions``; FLATTEN_AND_KILL
   flattens every position (closes still pass the risk engine).
5. exits: ``strategy.should_exit`` at the close; positions being exited are re-planned every bar
   until flat (unfilled IOC / post-only exits retry); exit plan dust is written off at the mark.
6. entries: candidates -> ranker -> ``construct`` -> ``plan_entry`` -> ``risk.check`` -> submit.
   All legs of a selection are checked as one batch and submitted all-or-nothing.

Every order passes ``risk.engine.check``. The only fills that do not are emergency stops, which
model a protective (exchange-side) stop of an already risk-approved position.

Funding modes for hours without a known rate (before the first published rate, or gaps):
``known_only`` -> unknown (no cash, ``funding_unknown_bars`` += 1), ``zero`` -> 0,
``estimated`` -> mean of the asset's known rates (pooled mean if it has none), ``estimated_x2`` ->
twice that. Known hours always use the actual rate; unknown hours are counted in
``funding_unknown_bars`` in every mode. The estimated modes are sensitivity scenarios only (the
mean is taken over the whole funding frame).
"""

from __future__ import annotations

import copy
import dataclasses
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from luna.config import Settings
from luna.engine.pipeline import CORR_WINDOW, Pipeline  # noqa: F401  (CORR_WINDOW re-exported)
from luna.execution.costs import CostModel
from luna.execution.sim import SimBroker
from luna.instruments import perp_symbol, spot_pair
from luna.portfolio.portfolio import Portfolio
from luna.ranking.ranker import Ranker
from luna.risk.state import RiskState
from luna.strategy.base import SignalFrame, Strategy
from luna.types import AccountKind, Fill, Instrument, OrderModel, OrderRequest

FundingMode = Literal["known_only", "zero", "estimated", "estimated_x2"]

BAR = pd.Timedelta("15min")

TRADE_COLUMNS = [
    "position_id", "strategy", "key", "direction", "entry_ts", "exit_ts", "exit_reason",
    "gross_notional", "risk_amount", "pnl_gross", "fees", "spread_slip", "funding",
    "funding_unknown_bars", "pnl_net", "r_multiple", "regime",
]
CANDIDATE_COLUMNS = [
    "ts", "strategy", "key", "direction", "score", "edge_bps", "stop_distance", "regime", "status",
    "reason",
]
EVENT_COLUMNS = ["ts", "kind", "position_id", "asset", "detail"]
# entry_gross_leverage: (gross at decision marks + entries submitted this bar) / equity, the quantity
# R8 bounds; NaN on bars without new entries. gross_leverage at the close may drift above the limit
# with prices (R8 applies at order time; there is no forced deleveraging).
# net_leverage: signed net notional / equity; max_asset_share: largest single-asset |notional| / equity
BAR_STAT_COLUMNS = ["equity", "gross_leverage", "n_positions", "open_risk", "entry_gross_leverage",
                    "net_leverage", "max_asset_share"]


@dataclass
class BacktestResult:
    equity: pd.Series  # equity at each bar close, indexed by bar open time
    trades: pd.DataFrame  # TRADE_COLUMNS, one row per closed position
    candidates: pd.DataFrame  # CANDIDATE_COLUMNS; status rejected|submitted|filled|partial|unfilled
    risk_events: pd.DataFrame  # EVENT_COLUMNS: vetoes, portfolio actions, stops, leg failures
    funding_known_from: dict[str, pd.Timestamp]  # first known funding rate per asset (NaT: none)
    bar_stats: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=BAR_STAT_COLUMNS))
    benchmark: pd.Series | None = None  # BTC close over the run (None if BTC not in the panel)
    portfolio: Portfolio | None = None  # final state (open positions carry over to a next window)
    risk_state: RiskState | None = None  # final risk state (kill switch, day start, peak, orders)
    pending_exits: PendingExits | None = None  # flattens in progress at the end of the window
    initial_equity: float = math.nan  # equity before the first bar (daily-return base)


@dataclass
class PendingExits:
    """Positions being exited (position_id -> exit reason) and their exit orders decided on the
    last bar, still to be filled on the next bar as (order, reference mid)."""

    reasons: dict[str, str] = field(default_factory=dict)
    orders: list[tuple[OrderRequest, float]] = field(default_factory=list)


def default_instruments(
    assets: list[str], kind: AccountKind, settings: Settings
) -> dict[str, Instrument]:
    """Permissive synthetic instruments (fine qty step, no minimum notional, 1% maintenance margin
    on perp). For research/tests only; reported runs should pass real exchange instruments."""
    cfg = settings.costs[kind]
    perp = kind == "perp"
    return {
        a: Instrument(
            asset=a,
            symbol=perp_symbol(a) if perp else spot_pair(a),
            venue="kraken_futures" if perp else "kraken_spot",
            kind=kind,
            qty_step=1e-8,
            price_tick=1e-8,
            min_qty=1e-8,
            min_notional=0.0,
            maker_fee=cfg.maker_fee,
            taker_fee=cfg.taker_fee,
            initial_margin=0.02 if perp else 0.0,
            maintenance_margin=0.01 if perp else 0.0,
        )
        for a in assets
    }


# --- precomputed market data ------------------------------------------------------------------------


class _Market:
    """Panel aligned to the union index as dense float64 / bool arrays (rows = bars, cols = assets)."""

    def __init__(self, panel: dict[str, pd.DataFrame], idx: pd.DatetimeIndex) -> None:
        self.idx = idx
        self.assets = sorted(panel)
        self.col = {a: j for j, a in enumerate(self.assets)}
        n, k = len(idx), len(self.assets)
        self.open = np.full((n, k), np.nan)
        self.high = np.full((n, k), np.nan)
        self.low = np.full((n, k), np.nan)
        self.close = np.full((n, k), np.nan)
        self.real = np.zeros((n, k), dtype=bool)  # bar exists and was traded (not forward-filled)
        for a, j in self.col.items():
            df = panel[a]
            if not df.index.is_unique:
                raise ValueError(f"duplicate bar timestamps for {a}")
            r = df.reindex(idx)
            for name, arr in (("open", self.open), ("high", self.high), ("low", self.low),
                              ("close", self.close)):
                arr[:, j] = r[name].to_numpy(dtype=float)
            filled = df["filled"].reindex(idx, fill_value=True).to_numpy(dtype=bool)
            self.real[:, j] = np.isfinite(self.close[:, j]) & ~filled
        present = np.isfinite(self.close)
        self.present = present
        self.last = pd.DataFrame(self.close).ffill().to_numpy()  # last known close (NaN before listing)
        with np.errstate(divide="ignore", invalid="ignore"):
            self.logret = np.diff(np.log(self.last), axis=0, prepend=np.nan)

    def bar(self, i: int, a: str) -> pd.Series:
        j = self.col[a]
        return pd.Series(
            {"open": float(self.open[i, j]), "high": float(self.high[i, j]),
             "low": float(self.low[i, j]), "close": float(self.close[i, j]),
             "filled": not bool(self.real[i, j])},
            name=self.idx[i],
        )


class _Funding:
    """Hourly funding rates on the bars whose close is an hour boundary."""

    def __init__(self, funding: dict[str, pd.DataFrame], mkt: _Market, mode: FundingMode) -> None:
        self.mode = mode
        ends = mkt.idx + BAR
        self.boundary = np.asarray(ends == ends.floor("h"))
        self.rate = np.full((len(mkt.idx), len(mkt.assets)), np.nan)
        self.known_from: dict[str, pd.Timestamp] = {}
        series: dict[str, pd.Series] = {}
        for a in mkt.assets:
            f = funding.get(a)
            s = pd.Series(dtype=float)
            if f is not None and len(f):
                s = f["rate"].dropna().astype(float)
                ix = pd.DatetimeIndex(s.index)
                ix = ix.tz_localize("UTC") if ix.tz is None else ix.tz_convert("UTC")
                s.index = ix.floor("h")
                s = s[~s.index.duplicated(keep="last")].sort_index()
            series[a] = s
            self.known_from[a] = s.index[0] if len(s) else pd.NaT
            if len(s):
                self.rate[self.boundary, mkt.col[a]] = s.reindex(ends[self.boundary]).to_numpy(dtype=float)
        known = [s for s in series.values() if len(s)]
        pooled_mean = float(pd.concat(known).mean()) if known else 0.0
        mult = {"known_only": math.nan, "zero": 0.0, "estimated": 1.0, "estimated_x2": 2.0}[mode]
        self.fallback = {a: (float(s.mean()) if len(s) else pooled_mean) * mult for a, s in series.items()}

    def rate_for(self, i: int, j: int, asset: str) -> tuple[float | None, bool]:
        """(rate to apply, known?). Unknown under known_only -> (None, False)."""
        r = self.rate[i, j]
        if math.isfinite(r):
            return float(r), True
        if self.mode == "known_only":
            return None, False
        return self.fallback[asset], False


@dataclass
class _Signal:
    strategy: Strategy
    sf: SignalFrame
    rows: np.ndarray  # row in sf for each union-index bar, -1 if absent


# --- engine -----------------------------------------------------------------------------------------


def run_backtest(
    panel: dict[str, pd.DataFrame],
    funding: dict[str, pd.DataFrame],
    strategies: list[Strategy],
    keys: dict[str, list[str]],
    ranker: Ranker | Callable[[pd.Timestamp], Ranker],
    kind: AccountKind,
    settings: Settings,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cost_mult: float = 1.0,
    funding_mode: FundingMode = "known_only",
    initial_portfolio: Portfolio | None = None,
    *,
    instruments: dict[str, Instrument] | None = None,
    initial_state: RiskState | None = None,
    initial_exiting: PendingExits | None = None,
    order_model: OrderModel | None = None,
    flatten_at_end: bool = False,
) -> BacktestResult:
    """Simulate [start, end) on the union of the panel's bar indexes (see module docstring).

    `ranker` is a Ranker or a factory ``ts -> Ranker`` (e.g. walk-forward window rankers).
    `initial_portfolio` is deep-copied; the final state is returned in ``result.portfolio``.
    `initial_state` / `initial_exiting` continue a previous window's ``risk_state`` /
    ``pending_exits`` (see "Window continuity").
    `instruments` defaults to `default_instruments` (synthetic, permissive).
    `order_model` (None = account default: perp IOC taker, spot post-only maker) sets the entry and
    exit order type and the constructor's round-trip fee side.
    `flatten_at_end` (Research V2 engine switch): after the last bar every open position is closed at
    that bar, each leg at the worse of its open and close with taker frictions and fee (a forward-filled
    bar: its last close with frictions), exit reason ``model_switch``; nothing is left pending.
    """
    if funding_mode not in ("known_only", "zero", "estimated", "estimated_x2"):
        raise ValueError(f"unknown funding_mode {funding_mode!r}")
    limits = settings.accounts[kind]
    costs = CostModel(settings.costs[kind])
    instruments = instruments or default_instruments(sorted(panel), kind, settings)
    missing = set(panel) - set(instruments)
    if missing:
        raise ValueError(f"no instrument for {sorted(missing)}")
    pf = copy.deepcopy(initial_portfolio) if initial_portfolio is not None else Portfolio(
        kind, limits.capital_usd
    )
    if pf.kind != kind:
        raise ValueError(f"initial portfolio is {pf.kind}, run is {kind}")
    held_unknown = {a for p in pf.positions.values() for a in p.qty} - set(panel)
    if held_unknown:
        raise ValueError(f"initial positions hold assets missing from the panel: {held_unknown}")

    full_idx = pd.DatetimeIndex([])
    for df in panel.values():
        full_idx = full_idx.union(df.index)
    full_idx = pd.DatetimeIndex(full_idx).sort_values()
    mkt = _Market(panel, full_idx)
    i0 = int(full_idx.searchsorted(start, side="left"))
    i1 = int(full_idx.searchsorted(end, side="left"))
    fund = _Funding(funding, mkt, funding_mode) if kind == "perp" else None

    # signals: once per (strategy, key), mapped onto the union index
    signals: dict[tuple[str, str], _Signal] = {}
    fires: dict[int, list[tuple[str, str]]] = {}
    for strat in strategies:
        for key, sf in strat.signals(panel, list(keys.get(strat.name, []))).items():
            rows = sf.entry.index.get_indexer(full_idx)
            signals[(strat.name, key)] = _Signal(strat, sf, rows)
            entry = sf.entry.to_numpy()
            ok = rows >= 0
            nz = np.zeros(len(full_idx), dtype=bool)
            nz[ok] = entry[rows[ok]] != 0
            for i in np.flatnonzero(nz[i0:i1]) + i0:
                fires.setdefault(int(i), []).append((strat.name, key))

    broker = SimBroker(kind, costs, instruments, cost_mult)
    state = dataclasses.replace(initial_state) if initial_state is not None else RiskState()
    pipe = Pipeline(pf, state, kind, limits, costs, instruments, broker, "backtest",
                    order_model=order_model)
    if initial_exiting is not None:
        stray = (set(initial_exiting.reasons) | {o.position_id for o, _ in initial_exiting.orders}) - set(
            pf.positions
        )
        if stray:
            raise ValueError(f"pending exits for positions not in the portfolio: {sorted(stray)}")
        pipe.exiting.update(initial_exiting.reasons)
        for o, ref in initial_exiting.orders:
            if o.intent == "open":
                raise ValueError(f"pending exit order {o.client_id} is an open")
            broker.submit(o)
            pipe.submitted[o.client_id] = (o, ref)
    initial_equity = float(pf.equity())
    n_out = i1 - i0
    equity = np.empty(max(n_out, 0))
    stats = np.empty((max(n_out, 0), len(BAR_STAT_COLUMNS)))

    for i in range(i0, i1):
        t = full_idx[i]
        last = mkt.last[i]

        # (1) fills of orders decided at the previous bar (every one is consumed on this bar)
        if pipe.submitted:
            bars = {
                a: {"open": float(mkt.open[i, j]), "high": float(mkt.high[i, j]),
                    "low": float(mkt.low[i, j]), "close": float(mkt.close[i, j]),
                    "filled": not bool(mkt.real[i, j])}
                for a, j in mkt.col.items() if mkt.present[i, j]
            }
            for f in broker.on_bar(t, bars):
                pipe.apply_fill(f)
            pipe.settle(t)

        # (2) emergency stops on bar t's range
        if pf.positions:
            threshold = limits.emergency_stop_frac * pf.equity()
            # equity <= 0: no meaningful threshold; the drawdown kill switch owns that case
            for pid, pos in list(pf.positions.items()) if threshold > 0 else []:
                legs = [(a, q) for a, q in pos.qty.items() if q != 0.0]
                if not legs or not all(mkt.real[i, mkt.col[a]] for a, _ in legs):
                    continue
                base = pos.net_pnl()
                at_open = base + sum(q * (mkt.open[i, mkt.col[a]] - pos.entry_ref_price[a]) for a, q in legs)
                worst_px = {a: mkt.low[i, mkt.col[a]] if q > 0 else mkt.high[i, mkt.col[a]] for a, q in legs}
                worst = base + sum(q * (worst_px[a] - pos.entry_ref_price[a]) for a, q in legs)
                if not worst <= -threshold:
                    continue
                if at_open <= -threshold or worst == at_open:
                    lam = 0.0
                else:
                    lam = (-threshold - at_open) / (worst - at_open)
                for a, q in legs:
                    o = float(mkt.open[i, mkt.col[a]])
                    stop_px = float(o + lam * (worst_px[a] - o))
                    f = broker.stop_fill(a, "sell" if q > 0 else "buy", abs(q), stop_px, mkt.bar(i, a), pid)
                    pf.apply_fill(f, pid, stop_px)
                pipe.event(t, "emergency_stop", pid, ",".join(a for a, _ in legs),
                           f"intrabar loss {-worst:.4f} >= {threshold:.4f}")
                pipe.finish(pid, t, "emergency_stop")

        # (3) funding at the hour boundary closing this bar
        if fund is not None and fund.boundary[i] and pf.holdings:
            ts_f = t + BAR
            for a in list(pf.holdings):
                j = mkt.col[a]
                rate, known = fund.rate_for(i, j, a)
                pf.apply_funding(a, rate, float(last[j]), ts_f)
                if not known and rate is not None:  # estimated scenario: still flag unknown hours
                    for p in pf.positions.values():
                        if p.qty.get(a, 0.0) != 0.0:
                            p.pnl["funding_unknown_bars"] += 1

        # (4) mark, day roll, portfolio-level actions
        pipe.portfolio_step(t, {a: float(last[mkt.col[a]]) for a in pf.holdings})

        # (5) exits
        pipe.exit_signals(i, mkt, signals)
        pipe.flatten(t, lambda a, last=last: float(last[mkt.col[a]]))

        # (6) entries
        firing = fires.get(i)
        entry_lev = math.nan
        if firing:
            entry_lev = pipe.entries(i, t, firing, mkt, signals, ranker)

        k = i - i0
        eq = pf.equity()
        equity[k] = eq
        notional = {a: pf.notional(a) for a in pf.holdings}
        net = sum(math.copysign(v, pf.holdings[a]) for a, v in notional.items())
        big = max(notional.values(), default=0.0)
        stats[k] = (eq, pf.gross_leverage(), len(pf.positions), pf.open_risk(), entry_lev,
                    net / eq if eq > 0 else math.nan, big / eq if eq > 0 else math.nan)

    if flatten_at_end and i1 > i0 and pf.positions:
        i = i1 - 1
        t = full_idx[i]
        for pid, pos in list(pf.positions.items()):
            for a, q in list(pos.qty.items()):
                if q == 0.0:
                    continue
                j = mkt.col[a]
                side = "sell" if q > 0 else "buy"
                mark = float(mkt.last[i, j])
                if mkt.real[i, j]:
                    f = broker.stop_fill(a, side, abs(q), float(mkt.close[i, j]), mkt.bar(i, a), pid)
                else:
                    fr = (costs.half_spread(a) + costs.slippage) * cost_mult
                    px = mark * (1.0 - fr if side == "sell" else 1.0 + fr)
                    f = Fill(f"{pid}:switch:{a}", t, a, side, abs(q), px,
                             costs.fee(abs(q) * px, "taker") * cost_mult, "taker")
                pf.apply_fill(f, pid, mark)
            pipe.finish(pid, t, "model_switch")
        pipe.exiting.clear()
        pipe.submitted.clear()
        eq = pf.equity()
        equity[-1] = eq
        stats[-1, 0] = eq
        stats[-1, 2] = 0

    out_idx = full_idx[i0:i1]
    benchmark = None
    if "BTC" in mkt.col:
        benchmark = pd.Series(mkt.last[i0:i1, mkt.col["BTC"]], index=out_idx, name="BTC")
    bar_stats = pd.DataFrame(stats, index=out_idx, columns=BAR_STAT_COLUMNS)
    bar_stats["n_positions"] = bar_stats["n_positions"].astype("int64")
    return BacktestResult(
        equity=pd.Series(equity, index=out_idx, name="equity"),
        trades=_frame(pipe.trades, TRADE_COLUMNS),
        candidates=_frame(pipe.cand_rows, CANDIDATE_COLUMNS),
        risk_events=_frame(pipe.events, EVENT_COLUMNS),
        funding_known_from=dict(fund.known_from) if fund is not None else {},
        bar_stats=bar_stats,
        benchmark=benchmark,
        portfolio=pf,
        risk_state=pipe.state,
        pending_exits=PendingExits(
            reasons=dict(pipe.exiting),
            orders=[(o, ref) for o, ref in pipe.submitted.values() if o.intent != "open"],
        ),
        initial_equity=initial_equity,
    )


def _frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)

