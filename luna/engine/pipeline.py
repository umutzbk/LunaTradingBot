"""Decision steps shared by the backtest and the live engine (spec §3.3 steps 4-8, §6, §7).

``Pipeline`` owns the per-run decision state (portfolio, risk state, positions being exited, entry
groups awaiting fills, orders in flight) and implements the steps of one decision bar:

- ``apply_fill`` / ``settle``: book fills of orders in flight; resolve entry groups (filled /
  partial = ``leg_failure`` -> flatten / unfilled) once all their orders are terminal; close
  positions that went flat.
- ``portfolio_step``: mark, ``roll_day``, ``portfolio_actions`` -> ``apply_actions``;
  FLATTEN_AND_KILL marks every position for exit.
- ``exit_signals`` + ``flatten``: ``strategy.should_exit`` at the close, then exit orders for every
  position being exited (dust written off at the mark). Exits pass ``risk.check`` (closes are
  exempt from most rules, never from R10/R13/R15/R17/R19).
- ``entries``: candidates -> ranker -> ``construct`` -> ``plan_entry`` -> ``risk.check`` ->
  submit, all legs of a selection as one all-or-nothing batch.

The caller supplies the market view (``mkt``: dense arrays over a bar index, see
``luna.engine.backtest._Market``; ``signals``: per (strategy, key) signal frame and row map), the
decision bar ``t`` (open time of the last closed bar) and the clock ``now`` (backtest: ``now = t``;
live: the real tick time, which stamps the orders and drives the day roll and R16).

Orders in flight (``submitted``) are registered before ``broker.submit`` is called, so a broker
that persists state on submit (live) sees a consistent picture. In the backtest every order in
flight is consumed by the next bar; live orders may stay working across polls: ``settle`` keeps
the ones still open, and ``flatten`` / ``entries`` never duplicate an exit or an entry that is
still working.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd

from luna.config import RiskLimits
from luna.execution.costs import CostModel
from luna.planner import plan_entry, plan_exit
from luna.portfolio.constructor import Selection, construct
from luna.portfolio.portfolio import Portfolio, Position
from luna.risk.engine import Action, OrderCheck, RiskContext, apply_actions, check, portfolio_actions
from luna.risk.state import RiskState, roll_day
from luna.types import AccountKind, Candidate, Fill, Instrument, OrderModel, OrderRequest

CORR_WINDOW = 30 * 96  # 30 days of 15-minute bars


@dataclass
class EntryGroup:
    """The orders of one selected candidate (all legs), until every one of them is terminal."""

    sel: Selection
    assets: set[str]
    cand_row: int  # row in Pipeline.cand_rows (-1: not tracked, e.g. reloaded after a restart)
    filled: set[str] = field(default_factory=set)
    cids: set[str] = field(default_factory=set)


class Pipeline:
    def __init__(
        self,
        pf: Portfolio,
        state: RiskState,
        kind: AccountKind,
        limits: RiskLimits,
        costs: CostModel,
        instruments: dict[str, Instrument],
        broker: Any,
        mode: Literal["backtest", "paper", "live"],
        *,
        keep_candidates: bool = False,
        track_attempts: bool = False,
        order_model: OrderModel | None = None,
    ) -> None:
        self.pf = pf
        self.state = state
        self.kind: AccountKind = kind
        self.limits = limits
        self.costs = costs
        self.instruments = instruments
        self.broker = broker
        self.mode = mode
        self.band = limits.max_price_band
        self.order_model = order_model  # None = account default (perp IOC, spot post-only)
        self.min_qty = {a: inst.min_qty for a, inst in instruments.items()}
        self.min_notional = {a: inst.min_notional for a, inst in instruments.items()}
        self.submitted: dict[str, tuple[OrderRequest, float]] = {}  # client_id -> (order, ref price)
        self.entry_groups: dict[str, EntryGroup] = {}
        self.exiting: dict[str, str] = {}  # position_id -> exit reason
        self.entry_notional: dict[str, float] = {
            pid: sum(abs(q) * p.entry_price.get(a, 0.0) for a, q in p.qty.items())
            for pid, p in pf.positions.items()
        }
        self.trades: list[dict] = []
        self.cand_rows: list[dict] = []
        self.cand_objs: list[Candidate] | None = [] if keep_candidates else None
        # params of the strategy instance that produced each candidate (aligned with cand_rows)
        self.cand_params: list[dict | None] = []
        self._params_of: dict[tuple, dict] = {}
        self.events: list[dict] = []
        self._touched: set[str] = set()
        # live: monotone exit re-plan counter per position (client ids unique within a minute);
        # None in the backtest (attempt 0 = the historical ids)
        self.exit_attempts: dict[str, int] | None = {} if track_attempts else None
        # live: called as (position_id, status, group) when an entry group resolves
        self.on_entry_resolved: Callable[[str, str, EntryGroup], None] | None = None

    # --- records ----------------------------------------------------------------------------------

    def event(self, ts, kind_, pid="", asset="", detail="") -> None:
        self.events.append({"ts": ts, "kind": kind_, "position_id": pid, "asset": asset, "detail": detail})

    def cand_row(self, c: Candidate, status: str, reason: str | None) -> int:
        self.cand_rows.append({
            "ts": c.ts, "strategy": c.strategy, "key": c.key, "direction": c.direction,
            "score": c.score, "edge_bps": c.edge_bps, "stop_distance": c.stop.distance_frac,
            "regime": c.regime, "status": status, "reason": reason,
        })
        self.cand_params.append(self._params_of.get((c.strategy, c.key, c.ts)))
        if self.cand_objs is not None:
            self.cand_objs.append(c)
        return len(self.cand_rows) - 1

    def finish(self, pid: str, ts: pd.Timestamp, reason: str) -> None:
        pos = self.pf.close_position(pid, ts, reason)
        self.exiting.pop(pid, None)
        if self.exit_attempts is not None:
            self.exit_attempts.pop(pid, None)
        p = pos.pnl
        net = pos.net_pnl()
        self.trades.append({
            "position_id": pid, "strategy": pos.strategy, "key": pos.key,
            "direction": pos.direction, "entry_ts": pos.entry_ts, "exit_ts": ts,
            "exit_reason": reason, "gross_notional": self.entry_notional.pop(pid, math.nan),
            "risk_amount": pos.risk_amount, "pnl_gross": p["gross"], "fees": p["fees"],
            "spread_slip": p["spread_slip"], "funding": p["funding"],
            "funding_unknown_bars": int(p["funding_unknown_bars"]), "pnl_net": net,
            "r_multiple": net / pos.risk_amount if pos.risk_amount > 0 else math.nan,
            "regime": pos.regime,
        })

    def ctx(self, t, eq, st, *, now=None, extra_pos=0, extra_risk=0.0, extra_gross=0.0,
            holdings=None, cash_used=0.0, liq=None) -> RiskContext:
        pf = self.pf
        return RiskContext(
            limits=self.limits, kind=self.kind, mode=self.mode, state=st, equity=float(eq),
            open_positions=len(pf.positions) + extra_pos,
            open_risk=pf.open_risk() + extra_risk,
            gross_notional=pf.gross_notional() + extra_gross,
            holdings=dict(pf.holdings) if holdings is None else holdings,
            last_bar_ts=t, now=t if now is None else now, liq_distance=liq or {},
            min_qty=self.min_qty, min_notional=self.min_notional,
            available_cash=pf.cash - cash_used, open_position_ids=frozenset(pf.positions),
        )

    def veto_events(self, t, vetoed) -> None:
        for o, reason in vetoed:
            self.event(t, "veto", o.position_id, o.asset, reason)

    def regime_mult(self, c: Candidate) -> float:
        return self.limits.highvol_risk_mult if c.regime == "HIGH_VOL" else 1.0

    # --- (1) fills --------------------------------------------------------------------------------

    def apply_fill(self, f: Fill) -> None:
        """Book one fill of an order in flight (the first entry fill registers the position)."""
        pf = self.pf
        order, ref = self.submitted[f.client_id]
        pid = order.position_id
        if order.intent == "open":
            g = self.entry_groups[pid]
            if pid not in pf.positions:
                c = g.sel.cand
                pf.open_position(Position(
                    id=pid, strategy=c.strategy, key=c.key, legs=c.legs,
                    direction=c.direction, qty={}, entry_price={}, entry_ts=f.ts,
                    entry_ref=c.entry_ref, stop=c.stop, exit=c.exit,
                    risk_amount=g.sel.risk_amount, regime=c.regime,
                ))
            pf.apply_fill(f, pid, ref)
            self.entry_notional[pid] = self.entry_notional.get(pid, 0.0) + f.qty * f.price
            g.filled.add(f.asset)
        elif pid in pf.positions:
            pf.apply_fill(f, pid, ref)
            self._touched.add(pid)

    def settle(self, t: pd.Timestamp, open_cids: frozenset[str] | set[str] = frozenset()) -> None:
        """Forget terminal orders (all but ``open_cids``), resolve entry groups none of whose orders
        is still open, and close positions that went flat."""
        for cid in list(self.submitted):
            if cid not in open_cids:
                del self.submitted[cid]
        for pid, g in list(self.entry_groups.items()):
            if g.cids & set(open_cids):
                continue
            status = None
            if not g.filled:
                status = "unfilled"
            elif g.filled != g.assets:
                status = "partial"
                self.event(t, "leg_failure", pid, ",".join(sorted(g.assets - g.filled)),
                           "entry legs unfilled; flattening filled legs")
                self.exiting[pid] = "leg_failure"
            else:
                status = "filled"
            if g.cand_row >= 0:
                self.cand_rows[g.cand_row]["status"] = status
            del self.entry_groups[pid]
            if self.on_entry_resolved is not None:
                self.on_entry_resolved(pid, status, g)
        for pid in self._touched:
            if pid in self.pf.positions and self.pf.positions[pid].is_flat():
                self.finish(pid, t, self.exiting.get(pid, "signal"))
        self._touched.clear()

    # --- (4) portfolio-level actions --------------------------------------------------------------

    def portfolio_step(self, t: pd.Timestamp, marks: dict[str, float], now=None) -> list[Action]:
        """Mark the holdings at ``marks``, roll the UTC day at ``now`` and apply the portfolio
        actions (R3 / R4 / R18)."""
        pf = self.pf
        now = t if now is None else now
        if pf.holdings:
            pf.mark({a: marks[a] for a in pf.holdings})
        eq = float(pf.equity())
        state = roll_day(self.state, now, eq)
        actions = portfolio_actions(self.ctx(t, eq, state, now=now))
        new_state = apply_actions(state, actions, now)
        if new_state.kill_switch and not state.kill_switch:
            self.event(t, "FLATTEN_AND_KILL", detail=f"equity {eq:.4f} vs peak {state.peak_equity}")
        if new_state.entries_blocked_day != state.entries_blocked_day and new_state.entries_blocked_day:
            self.event(t, "BLOCK_ENTRIES_TODAY",
                       detail=f"equity {eq:.4f} vs day start {state.day_start_equity}")
        if new_state.paused and not state.paused:
            self.event(t, "PAUSE_RECONCILIATION")
        self.state = new_state
        if self.state.kill_switch:
            for pid in pf.positions:
                self.exiting.setdefault(pid, "kill_switch")
        return actions

    # --- (5) exits --------------------------------------------------------------------------------

    def exit_signals(self, i: int, mkt, signals, bars: int = 1) -> None:
        """Advance ``bars_held`` and ask each position's strategy whether to exit at row ``i``."""
        for pid, pos in self.pf.positions.items():
            pos.bars_held += bars
            if pid in self.exiting:
                continue
            sig = signals.get((pos.strategy, pos.key))
            if sig is None or sig.rows[i] < 0:
                continue
            closes = {a: float(mkt.last[i, mkt.col[a]]) for a in pos.qty}
            reason = sig.strategy.should_exit(sig.sf, int(sig.rows[i]), pos, closes)
            if reason:
                self.exiting[pid] = reason

    def flatten(
        self, t: pd.Timestamp, mid: Callable[[str], float], now=None,
        adjust: Callable[[OrderRequest, float], OrderRequest] | None = None,
        only: set[str] | None = None,
    ) -> None:
        """Exit orders for every position being exited (restricted to ``only`` if given) whose
        previous exit order is no longer working. ``mid(asset)`` is the reference price;
        ``adjust`` may rewrite an order (e.g. an emergency exit as IOC) before the risk check."""
        if not self.exiting:
            return
        pf = self.pf
        now = t if now is None else now
        working = {o.position_id for o, _ in self.submitted.values() if o.intent != "open"}
        exit_checks: list[OrderCheck] = []
        for pid in list(self.exiting):
            pos = pf.positions.get(pid)
            if pos is None:
                self.exiting.pop(pid)
                continue
            if pid in working or (only is not None and pid not in only):
                continue
            mids = {a: mid(a) for a in pos.qty}
            attempt = 0
            if self.exit_attempts is not None:
                attempt = self.exit_attempts.get(pid, 0)
                self.exit_attempts[pid] = attempt + 1
            plan = plan_exit(pos, mids, self.instruments, self.kind, now, self.band, costs=self.costs,
                             attempt=attempt, order_model=self.order_model)
            for a in plan.dust:
                pf.mark({a: mids[a]})
                pf.write_off_dust(pid, a, mids[a], t)
            if not plan.orders:
                if pos.is_flat():
                    self.finish(pid, t, self.exiting[pid])
                continue
            for o in plan.orders:
                m = mids[o.asset]
                if adjust is not None:
                    o = adjust(o, m)
                exit_checks.append(OrderCheck(o, o.qty * m, 0.0, 0.0, None, True, False, m))
        if exit_checks:
            res = check(exit_checks, self.ctx(t, pf.equity(), self.state, now=now))
            approved = {o.client_id for o in res.approved}
            self.state = dataclasses.replace(
                self.state, orders_today=self.state.orders_today + len(res.approved))
            chosen = [oc for oc in exit_checks if oc.order.client_id in approved]
            for oc in chosen:
                self.submitted[oc.order.client_id] = (oc.order, oc.mid)
            for oc in chosen:
                self.broker.submit(oc.order)
            self.veto_events(t, res.vetoed)

    # --- (6) entries ------------------------------------------------------------------------------

    def entries(self, i: int, t: pd.Timestamp, firing, mkt, signals, ranker, now=None) -> float:
        """Step 6 for one bar. Returns the committed gross leverage after the submitted entries
        (NaN if none were submitted)."""
        pf = self.pf
        kind = self.kind
        now = t if now is None else now
        mids = {a: float(mkt.close[i, j]) for a, j in mkt.col.items() if mkt.real[i, j]}
        # entries still working (live only; the backtest settles every order on the next bar)
        pend = list(self.entry_groups.values())
        open_keys = {(p.strategy, p.key) for p in pf.positions.values()}
        open_keys |= {(g.sel.cand.strategy, g.sel.cand.key) for g in pend}
        scorer = ranker if hasattr(ranker, "score") else ranker(t)
        cands: list[Candidate] = []
        for name, key in firing:
            sig = signals[(name, key)]
            c = sig.strategy.candidate_at(sig.sf, int(sig.rows[i]), mids)
            if c is None:
                continue
            params = getattr(sig.strategy, "params", None)
            if params:
                self._params_of[(c.strategy, c.key, c.ts)] = dict(params)
            if (c.strategy, c.key) in open_keys:
                self.cand_row(c, "rejected", "already_open")
                continue
            if not all(leg.asset in mids for leg in c.legs):
                self.cand_row(c, "rejected", "no_data")
                continue
            if kind == "spot" and any(leg.weight * c.direction < 0 for leg in c.legs):
                self.cand_row(c, "rejected", "short_not_allowed")
                continue
            scored = scorer.score(c, t)
            if scored is None:
                self.cand_row(c, "rejected", "ranker")
                continue
            cands.append(scored)
        if not cands:
            return math.nan

        assets = sorted({leg.asset for c in cands for leg in c.legs} | set(pf.holdings)
                        | {leg.asset for g in pend for leg in g.sel.cand.legs if leg.asset in mkt.col})
        lo = max(0, i - CORR_WINDOW + 1)
        returns = pd.DataFrame(mkt.logret[lo:i + 1, [mkt.col[a] for a in assets]], columns=assets)
        # working entries not yet registered as positions count as if filled (a partially filled
        # one is already in the portfolio's positions / risk / gross): in construct (positions,
        # opposite / corr-bucket exposures) and in the risk check (positions, risk, gross, cash)
        unreg = [g for pid, g in self.entry_groups.items() if pid not in pf.positions]
        sels, skipped = construct(cands, pf, self.limits, self.costs, self.instruments, mids, returns,
                                  self.regime_mult, pending=[g.sel.cand for g in unreg],
                                  liquidity=self.order_model)
        for c, reason in skipped:
            self.cand_row(c, "rejected", reason)

        pend_pos = len(unreg)
        pend_risk = sum(g.sel.risk_amount for g in unreg)
        pend_gross = sum(g.sel.gross_notional for g in unreg)
        pend_cash = pend_gross if kind == "spot" else 0.0
        eq = pf.equity()
        acc_pos, acc_risk, acc_gross, acc_cash = 0, 0.0, 0.0, 0.0
        holdings = dict(pf.holdings)
        for sel in sels:
            c = sel.cand
            pr = plan_entry(c, sel.gross_notional, mids, self.instruments, kind, now, self.band,
                            costs=self.costs, order_model=self.order_model)
            if pr.reject_reason:
                self.cand_row(c, "rejected", pr.reject_reason)
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
                post_gross = pf.gross_notional() + pend_gross + acc_gross + batch_gross
                for o in pr.orders:
                    notional = abs(post[o.asset]) * mids[o.asset]
                    mm = self.instruments[o.asset].maintenance_margin
                    liq[o.asset] = math.inf if notional == 0 else max(0.0, (eq - post_gross * mm) / notional)
            res = check(checks, self.ctx(
                t, eq, self.state, now=now, extra_pos=pend_pos + acc_pos,
                extra_risk=pend_risk + acc_risk, extra_gross=pend_gross + acc_gross,
                holdings=dict(holdings), cash_used=pend_cash + acc_cash, liq=liq))
            if res.vetoed:  # all-or-nothing per selection
                self.cand_row(c, "rejected", res.vetoed[0][1])
                self.veto_events(t, res.vetoed)
                continue
            row = self.cand_row(c, "submitted", None)
            pid = pr.orders[0].position_id
            self.entry_groups[pid] = EntryGroup(sel, {o.asset for o in pr.orders}, row,
                                                cids={oc.order.client_id for oc in checks})
            self.state = dataclasses.replace(
                self.state, orders_today=self.state.orders_today + len(checks))
            for oc in checks:
                self.submitted[oc.order.client_id] = (oc.order, oc.mid)
            for oc in checks:
                self.broker.submit(oc.order)
            acc_pos += 1
            acc_risk += sel.risk_amount
            acc_gross += batch_gross
            if kind == "spot":
                acc_cash += sum(oc.notional for oc in checks if oc.order.side == "buy")
            holdings = post
        committed = (pf.gross_notional() + acc_gross) / eq if acc_pos and eq > 0 else math.nan
        return committed
