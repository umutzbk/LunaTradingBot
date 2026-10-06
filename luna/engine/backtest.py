"""Bar-by-bar portfolio backtest (spec §3.3, §6, §7, §8): ranker -> constructor -> planner -> risk
-> SimBroker -> Portfolio.

Timing. Bars are indexed by their open time ``t``. All decisions for bar ``t`` are taken at its
close and use only bars ``<= t`` (signals are causal and are read at row ``t``). Orders are stamped
``t`` and SimBroker fills them on bar ``t + 1`` (open for IOC, through-the-limit for post-only).

Per bar ``t`` of the union index inside ``[start, end)``:

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
from luna.execution.costs import CostModel
from luna.execution.sim import SimBroker
from luna.instruments import perp_symbol, spot_pair
from luna.planner import plan_entry, plan_exit
from luna.portfolio.constructor import Selection, construct
from luna.portfolio.portfolio import Portfolio, Position
from luna.ranking.ranker import Ranker
from luna.risk.engine import OrderCheck, RiskContext, apply_actions, check, portfolio_actions
from luna.risk.state import RiskState, roll_day
from luna.strategy.base import SignalFrame, Strategy
from luna.types import AccountKind, Candidate, Instrument, OrderRequest

FundingMode = Literal["known_only", "zero", "estimated", "estimated_x2"]

BAR = pd.Timedelta("15min")
CORR_WINDOW = 30 * 96  # 30 days of 15-minute bars

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
BAR_STAT_COLUMNS = ["equity", "gross_leverage", "n_positions", "open_risk", "entry_gross_leverage"]


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


@dataclass
class _EntryGroup:
    sel: Selection
    assets: set[str]
    cand_row: int
    filled: set[str] = field(default_factory=set)


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
) -> BacktestResult:
    """Simulate [start, end) on the union of the panel's bar indexes (see module docstring).

    `ranker` is a Ranker or a factory ``ts -> Ranker`` (e.g. walk-forward window rankers).
    `initial_portfolio` is deep-copied; the final state is returned in ``result.portfolio``.
    `instruments` defaults to `default_instruments` (synthetic, permissive).
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
    band = limits.max_price_band
    min_qty = {a: inst.min_qty for a, inst in instruments.items()}
    min_notional = {a: inst.min_notional for a, inst in instruments.items()}

    def regime_mult(c: Candidate) -> float:
        return limits.highvol_risk_mult if c.regime == "HIGH_VOL" else 1.0

    state = RiskState()
    submitted: dict[str, tuple[OrderRequest, float]] = {}  # client_id -> (order, ref price)
    entry_groups: dict[str, _EntryGroup] = {}
    exiting: dict[str, str] = {}  # position_id -> exit reason
    entry_notional: dict[str, float] = {
        pid: sum(abs(q) * p.entry_price.get(a, 0.0) for a, q in p.qty.items())
        for pid, p in pf.positions.items()
    }
    trades: list[dict] = []
    cand_rows: list[dict] = []
    events: list[dict] = []
    n_out = i1 - i0
    equity = np.empty(max(n_out, 0))
    stats = np.empty((max(n_out, 0), len(BAR_STAT_COLUMNS)))

    def event(ts, kind_, pid="", asset="", detail=""):
        events.append({"ts": ts, "kind": kind_, "position_id": pid, "asset": asset, "detail": detail})

    def cand_row(c: Candidate, status: str, reason: str | None) -> int:
        cand_rows.append({
            "ts": c.ts, "strategy": c.strategy, "key": c.key, "direction": c.direction,
            "score": c.score, "edge_bps": c.edge_bps, "stop_distance": c.stop.distance_frac,
            "regime": c.regime, "status": status, "reason": reason,
        })
        return len(cand_rows) - 1

    def finish(pid: str, ts: pd.Timestamp, reason: str) -> None:
        pos = pf.close_position(pid, ts, reason)
        exiting.pop(pid, None)
        p = pos.pnl
        net = pos.net_pnl()
        trades.append({
            "position_id": pid, "strategy": pos.strategy, "key": pos.key,
            "direction": pos.direction, "entry_ts": pos.entry_ts, "exit_ts": ts,
            "exit_reason": reason, "gross_notional": entry_notional.pop(pid, math.nan),
            "risk_amount": pos.risk_amount, "pnl_gross": p["gross"], "fees": p["fees"],
            "spread_slip": p["spread_slip"], "funding": p["funding"],
            "funding_unknown_bars": int(p["funding_unknown_bars"]), "pnl_net": net,
            "r_multiple": net / pos.risk_amount if pos.risk_amount > 0 else math.nan,
            "regime": pos.regime,
        })

    def ctx(t, eq, st, *, extra_pos=0, extra_risk=0.0, extra_gross=0.0, holdings=None,
            cash_used=0.0, liq=None) -> RiskContext:
        return RiskContext(
            limits=limits, kind=kind, mode="backtest", state=st, equity=float(eq),
            open_positions=len(pf.positions) + extra_pos,
            open_risk=pf.open_risk() + extra_risk,
            gross_notional=pf.gross_notional() + extra_gross,
            holdings=dict(pf.holdings) if holdings is None else holdings,
            last_bar_ts=t, now=t, liq_distance=liq or {}, min_qty=min_qty,
            min_notional=min_notional, available_cash=pf.cash - cash_used,
            open_position_ids=frozenset(pf.positions),
        )

    def veto_events(t, vetoed):
        for o, reason in vetoed:
            event(t, "veto", o.position_id, o.asset, reason)

    for i in range(i0, i1):
        t = full_idx[i]
        last = mkt.last[i]

        # (1) fills of orders decided at the previous bar
        if submitted:
            bars = {
                a: {"open": float(mkt.open[i, j]), "high": float(mkt.high[i, j]),
                    "low": float(mkt.low[i, j]), "close": float(mkt.close[i, j]),
                    "filled": not bool(mkt.real[i, j])}
                for a, j in mkt.col.items() if mkt.present[i, j]
            }
            touched: set[str] = set()
            for f in broker.on_bar(t, bars):
                order, ref = submitted[f.client_id]
                pid = order.position_id
                if order.intent == "open":
                    g = entry_groups[pid]
                    if pid not in pf.positions:
                        c = g.sel.cand
                        pf.open_position(Position(
                            id=pid, strategy=c.strategy, key=c.key, legs=c.legs,
                            direction=c.direction, qty={}, entry_price={}, entry_ts=f.ts,
                            entry_ref=c.entry_ref, stop=c.stop, exit=c.exit,
                            risk_amount=g.sel.risk_amount, regime=c.regime,
                        ))
                    pf.apply_fill(f, pid, ref)
                    entry_notional[pid] = entry_notional.get(pid, 0.0) + f.qty * f.price
                    g.filled.add(f.asset)
                elif pid in pf.positions:
                    pf.apply_fill(f, pid, ref)
                    touched.add(pid)
            submitted.clear()  # every pending order was decided before t: all consumed
            for pid, g in entry_groups.items():
                if not g.filled:
                    cand_rows[g.cand_row]["status"] = "unfilled"
                elif g.filled != g.assets:
                    cand_rows[g.cand_row]["status"] = "partial"
                    event(t, "leg_failure", pid, ",".join(sorted(g.assets - g.filled)),
                          "entry legs unfilled; flattening filled legs")
                    exiting[pid] = "leg_failure"
                else:
                    cand_rows[g.cand_row]["status"] = "filled"
            entry_groups.clear()
            for pid in touched:
                if pid in pf.positions and pf.positions[pid].is_flat():
                    finish(pid, t, exiting.get(pid, "signal"))

        # (2) emergency stops on bar t's range
        if pf.positions:
            threshold = limits.emergency_stop_frac * pf.equity()
            for pid, pos in list(pf.positions.items()):
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
                event(t, "emergency_stop", pid, ",".join(a for a, _ in legs),
                      f"intrabar loss {-worst:.4f} >= {threshold:.4f}")
                finish(pid, t, "emergency_stop")

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
        if pf.holdings:
            pf.mark({a: float(last[mkt.col[a]]) for a in pf.holdings})
        eq = float(pf.equity())
        state = roll_day(state, t, eq)
        actions = portfolio_actions(ctx(t, eq, state))
        new_state = apply_actions(state, actions, t)
        if new_state.kill_switch and not state.kill_switch:
            event(t, "FLATTEN_AND_KILL", detail=f"equity {eq:.4f} vs peak {state.peak_equity}")
        if new_state.entries_blocked_day != state.entries_blocked_day and new_state.entries_blocked_day:
            event(t, "BLOCK_ENTRIES_TODAY", detail=f"equity {eq:.4f} vs day start {state.day_start_equity}")
        if new_state.paused and not state.paused:
            event(t, "PAUSE_RECONCILIATION")
        state = new_state
        if state.kill_switch:
            for pid in pf.positions:
                exiting.setdefault(pid, "kill_switch")

        # (5) exits
        for pid, pos in pf.positions.items():
            pos.bars_held += 1
            if pid in exiting:
                continue
            sig = signals.get((pos.strategy, pos.key))
            if sig is None or sig.rows[i] < 0:
                continue
            closes = {a: float(last[mkt.col[a]]) for a in pos.qty}
            reason = sig.strategy.should_exit(sig.sf, int(sig.rows[i]), pos, closes)
            if reason:
                exiting[pid] = reason
        if exiting:
            exit_checks: list[OrderCheck] = []
            for pid in list(exiting):
                pos = pf.positions.get(pid)
                if pos is None:
                    exiting.pop(pid)
                    continue
                mids = {a: float(last[mkt.col[a]]) for a in pos.qty}
                plan = plan_exit(pos, mids, instruments, kind, t, band, costs=costs)
                for a in plan.dust:
                    pf.mark({a: mids[a]})
                    pf.write_off_dust(pid, a, mids[a], t)
                if not plan.orders:
                    if pos.is_flat():
                        finish(pid, t, exiting[pid])
                    continue
                for o in plan.orders:
                    m = mids[o.asset]
                    exit_checks.append(OrderCheck(o, o.qty * m, 0.0, 0.0, None, True, False, m))
            if exit_checks:
                res = check(exit_checks, ctx(t, pf.equity(), state))
                approved = {o.client_id for o in res.approved}
                for oc in exit_checks:
                    if oc.order.client_id in approved:
                        broker.submit(oc.order)
                        submitted[oc.order.client_id] = (oc.order, oc.mid)
                veto_events(t, res.vetoed)
                state = dataclasses.replace(state, orders_today=state.orders_today + len(res.approved))

        # (6) entries
        firing = fires.get(i)
        entry_lev = math.nan
        if firing:
            state, entry_lev = _entries(
                i, t, firing, mkt, signals, pf, ranker, kind, limits, costs, instruments,
                regime_mult, band, state, ctx, cand_row, veto_events, broker, submitted,
                entry_groups,
            )

        k = i - i0
        eq = pf.equity()
        equity[k] = eq
        stats[k] = (eq, pf.gross_leverage(), len(pf.positions), pf.open_risk(), entry_lev)

    out_idx = full_idx[i0:i1]
    benchmark = None
    if "BTC" in mkt.col:
        benchmark = pd.Series(mkt.last[i0:i1, mkt.col["BTC"]], index=out_idx, name="BTC")
    bar_stats = pd.DataFrame(stats, index=out_idx, columns=BAR_STAT_COLUMNS)
    bar_stats["n_positions"] = bar_stats["n_positions"].astype("int64")
    return BacktestResult(
        equity=pd.Series(equity, index=out_idx, name="equity"),
        trades=_frame(trades, TRADE_COLUMNS),
        candidates=_frame(cand_rows, CANDIDATE_COLUMNS),
        risk_events=_frame(events, EVENT_COLUMNS),
        funding_known_from=dict(fund.known_from) if fund is not None else {},
        bar_stats=bar_stats,
        benchmark=benchmark,
        portfolio=pf,
    )


