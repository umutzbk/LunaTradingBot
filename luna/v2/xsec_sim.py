"""Bar-by-bar simulator of the cross-sectional momentum book (spec §4.2/§4.3, SC-1, plan P5-P8, P10).

Per 15m bar ``t`` (index ``i``) of ``[start, end)``:

0. rebalance fill bar (the bar after a decision): every held leg whose new target has the SAME sign
   is re-referenced: an equity-neutral internal transfer at the bar's open (no order, fee 0, fill =
   reference price) closes the old period position (exit ``rebalance``) and opens the new period's;
1. fills of the orders decided at ``i - 1`` (``SimBroker``; forward-filled bars never fill);
2. perp funding at the hour boundary closing the bar;
3. mark, ``roll_day``, ``portfolio_actions`` / ``apply_actions`` (R3 day loss, R4 drawdown kill: all
   targets to 0, exit ``kill_switch``);
4. catastrophic stop (P7): a leg whose net P&L since its period start (realized + open at the mark,
   reference basis) is <= -1.5% of equity gets an IOC close; the asset is blocked until the next
   rebalance;
5. at a rebalance stamp (P5): eligible assets -> ``raw_targets`` -> ``project_weights`` (daily
   EWMA covariance / 30-day correlation up to ``t``) -> target quantities at the decision mids;
6. orders toward the targets on the decision bar and, while unfilled, on each later bar for at most
   ``RETRY_BARS`` (P6); every order passes ``risk.engine.check`` with ``xsec_limits`` (SC-1);
   closes / reduces first, then opens (a vetoed open retries on the next bar);
7. bar stats.

Trades (P8): one per (period, asset) position, ``risk_amount = 1.5% x equity + round_trip x
|target notional|`` at the decision, ``risk_precap`` the same with the pre-projection notional,
``r_multiple = pnl_net / risk_amount``. ``order_model`` (None = account default) sets the rebalance
order type; catastrophic-stop and kill exits are always IOC. ``flatten_at_end`` closes every leg at
the last bar at the worse of its open and close with taker frictions and fee (exit ``model_switch``, P10).
"""

from __future__ import annotations

import copy
import dataclasses
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.config import Settings
from luna.engine.backtest import (
    BAR_STAT_COLUMNS,
    CANDIDATE_COLUMNS,
    EVENT_COLUMNS,
    TRADE_COLUMNS,
    BacktestResult,
    FundingMode,
    _Funding,
    _Market,
    default_instruments,
)
from luna.execution.costs import CostModel
from luna.execution.sim import SimBroker
from luna.planner import round_qty
from luna.portfolio.portfolio import Portfolio, Position
from luna.risk.engine import OrderCheck, RiskContext, apply_actions, check, portfolio_actions
from luna.risk.state import RiskState, roll_day
from luna.types import (
    AccountKind,
    ExitSpec,
    Fill,
    Instrument,
    Leg,
    OrderModel,
    OrderRequest,
    StopSpec,
    default_order_model,
)
from luna.v2.portfolio_risk import (
    CAT_STOP,
    catastrophic_stop_hit,
    ewma_cov,
    project_weights,
    trailing_corr,
    xsec_limits,
)
from luna.v2.xsec import XSecParams, daily_closes, eligible, raw_targets, rebalance_stamps

BAR = pd.Timedelta("15min")
RETRY_BARS = 96
COV_HISTORY_DAYS = 200
# traded_notional: notional of real fills booked to the leg (both sides; carry-over transfers excluded)
XSEC_TRADE_EXTRA = ["risk_precap", "period", "traded_notional"]
XSEC_BAR_EXTRA = ["vol_scale", "ex_ante_var"]  # net / max share are V1 BAR_STAT_COLUMNS


@dataclass
class _Period:
    """The current period's plan for one asset."""

    pid: str
    period: pd.Timestamp
    target_qty: float  # signed
    risk_amount: float
    risk_precap: float
    stop_dist: float  # CAT_STOP x equity / target notional (fraction of price)
    mid: float


@dataclass
class _State:
    targets: dict[str, _Period] = field(default_factory=dict)
    retry_until: int = -1
    transfer_at: int = -1  # bar index of the pending re-reference (decision bar + 1)
    blocked: set[str] = field(default_factory=set)
    exit_reason: dict[str, str] = field(default_factory=dict)  # pid -> reason when it goes flat
    entry_notional: dict[str, float] = field(default_factory=dict)
    traded: dict[str, float] = field(default_factory=dict)  # pid -> real traded notional (no transfers)
    meta: dict[str, _Period] = field(default_factory=dict)  # pid -> period plan (risk, period ts)
    vol_scale: float = math.nan
    ex_ante_var: float = math.nan


def run_xsec(
    panel: dict[str, pd.DataFrame],
    funding: dict[str, pd.DataFrame],
    params: XSecParams,
    universe: list[str],
    kind: AccountKind,
    settings: Settings,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    name: str = "xsec",
    cost_mult: float = 1.0,
    funding_mode: FundingMode = "known_only",
    instruments: dict[str, Instrument] | None = None,
    initial_portfolio: Portfolio | None = None,
    initial_state: RiskState | None = None,
    flatten_at_end: bool = False,
    order_model: OrderModel | None = None,
) -> BacktestResult:
    """Simulate the cross-sectional book over [start, end) (module docstring)."""
    v1 = settings.accounts[kind]
    limits = xsec_limits(v1)
    costs = CostModel(settings.costs[kind])
    instruments = instruments or default_instruments(sorted(panel), kind, settings)
    pf = (
        copy.deepcopy(initial_portfolio) if initial_portfolio is not None else Portfolio(kind, v1.capital_usd)
    )
    if pf.kind != kind:
        raise ValueError(f"initial portfolio is {pf.kind}, run is {kind}")
    rs = dataclasses.replace(initial_state) if initial_state is not None else RiskState()

    idx = pd.DatetimeIndex([])
    for df in panel.values():
        idx = idx.union(df.index)
    idx = pd.DatetimeIndex(idx).sort_values()
    mkt = _Market(panel, idx)
    i0 = int(idx.searchsorted(start, side="left"))
    i1 = int(idx.searchsorted(end, side="left"))
    fund = _Funding(funding, mkt, funding_mode) if kind == "perp" else None
    broker = SimBroker(kind, costs, instruments, cost_mult)
    dc = daily_closes(panel)
    aux = None
    if params.signal != "momentum":  # Research V3: daily perp-spot premium read at decision stamps
        from luna.v3.signals import base_signal, premium_daily

        if base_signal(params.signal) == "premium":
            aux = premium_daily(panel)
    stamps = set(rebalance_stamps(idx[i0:i1], params.rebalance))
    liquidity = order_model or default_order_model(kind)
    band = v1.max_price_band

    st = _State()
    pending: dict[str, tuple[OrderRequest, float]] = {}
    trades: list[dict] = []
    events: list[dict] = []
    # positions carried in: keep their pid as the current period of their asset
    for pid, pos in pf.positions.items():
        m = pos.meta
        st.meta[pid] = _Period(
            pid,
            m.get("period", pos.entry_ts),
            pos.qty.get(pos.key, 0.0),
            pos.risk_amount,
            float(m.get("risk_precap", pos.risk_amount)),
            pos.stop.distance_frac,
            pos.entry_ref,
        )
        st.entry_notional[pid] = float(
            m.get(
                "entry_notional",
                sum(abs(q) * p for q, p in zip(pos.qty.values(), pos.entry_price.values(), strict=True)),
            )
        )
        st.traded[pid] = float(m.get("traded_notional", 0.0))

    def event(t, kind_, asset="", detail="", pid=""):
        events.append({"ts": t, "kind": kind_, "position_id": pid, "asset": asset, "detail": detail})

    def pid_of(asset: str) -> str | None:
        found = [pid for pid, pos in pf.positions.items() if pos.key == asset]
        if len(found) > 1:  # one period leg per asset at any time (transfers close the old one first)
            raise RuntimeError(f"several open positions for {asset}: {found}")
        return found[0] if found else None

    def finish(pid: str, t: pd.Timestamp, reason: str) -> None:
        pos = pf.close_position(pid, t, reason)
        p = pos.pnl
        net = pos.net_pnl()
        meta = st.meta.pop(pid)
        trades.append(
            {
                "position_id": pid,
                "strategy": name,
                "key": pos.key,
                "direction": pos.direction,
                "entry_ts": pos.entry_ts,
                "exit_ts": t,
                "exit_reason": reason,
                "gross_notional": st.entry_notional.pop(pid, math.nan),
                "risk_amount": pos.risk_amount,
                "pnl_gross": p["gross"],
                "fees": p["fees"],
                "spread_slip": p["spread_slip"],
                "funding": p["funding"],
                "funding_unknown_bars": int(p["funding_unknown_bars"]),
                "pnl_net": net,
                "r_multiple": net / pos.risk_amount if pos.risk_amount > 0 else math.nan,
                "regime": pos.regime,
                "risk_precap": meta.risk_precap,
                "period": meta.period,
                "traded_notional": st.traded.pop(pid, 0.0),
            }
        )
        st.exit_reason.pop(pid, None)

    def open_position(plan: _Period, asset: str, t: pd.Timestamp) -> None:
        d = 1 if plan.target_qty > 0 else -1
        pf.open_position(
            Position(
                id=plan.pid,
                strategy=name,
                key=asset,
                legs=(Leg(asset, 1.0),),
                direction=d,
                qty={},
                entry_price={},
                entry_ts=t,
                entry_ref=plan.mid,
                stop=StopSpec("price", plan.mid * (1.0 - d * plan.stop_dist), plan.stop_dist),
                exit=ExitSpec(max_hold_bars=10**9),
                risk_amount=plan.risk_amount,
                regime="NONE",
                meta={"risk_precap": plan.risk_precap, "period": plan.period},
            )
        )
        st.meta[plan.pid] = plan

    def book(f: Fill, pid: str, ref: float, transfer: bool = False) -> None:
        pos = pf.positions[pid]
        before = abs(pos.qty.get(f.asset, 0.0))
        pf.apply_fill(f, pid, ref)
        if abs(pos.qty.get(f.asset, 0.0)) > before:
            st.entry_notional[pid] = st.entry_notional.get(pid, 0.0) + f.qty * f.price
        if not transfer:
            st.traded[pid] = st.traded.get(pid, 0.0) + f.qty * f.price
        if pid in pf.positions:  # survives a window boundary with the position
            pos.meta["entry_notional"] = st.entry_notional.get(pid, 0.0)
            pos.meta["traded_notional"] = st.traded.get(pid, 0.0)

    def order(asset, side, qty, mid, t, intent, pid, tag, tif) -> OrderRequest | None:
        inst = instruments[asset]
        q = round_qty(abs(qty), inst.qty_step)
        if q <= 0 or q < inst.min_qty or q * mid < inst.min_notional:
            return None
        s = 1.0 if side == "buy" else -1.0
        limit = mid * (1.0 + s * band) if tif == "IOC" else mid * (1.0 - s * costs.half_spread(asset))
        return OrderRequest(
            f"{pid}:{tag}:{asset}:{intent}:{t:%Y%m%d%H%M}", t, asset, side, q, limit, tif, intent, pid, tag
        )

    def ctx(t, eq, liq) -> RiskContext:
        return RiskContext(
            limits=limits,
            kind=kind,
            mode="backtest",
            state=rs,
            equity=float(eq),
            open_positions=len(pf.positions),
            open_risk=pf.open_risk(),
            gross_notional=pf.gross_notional(),
            holdings=dict(pf.holdings),
            last_bar_ts=t,
            now=t,
            liq_distance=liq,
            min_qty={a: x.min_qty for a, x in instruments.items()},
            min_notional={a: x.min_notional for a, x in instruments.items()},
            available_cash=pf.cash,
            open_position_ids=frozenset(pf.positions),
        )

    def submit(checks: list[OrderCheck], t, eq, liq) -> None:
        nonlocal rs
        if not checks:
            return
        res = check(checks, ctx(t, eq, liq))
        ok = {o.client_id for o in res.approved}
        rs = dataclasses.replace(rs, orders_today=rs.orders_today + len(ok))
        for oc in checks:
            if oc.order.client_id in ok:
                pending[oc.order.client_id] = (oc.order, oc.mid)
                broker.submit(oc.order)
        for o, reason in res.vetoed:
            event(t, "veto", o.asset, reason, o.position_id)

    def plan_orders(
        i: int, t: pd.Timestamp, eq: float, stop_assets: set[str], only: set[str] | None = None
    ) -> None:
        """Orders toward the current targets (and IOC closes of stopped / killed legs); ``only``
        restricts them to those assets (stops outside the rebalance retry window)."""
        working = {o.asset for o, _ in pending.values()}
        closes: list[OrderCheck] = []
        opens: list[OrderCheck] = []
        post_gross = pf.gross_notional()
        assets = sorted(set(st.targets) | set(pf.holdings))
        for a in assets:
            if a in working or (only is not None and a not in only):
                continue
            j = mkt.col[a]
            if not mkt.real[i, j]:
                continue
            mid = float(mkt.last[i, j])
            cur = pf.holdings.get(a, 0.0)
            plan = st.targets.get(a)
            stopping = a in stop_assets or rs.kill_switch
            tgt = 0.0 if (stopping or plan is None) else plan.target_qty
            tif = "IOC" if (stopping or liquidity == "taker") else "POST_ONLY"
            if cur != 0.0 and (tgt == 0.0 or np.sign(tgt) != np.sign(cur)):
                pid = pid_of(a)
                o = order(
                    a,
                    "sell" if cur > 0 else "buy",
                    cur,
                    mid,
                    t,
                    "close",
                    pid,
                    "stop" if a in stop_assets else "rebalance",
                    tif,
                )
                if o is not None:
                    closes.append(OrderCheck(o, o.qty * mid, 0.0, 0.0, None, True, False, mid))
                elif pid is not None:  # untradeable residue (qty step / minimums): write it off
                    pf.mark({a: mid})
                    pf.write_off_dust(pid, a, mid, t)
                    if pf.positions[pid].is_flat():
                        finish(
                            pid, t, st.exit_reason.get(pid, "kill_switch" if rs.kill_switch else "rebalance")
                        )
                cur = 0.0 if tgt != 0.0 else cur
                if tgt == 0.0:
                    continue
            delta = tgt - cur
            if plan is None or abs(delta) * mid < 1e-12:
                continue
            side = "buy" if delta > 0 else "sell"
            if abs(tgt) < abs(cur):
                o = order(a, side, delta, mid, t, "reduce", plan.pid, "rebalance", tif)
                if o is not None:
                    closes.append(OrderCheck(o, o.qty * mid, 0.0, 0.0, None, True, False, mid))
                continue
            o = order(a, side, delta, mid, t, "open", plan.pid, "rebalance", tif)
            if o is None:
                continue
            risk = CAT_STOP * eq * min(1.0, o.qty / abs(tgt))
            post_gross += o.qty * mid
            opens.append(
                OrderCheck(
                    o, o.qty * mid, risk, risk / eq if eq > 0 else math.inf, plan.stop_dist, True, False, mid
                )
            )
        liq: dict[str, float] = {}
        if kind == "perp":
            for oc in opens:
                a = oc.order.asset
                notional = abs(st.targets[a].target_qty) * oc.mid
                mm = instruments[a].maintenance_margin
                liq[a] = math.inf if notional == 0 else max(0.0, (eq - post_gross * mm) / notional)
        submit(closes, t, eq, liq)
        submit(opens, t, eq, liq)

    def decide(i: int, t: pd.Timestamp, eq: float) -> None:
        if i <= st.retry_until:  # the previous decision's retry window is cut short (daily rebalance)
            record_misses(i, t)
        real_at = {a: bool(mkt.real[i, mkt.col[a]]) for a in panel}
        hist = dc.loc[dc.index <= t]
        elig = eligible(
            dc, real_at, [a for a in universe if a in panel], params.lookback_days, t, params=params, aux=aux
        )
        raw = raw_targets(dc, elig, params, kind, t, aux)
        w = raw
        st.vol_scale, st.ex_ante_var = 1.0, math.nan
        if len(elig) and (raw != 0).any():
            rets = np.log(hist[elig].iloc[-COV_HISTORY_DAYS:]).diff().iloc[1:]
            proj = project_weights(
                raw,
                ewma_cov(rets),
                trailing_corr(rets),
                controls="perp" if kind == "perp" else "spot",
                gross=params.gross if kind == "perp" else 1.0,
                vol_target=params.vol_target,  # V2 spot grids never set one (spot: V3 vol-managed only)
            )
            if proj.reason:
                event(t, proj.reason, detail=",".join(elig))
            w, st.vol_scale, st.ex_ante_var = proj.weights, proj.vol_scale, proj.var_95
        st.targets = {}
        for a in elig:
            if w[a] == 0.0 or not eq > 0:
                continue
            mid = float(mkt.last[i, mkt.col[a]])
            notional = abs(float(w[a])) * eq
            rt = costs.round_trip_frac(a, liquidity)
            st.targets[a] = _Period(
                pid=f"{name}:{a}:{t:%Y%m%d%H%M}",
                period=t,
                target_qty=float(w[a]) * eq / mid,
                risk_amount=CAT_STOP * eq + rt * notional,
                risk_precap=CAT_STOP * eq + rt * abs(float(raw[a])) * eq,
                stop_dist=CAT_STOP * eq / notional,
                mid=mid,
            )
        st.retry_until = i + RETRY_BARS
        st.transfer_at = i + 1

    def transfer(i: int, t: pd.Timestamp) -> None:
        """Re-reference same-sign carried legs into the new period's positions (P8)."""
        for a, plan in st.targets.items():
            old = pid_of(a)
            if old is None or old == plan.pid:
                continue
            q = pf.positions[old].qty.get(a, 0.0)
            if q == 0.0 or np.sign(q) != np.sign(plan.target_qty):
                continue
            j = mkt.col[a]
            px = float(mkt.open[i, j]) if np.isfinite(mkt.open[i, j]) else float(mkt.last[i - 1, j])
            out_side, in_side = ("sell", "buy") if q > 0 else ("buy", "sell")
            out_fill = Fill(f"{old}:transfer:{a}", t, a, out_side, abs(q), px, 0.0, "maker")
            book(out_fill, old, px, transfer=True)
            finish(old, t, "rebalance")
            open_position(plan, a, t)
            book(
                Fill(f"{plan.pid}:transfer:{a}", t, a, in_side, abs(q), px, 0.0, "maker"),
                plan.pid,
                px,
                transfer=True,
            )

    def bar_row() -> tuple[float, list[float]]:
        eq = float(pf.equity())
        net = sum(q * pf.marks.get(a, pf.avg_entry[a]) for a, q in pf.holdings.items())
        biggest = max((pf.notional(a) for a in pf.holdings), default=0.0)
        row = {
            "equity": eq,
            "gross_leverage": pf.gross_leverage(),
            "n_positions": len(pf.positions),
            "open_risk": pf.open_risk(),
            "entry_gross_leverage": math.nan,
            "net_leverage": net / eq if eq > 0 else math.nan,
            "max_asset_share": biggest / eq if eq > 0 else math.nan,
            "vol_scale": st.vol_scale,
            "ex_ante_var": st.ex_ante_var,
        }
        return eq, [row[c] for c in cols]

    def record_misses(i: int, t: pd.Timestamp) -> None:
        """Targets still not reached when their retry window ends or a new decision replaces them."""
        for a, plan in st.targets.items():
            mid = float(mkt.last[i, mkt.col[a]])
            gap = plan.target_qty - pf.holdings.get(a, 0.0)
            if order(a, "buy" if gap > 0 else "sell", gap, mid, t, "open", plan.pid, "probe", "IOC"):
                event(t, "missed_rebalance", a, f"notional {abs(gap) * mid:.4f}", plan.pid)

    n_out = max(i1 - i0, 0)
    cols = BAR_STAT_COLUMNS + XSEC_BAR_EXTRA
    stats = np.full((n_out, len(cols)), np.nan)
    equity = np.empty(n_out)
    initial_equity = float(pf.equity())

    # a rebalance stamp on the bar just before ``start`` (the previous window's last bar) is decided
    # here, from data up to that bar, so its orders fill on this window's first bar instead of being
    # dropped at the window boundary
    if 0 < i0 < i1 and len(rebalance_stamps(idx[i0 - 1 : i0], params.rebalance)) and not rs.kill_switch:
        tp = idx[i0 - 1]
        if pf.holdings:
            pf.mark({a: float(mkt.last[i0 - 1, mkt.col[a]]) for a in pf.holdings})
        eq0 = float(pf.equity())
        decide(i0 - 1, tp, eq0)
        plan_orders(i0 - 1, tp, eq0, set())

    for i in range(i0, i1):
        t = idx[i]
        last = mkt.last[i]
        # (0) re-reference carried legs at the rebalance fill bar
        if i == st.transfer_at:
            transfer(i, t)
        # (1) fills
        if pending:
            bars = {
                a: {
                    "open": float(mkt.open[i, j]),
                    "high": float(mkt.high[i, j]),
                    "low": float(mkt.low[i, j]),
                    "close": float(mkt.close[i, j]),
                    "filled": not bool(mkt.real[i, j]),
                }
                for a, j in mkt.col.items()
                if mkt.present[i, j]
            }
            for f in broker.on_bar(t, bars):
                o, ref = pending[f.client_id]
                pid = o.position_id
                if pid not in pf.positions:
                    plan = st.targets.get(f.asset)
                    if plan is None or plan.pid != pid:  # orders live one bar: cannot happen
                        raise RuntimeError(f"fill {f.client_id} for unknown period position {pid}")
                    open_position(plan, f.asset, t)
                book(f, pid, ref)
                if o.leg_group == "stop":
                    st.exit_reason[pid] = "catastrophic_stop"
                elif rs.kill_switch:
                    st.exit_reason[pid] = "kill_switch"
            pending.clear()
            for pid in [p for p, pos in pf.positions.items() if pos.is_flat()]:
                finish(pid, t, st.exit_reason.get(pid, "rebalance"))
        # (2) funding
        if fund is not None and fund.boundary[i] and pf.holdings:
            for a in list(pf.holdings):
                j = mkt.col[a]
                rate, known = fund.rate_for(i, j, a)
                pf.apply_funding(a, rate, float(last[j]), t + BAR)
                if not known and rate is not None:
                    for p in pf.positions.values():
                        if p.qty.get(a, 0.0) != 0.0:
                            p.pnl["funding_unknown_bars"] += 1
        # (3) mark, day roll, R3 / R4
        if pf.holdings:
            pf.mark({a: float(last[mkt.col[a]]) for a in pf.holdings})
        eq = float(pf.equity())
        rs = roll_day(rs, t, eq)
        was_killed = rs.kill_switch
        rs = apply_actions(rs, portfolio_actions(ctx(t, eq, {})), t)
        if rs.kill_switch and not was_killed:
            event(t, "FLATTEN_AND_KILL", detail=f"equity {eq:.4f} vs peak {rs.peak_equity}")
        if rs.kill_switch:  # also a kill carried in from an earlier window: flatten until flat
            st.targets = {}
            st.retry_until = 10**12
        # (4) catastrophic stops
        stop_assets: set[str] = set()
        for pid, pos in pf.positions.items() if not rs.kill_switch else []:  # killed: all legs close
            a = pos.key
            q = pos.qty.get(a, 0.0)
            if q == 0.0 or a in st.blocked:
                continue
            leg = pos.net_pnl() + q * (float(last[mkt.col[a]]) - pos.entry_ref_price[a])
            if catastrophic_stop_hit(leg, eq):
                stop_assets.add(a)
                st.blocked.add(a)
                st.targets.pop(a, None)
                st.exit_reason[pid] = "catastrophic_stop"
                event(t, "catastrophic_stop", a, f"leg {leg:.4f} <= -{CAT_STOP} x {eq:.4f}", pid)
        stop_assets |= {
            pos.key
            for pid, pos in pf.positions.items()
            if st.exit_reason.get(pid) == "catastrophic_stop" and pos.qty.get(pos.key)
        }
        # (5) rebalance decision
        if t in stamps and not rs.kill_switch:
            st.blocked.clear()
            for pid in list(st.exit_reason):
                if st.exit_reason[pid] == "catastrophic_stop" and pid in pf.positions:
                    stop_assets.add(pf.positions[pid].key)
            decide(i, t, eq)
            st.blocked |= stop_assets
            for a in stop_assets:
                st.targets.pop(a, None)
        # (6) orders: decision bar, retries, stops, kill flatten
        if i <= st.retry_until:
            plan_orders(i, t, eq, stop_assets)
        elif stop_assets:
            plan_orders(i, t, eq, stop_assets, only=stop_assets)
        if i == st.retry_until + 1:
            record_misses(i, t)
        # (7) stats
        k = i - i0
        equity[k], stats[k] = bar_row()

    # flatten at the end (engine switch, P10)
    if flatten_at_end and pf.positions and i1 > i0:
        i = i1 - 1
        t = idx[i]
        for pid, pos in list(pf.positions.items()):
            a = pos.key
            q = pos.qty.get(a, 0.0)
            if q != 0.0:
                j = mkt.col[a]
                side = "sell" if q > 0 else "buy"
                if mkt.real[i, j]:
                    f = broker.stop_fill(a, side, abs(q), float(mkt.close[i, j]), mkt.bar(i, a), pid)
                else:
                    fr = (costs.half_spread(a) + costs.slippage) * cost_mult
                    px = float(mkt.last[i, j]) * (1.0 - fr if side == "sell" else 1.0 + fr)
                    f = Fill(
                        f"{pid}:switch:{a}",
                        t,
                        a,
                        side,
                        abs(q),
                        px,
                        costs.fee(abs(q) * px, "taker") * cost_mult,
                        "taker",
                    )
                book(f, pid, float(mkt.last[i, j]))
            finish(pid, t, "model_switch")
        equity[-1], stats[-1] = bar_row()  # the flattened book, not the pre-flatten marks

    out_idx = idx[i0:i1]
    bar_stats = pd.DataFrame(stats, index=out_idx, columns=cols)
    bar_stats["n_positions"] = bar_stats["n_positions"].astype("int64")
    benchmark = None
    if "BTC" in mkt.col:
        benchmark = pd.Series(mkt.last[i0:i1, mkt.col["BTC"]], index=out_idx, name="BTC")
    tcols = TRADE_COLUMNS + XSEC_TRADE_EXTRA
    return BacktestResult(
        equity=pd.Series(equity, index=out_idx, name="equity"),
        trades=pd.DataFrame(trades, columns=tcols) if trades else pd.DataFrame(columns=tcols),
        candidates=pd.DataFrame(columns=CANDIDATE_COLUMNS),
        risk_events=pd.DataFrame(events, columns=EVENT_COLUMNS),
        funding_known_from=dict(fund.known_from) if fund is not None else {},
        bar_stats=bar_stats,
        benchmark=benchmark,
        portfolio=pf,
        risk_state=rs,
        pending_exits=None,
        initial_equity=initial_equity,
    )