def _frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)


def _entries(
    i, t, firing, mkt, signals, pf, ranker, kind, limits, costs, instruments, regime_mult, band,
    state, ctx, cand_row, veto_events, broker, submitted, entry_groups,
) -> RiskState:
    """Step 6 for one bar. Returns the updated risk state (orders_today)."""
    mids = {a: float(mkt.close[i, j]) for a, j in mkt.col.items() if mkt.real[i, j]}
    open_keys = {(p.strategy, p.key) for p in pf.positions.values()}
    scorer = ranker if hasattr(ranker, "score") else ranker(t)
    cands: list[Candidate] = []
    for name, key in firing:
        sig = signals[(name, key)]
        c = sig.strategy.candidate_at(sig.sf, int(sig.rows[i]), mids)
        if c is None:
            continue
        if (c.strategy, c.key) in open_keys:
            cand_row(c, "rejected", "already_open")
            continue
        if not all(leg.asset in mids for leg in c.legs):
            cand_row(c, "rejected", "no_data")
            continue
        if kind == "spot" and any(leg.weight * c.direction < 0 for leg in c.legs):
            cand_row(c, "rejected", "short_not_allowed")
            continue
        scored = scorer.score(c, t)
        if scored is None:
            cand_row(c, "rejected", "ranker")
            continue
        cands.append(scored)
    if not cands:
        return state, math.nan

    assets = sorted({leg.asset for c in cands for leg in c.legs} | set(pf.holdings))
    lo = max(0, i - CORR_WINDOW + 1)
    returns = pd.DataFrame(mkt.logret[lo:i + 1, [mkt.col[a] for a in assets]], columns=assets)
    sels, skipped = construct(cands, pf, limits, costs, instruments, mids, returns, regime_mult)
    for c, reason in skipped:
        cand_row(c, "rejected", reason)

    eq = pf.equity()
    acc_pos, acc_risk, acc_gross, acc_cash = 0, 0.0, 0.0, 0.0
    holdings = dict(pf.holdings)
    for sel in sels:
        c = sel.cand
        pr = plan_entry(c, sel.gross_notional, mids, instruments, kind, t, band, costs=costs)
        if pr.reject_reason:
            cand_row(c, "rejected", pr.reject_reason)
            continue
        batch_gross = sum(o.qty * mids[o.asset] for o in pr.orders)
        liq: dict[str, float] = {}
        checks: list[OrderCheck] = []
        post = dict(holdings)
        for n, o in enumerate(pr.orders):
            mid = mids[o.asset]
            risk = sel.risk_amount if n == 0 else 0.0  # the position's risk is counted once
            checks.append(OrderCheck(o, o.qty * mid, risk, risk / eq if eq > 0 else math.inf,
                                     c.stop.distance_frac, True, False, mid))
            post[o.asset] = post.get(o.asset, 0.0) + (o.qty if o.side == "buy" else -o.qty)
        if kind == "perp":
            post_gross = pf.gross_notional() + acc_gross + batch_gross
            for o in pr.orders:
                notional = abs(post[o.asset]) * mids[o.asset]
                mm = instruments[o.asset].maintenance_margin
                liq[o.asset] = math.inf if notional == 0 else max(0.0, (eq - post_gross * mm) / notional)
        res = check(checks, ctx(t, eq, state, extra_pos=acc_pos, extra_risk=acc_risk,
                                extra_gross=acc_gross, holdings=dict(holdings), cash_used=acc_cash,
                                liq=liq))
        if res.vetoed:  # all-or-nothing per selection
            cand_row(c, "rejected", res.vetoed[0][1])
            veto_events(t, res.vetoed)
            continue
        row = cand_row(c, "submitted", None)
        pid = pr.orders[0].position_id
        entry_groups[pid] = _EntryGroup(sel, {o.asset for o in pr.orders}, row)
        for oc in checks:
            broker.submit(oc.order)
            submitted[oc.order.client_id] = (oc.order, oc.mid)
        state = dataclasses.replace(state, orders_today=state.orders_today + len(checks))
        acc_pos += 1
        acc_risk += sel.risk_amount
        acc_gross += batch_gross
        if kind == "spot":
            acc_cash += sum(oc.notional for oc in checks if oc.order.side == "buy")
        holdings = post
    committed = (pf.gross_notional() + acc_gross) / eq if acc_pos and eq > 0 else math.nan
    return state, committed
