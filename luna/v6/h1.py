"""H1 replay (spec §6): qty-matched spot long + perp short. Historical: taker both legs at t + 15m
(S-H1-5), Kraken public Tier 1 fees, PIT perp spread, the frozen spot-spread assumption. Forward:
the market's S-H1-X pair execution.

Accounts: ``usd`` (real USD: spot purchases/sales, fees of both legs, funding), the spot inventory
(valued at the spot mid) and the perp ledger (``pvc`` = -sum dq * px; perp P&L = pvc + q * mid).
E = usd + sum spot_q * spot_mid + pvc - sum perp_short_q * perp_mid. Perp collateral = perp notional
at entry (1x), reserved from usd; a new entry needs 2N + N * entry_cost <= usd - reserved (S-H1-2).
The perp short quantity is kept separately (a forward partial hedge can differ from the spot qty).
Historical H1 can never be FINAL GO (the ladder caps the holdout at ADVANCE; forward needs S-H1-X).

``invariant_mode`` (blind development runs only): R4, R3, the combined stop and the hedge reset are
off so that no reported mechanical invariant depends on performance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.v6 import v5risk
from luna.v6.engine import SETT_COLS, RunResult, daily_table, fill_row, fills_frame, terminal_cost
from luna.v6.forecast import GateRecord, ModelWarmup
from luna.v6.market import HistMarket
from luna.v6.spec import constants

BAR = pd.Timedelta("15min")
HOUR = pd.Timedelta("1h")
_K = constants()
_H1 = _K["h1"]
_COMMON = _K["common"]
MAX_ASSETS = _H1["max_assets"]
PER_ASSET = _H1["per_asset_hedged_frac_equity"]
TOTAL = _H1["total_hedged_frac_equity"]
STOP = _H1["combined_stop_frac_of_hedged_notional"]
RESET = _H1["hedge_reset_move"]
COOLDOWN = pd.Timedelta(hours=_H1["cooldown_hours"])
MIN_SPOT_VOL = _H1["min_spot_volume_usd"]
MAX_SPREAD = _COMMON["gate_max_spread_bps"] * 1e-4
MIN_VOLUME = _COMMON["gate_min_quote_volume_usd"]
MAX_FUNDING_AGE = pd.Timedelta(hours=_COMMON["gate_max_funding_age_hours"])
KILL_DD = _COMMON["risk_max_drawdown_kill"]
DAILY_LOSS = 0.02
EXIT_SPREAD = 0.0040
MISSING_FC = pd.Timedelta(hours=_K["h2a"]["missing_forecast_exit_hours"])
DECISION_GAP = pd.Timedelta(hours=_K["h3"]["forward_rebalance_gap_hours"])
H = 720
QTY_EPS = 1e-12


@dataclass
class H1Config:
    name: str
    m: float
    row: str = "PRIMARY"


@dataclass
class _Pos:
    q: float  # spot qty (> 0)
    pq: float  # perp short qty (> 0); the perp position is -pq
    spot_entry: float  # average entry mid (stop / reset reference)
    perp_entry: float
    collateral: float
    spot_px: float = math.nan  # average entry FILL price of the spot leg (V5 R6, Erratum 001)
    perp_px: float = math.nan  # average entry FILL price of the perp leg

    def risk(self) -> float:
        """V5 risk amount of the pair's legs: 0.15 x qty x average fill price (fill-time mid only as a
        fallback when no fill price is recorded)."""
        sp = self.spot_px if math.isfinite(self.spot_px) else self.spot_entry
        pp = self.perp_px if math.isfinite(self.perp_px) else self.perp_entry
        return v5risk.V5_LEG_STOP * (self.q * sp + self.pq * pp)


@dataclass
class _St:
    usd: float
    pvc: float = 0.0
    pos: dict = field(default_factory=dict)
    acc: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)  # ((spot fills, perp fills), intent)
    exiting: set = field(default_factory=set)
    cooldown: dict = field(default_factory=dict)
    inelig_prev: set = field(default_factory=set)
    missing_fc: dict = field(default_factory=dict)
    retry: list = field(default_factory=list)
    killed: bool = False
    r3: bool = False
    peak: float = -math.inf
    day_start: float = math.nan
    last: dict = field(default_factory=dict)
    decision_due: pd.Timestamp | None = None
    orders_today: int = 0  # V5 R14


class H1Engine:
    def __init__(self, market: HistMarket, model, insts: dict, cfg: H1Config,
                 equity0: float = float(_COMMON["capital_usd"])) -> None:
        if model.h != H:
            raise ValueError("H1 uses the 720 h H2a model")
        if getattr(market, "spot", None) is None and not hasattr(market, "pair_fill"):
            raise ValueError("H1 needs spot prices")
        self.m, self.model, self.insts, self.cfg, self.E0 = market, model, insts, cfg, equity0

    # --- valuation ---------------------------------------------------------------------------------
    def _fresh(self, t: pd.Timestamp) -> bool:
        f = getattr(self.m, "has_fresh", None)
        return True if f is None else bool(f(t))

    def _px(self, st: _St, a: str, t: pd.Timestamp, leg: str) -> float:
        """Valuation mid (the market's mark view)."""
        if leg == "spot":
            v = getattr(self.m, "mark_spot_mid", self.m.spot_mid)(a, t)
        else:
            v = getattr(self.m, "mark_mid", self.m.mid)(a, t)
        if math.isfinite(v):
            st.last[(a, leg)] = v
            return v
        return st.last.get((a, leg), math.nan)

    def equity(self, st: _St, t: pd.Timestamp) -> float:
        e = st.usd + st.pvc
        for a, p in st.pos.items():
            e += p.q * self._px(st, a, t, "spot") - p.pq * self._px(st, a, t, "perp")
        return e

    def rt(self, a: str, t: pd.Timestamp) -> float:
        """All-in taker round trip per unit of hedged notional known at t (two legs, in and out)."""
        return 2 * (self.m.decision_cost(a, t, "spot") + self.m.decision_cost(a, t, "perp"))

    def _qty_step(self, a: str) -> tuple[float, float, float]:
        """(step = max(spot step, perp step), spot minimum, perp minimum) (S-H1-2)."""
        i = self.insts[a]
        return max(i["spot"]["step"], i["perp"]["step"]), i["spot"]["min_qty"], i["perp"]["min_qty"]

    # --- eligibility -------------------------------------------------------------------------------
    def eligible(self, a: str, t: pd.Timestamp, fc: pd.Series | None) -> bool:
        if fc is None or not math.isfinite(float(fc.get(a, np.nan))):
            return False
        if not (math.isfinite(self.m.mid(a, t)) and math.isfinite(self.m.spot_mid(a, t))):
            return False
        half, _ = self.m.decision_half(a, t)
        if 2 * half > MAX_SPREAD or not (self.m.volume24(a, t) >= MIN_VOLUME):
            return False
        if not (self.m.volume24(a, t, spot=True) >= MIN_SPOT_VOL):
            return False
        newest = self.m.funding.newest_end(a, t)
        return newest is not None and t - newest <= MAX_FUNDING_AGE

    def _holding_ineligible(self, a: str, t: pd.Timestamp) -> bool:
        if not (math.isfinite(self.m.mid(a, t)) and math.isfinite(self.m.spot_mid(a, t))):
            return True
        half, _ = self.m.decision_half(a, t)
        newest = self.m.funding.newest_end(a, t)
        fl = self.m.funding.fl(t, 720, 648).get(a, np.nan)
        return (2 * half > EXIT_SPREAD or not (self.m.volume24(a, t) >= MIN_VOLUME) or newest is None
                or t - newest > MAX_FUNDING_AGE or not math.isfinite(fl))

    # --- orders --------------------------------------------------------------------------------------
    def _pair(self, st: _St, a: str, q: float, t: pd.Timestamp, intent: str, perp_q: float | None = None) -> bool:
        """Open (q > 0) or close (q < 0) through the market's pair execution: historical = atomic taker
        at t + 15m; forward = S-H1-X (maker-first only for the 00:00 economic orders)."""
        st.orders_today += 2
        res = self.m.pair_fill(a, q, t, economic=intent in ("open", "exit"), perp_q=perp_q)
        if res is None or (not res[0] and not res[1]) or (intent == "open" and not res[0]):
            self.counters["unfilled_pairs"] += 1
            if intent != "open":
                st.retry.append((a, intent))
            return False
        st.pending.append(((res[0], res[1]), intent))
        if intent != "open":
            st.exiting.add(a)
        return True

    def _leg(self, st: _St, a: str, leg: str, dq: float, t: pd.Timestamp, intent: str) -> None:
        st.orders_today += 1
        f = self.m.fill(a, dq, t, leg=leg)
        if f is None:
            self.counters["unfilled_pairs"] += 1
            st.retry.append((a, intent))
            return
        st.pending.append((([f], []) if leg == "spot" else ([], [f]), intent))
        st.exiting.add(a)

    def _close(self, st: _St, a: str, t: pd.Timestamp, intent: str) -> None:
        """Any exit closes both legs, each by its own quantity, and starts the 24 h cooldown (S-H1-4). A
        one-legged remainder is closed through its own leg (taker), never through a zero-quantity pair."""
        if a not in st.pos or a in st.exiting:
            return
        st.cooldown[a] = t + COOLDOWN
        ps = st.pos[a]
        if ps.q > QTY_EPS and ps.pq > QTY_EPS:
            self._pair(st, a, -ps.q, t, intent, perp_q=ps.pq)
        elif ps.pq > QTY_EPS:
            self._leg(st, a, "perp", ps.pq, t, intent)
        elif ps.q > QTY_EPS:
            self._leg(st, a, "spot", -ps.q, t, intent)

    def _reconcile(self, st: _St, t: pd.Timestamp) -> None:
        """Hedge integrity (S-H1-1 qty-matched): a pair whose legs differ by more than one quantity step
        (one leg filled, the other not) is closed at once, both legs by their own quantities."""
        for a, ps in list(st.pos.items()):
            if a in st.exiting:
                continue
            step = self._qty_step(a)[0] if a in self.insts else QTY_EPS
            if abs(ps.q - ps.pq) > step + QTY_EPS:
                self.counters["hedge_reconciliations"] += 1
                self._close(st, a, t, "reconcile")

    # --- run ---------------------------------------------------------------------------------------
    def run(self, start: pd.Timestamp, end: pd.Timestamp, invariant_mode: bool = False) -> RunResult:
        start, end = pd.Timestamp(start), pd.Timestamp(end).floor("15min")
        st = _St(usd=self.E0)
        st.day_start = self.E0
        self.counters = {"unfilled_pairs": 0, "funding_unknown": 0, "kill": 0, "liquidation": 0, "r3_days": 0,
                         "stops": 0, "hedge_resets": 0, "inelig_exits": 0, "decisions": 0, "warmup_decisions": 0,
                         "entries": 0, "economic_exits": 0, "missing_fc_exits": 0, "skipped_cash": 0,
                         "skipped_size": 0, "stale_ticks": 0, "deferred_decisions": 0, "skipped_decision_days": 0,
                         "hedge_reconciliations": 0}
        self._inv = invariant_mode
        fills, sett, ticks, decisions, events, gates = [], [], [], [], [], []
        t = start
        while t <= end:
            E_pre = self.equity(st, t)
            if t > start:
                p = (t - BAR).floor("h")
                for a, ps in st.pos.items():
                    st.acc[(a, p)] = st.acc.get((a, p), 0.0) - ps.pq * 0.25  # perp qty = -pq
                if t.minute == 0:
                    pe = t - HOUR
                    sp = getattr(self.m, "settlement", None) or self.m.funding
                    for key in [k for k in st.acc if k[1] == pe]:
                        rate = sp.abs_rate(key[0], pe, t)
                        amt = -st.acc[key] * rate if math.isfinite(rate) else 0.0
                        if not math.isfinite(rate):
                            self.counters["funding_unknown"] += 1
                        st.usd += amt
                        sett.append({"period_start": pe, "ts": t, "asset": key[0], "amount": amt,
                                     "known": math.isfinite(rate), "qty_hours": st.acc[key]})
                        del st.acc[key]

            def last_ts(x):
                return max(f.ts for f in x[0][0] + x[0][1])

            due = [x for x in st.pending if last_ts(x) <= t]
            st.pending = [x for x in st.pending if last_ts(x) > t]
            for (sfs, pfs), intent in due:
                a = (sfs or pfs)[0].asset
                dq = sum(f.dq for f in sfs)
                dp = sum(f.dq for f in pfs)  # perp: negative opens a short, positive buys back
                for f in sfs:
                    st.usd -= f.dq * f.px + f.fee
                for f in pfs:
                    st.usd -= f.fee
                    st.pvc -= f.dq * f.px
                ps = st.pos.get(a)
                if ps is None and dq > 0:
                    smid = sum(f.mid * f.dq for f in sfs) / dq
                    pq = -dp
                    pmid = sum(f.mid * abs(f.dq) for f in pfs) / pq if pq > 0 else math.nan
                    spx = sum(f.px * f.dq for f in sfs) / dq
                    ppx = sum(f.px * abs(f.dq) for f in pfs) / pq if pq > 0 else math.nan
                    st.pos[a] = _Pos(dq, pq, smid, pmid, pq * pmid if pq > 0 else 0.0, spx, ppx)
                elif ps is not None:
                    q1, pq1 = ps.q + dq, ps.pq - dp
                    if q1 <= QTY_EPS and pq1 <= QTY_EPS:
                        st.pos.pop(a, None)
                    else:
                        ps.collateral *= (pq1 / ps.pq) if ps.pq > 0 else 0.0
                        ps.q, ps.pq = max(q1, 0.0), max(pq1, 0.0)
                st.exiting.discard(a)
                for f in sfs + pfs:
                    fills.append(fill_row(f, intent))
            E = self.equity(st, t)
            hedged = sum(ps.pq * self._px(st, a, t, "perp") for a, ps in st.pos.items())
            ticks.append({"t": t, "equity_pre": E_pre, "equity": E, "gross": hedged, "n_pairs": len(st.pos)})
            for a, ps in st.pos.items():
                if ps.pq > 0 and ps.collateral - ps.pq * (self._px(st, a, t, "perp") - ps.perp_entry) <= 0:
                    self.counters["liquidation"] += 1
            if t.hour == 0 and t.minute == 0 and t > start:
                st.r3, st.day_start, st.orders_today = False, E, 0
            if t.hour == 0 and t.minute == 0 and t <= end - BAR:
                st.decision_due = t
            if not self._fresh(t):
                self.counters["stale_ticks"] += 1
                if st.decision_due is not None and t - st.decision_due > DECISION_GAP:
                    self.counters["skipped_decision_days"] += 1
                    st.decision_due = None
                t += BAR
                continue
            st.peak = max(st.peak, E)
            if not invariant_mode and not st.killed and E <= (1 - KILL_DD) * st.peak:
                st.killed = True
                self.counters["kill"] += 1
                events.append({"t": t, "event": "kill"})
                if t < end:
                    for a in list(st.pos):
                        self._close(st, a, t, "kill")
            if not invariant_mode and not st.r3 and E <= (1 - DAILY_LOSS) * st.day_start:
                st.r3 = True
                self.counters["r3_days"] += 1
            retry, st.retry = st.retry, []
            for a, intent in retry:
                self._close(st, a, t, intent)
            if t < end:
                self._reconcile(st, t)
            inelig_now = set()
            if t < end:
                for a, ps in list(st.pos.items()):
                    if a in st.exiting:
                        continue
                    sm, pm = self.m.spot_mid(a, t), self.m.mid(a, t)
                    if not invariant_mode and math.isfinite(sm) and math.isfinite(pm):
                        pnl = ps.q * (sm - ps.spot_entry) - ps.pq * (pm - ps.perp_entry)
                        if pnl <= -STOP * ps.pq * ps.perp_entry:
                            self.counters["stops"] += 1
                            events.append({"t": t, "event": "combined_stop", "asset": a})
                            self._close(st, a, t, "stop")
                            continue
                        if abs(sm / ps.spot_entry - 1) >= RESET:
                            self.counters["hedge_resets"] += 1
                            events.append({"t": t, "event": "hedge_reset", "asset": a})
                            self._close(st, a, t, "reset")
                            continue
                    if self._holding_ineligible(a, t):
                        inelig_now.add(a)
                        if a in st.inelig_prev:
                            self.counters["inelig_exits"] += 1
                            self._close(st, a, t, "inelig")
            st.inelig_prev = inelig_now
            if st.decision_due is not None:
                if t - st.decision_due > DECISION_GAP:
                    self.counters["skipped_decision_days"] += 1
                    st.decision_due = None
                elif t <= end - BAR:
                    if t != st.decision_due:
                        self.counters["deferred_decisions"] += 1
                    self._decide(st, t, E, decisions, gates, gate_only=st.killed)
                    st.decision_due = None
            if t < end and not st.killed:
                for a, since in list(st.missing_fc.items()):
                    if a in st.pos and a not in st.exiting and t - since > MISSING_FC:
                        self.counters["missing_fc_exits"] += 1
                        self._close(st, a, t, "missing_fc")
            t += BAR
        for a, ps in list(st.pos.items()):
            for leg, q in (("spot", -ps.q), ("perp", ps.pq)):
                if q == 0:
                    continue
                mid, fee, ss = terminal_cost(self.m, a, -q, end, st.last.get((a, leg), math.nan), leg=leg)
                px = mid + math.copysign(ss / abs(q), q)
                if leg == "spot":
                    st.usd -= q * px + fee
                else:
                    st.pvc -= q * px
                    st.usd -= fee
                fills.append({"decision_ts": end, "ts": end, "asset": a, "leg": leg, "dq": q, "px": px, "mid": mid,
                              "mid_decision": mid, "half": math.nan, "fee": fee, "spread_slip": ss,
                              "fallback": False, "intent": "terminal", "notional": abs(q) * mid, "maker": False})
        st.pos = {}
        E_end = st.usd + st.pvc
        ticks.append({"t": end, "equity_pre": E_end, "equity": E_end, "gross": 0.0, "n_pairs": 0, "terminal": True})
        F = fills_frame(fills)
        S = pd.DataFrame(sett, columns=SETT_COLS)
        T = pd.DataFrame(ticks)
        return RunResult(self.cfg.name, self.cfg.row, start, end, self.E0, F, S, T, daily_table(F, S, T, start, end, self.E0),
                         decisions, events, gates, dict(self.counters))

    # --- daily decision (S-H1-3 / S-H1-4) ----------------------------------------------------------
    def _decide(self, st: _St, t: pd.Timestamp, E: float, decisions: list, gates: list, gate_only: bool = False) -> None:
        try:
            fc = self.model.forecast(t)
        except ModelWarmup:
            fc = None
        if gate_only:
            if fc is not None:
                elig = [a for a in self.m.assets if a in fc.index and a in self.insts and self.eligible(a, t, fc)]
                gates.append(GateRecord(t, elig, fc.copy()))
            return
        self.counters["decisions"] += 1
        if fc is None:
            self.counters["warmup_decisions"] += 1
        for a in list(st.pos):
            if a in st.exiting:
                continue
            if fc is None or not math.isfinite(float(fc.get(a, np.nan))):
                st.missing_fc.setdefault(a, t)
            else:
                st.missing_fc.pop(a, None)
        for a in list(st.missing_fc):
            if a not in st.pos:
                st.missing_fc.pop(a)
        if fc is None:
            decisions.append({"t": t, "status": "MODEL WARMUP"})
            return
        elig = [a for a in self.m.assets if a in fc.index and a in self.insts and self.eligible(a, t, fc)]
        gates.append(GateRecord(t, elig, fc.copy()))
        rec = {"t": t, "n_eligible": len(elig), "exits": [], "entries": []}
        for a in list(st.pos):
            if a in st.exiting or a not in fc.index or not math.isfinite(float(fc[a])):
                continue
            if float(fc[a]) * H < self.rt(a, t) / 2:
                self.counters["economic_exits"] += 1
                rec["exits"].append(a)
                self._close(st, a, t, "exit")
        held = [a for a in st.pos if a not in st.exiting]
        reserved = sum(st.pos[a].collateral for a in held)
        free = st.usd - reserved
        if getattr(self, "_inv", False):  # invariant mode: sizing on the initial capital (no P&L signal)
            E = self.E0
            free = self.E0 - sum(2 * st.pos[a].collateral for a in held)
        cands = sorted((a for a in elig if a not in st.pos and st.cooldown.get(a, t) <= t and float(fc[a]) > 0
                        and float(fc[a]) * H >= self.cfg.m * self.rt(a, t)), key=lambda a: (-float(fc[a]), a))
        if st.r3:
            cands = []
        hedged = sum(st.pos[a].pq * self._px(st, a, t, "perp") for a in held)
        entered: list = []
        for a in cands:
            if len(held) >= MAX_ASSETS:
                break
            entry_cost = self.m.decision_cost(a, t, "spot") + self.m.decision_cost(a, t, "perp")
            n_cap = min(PER_ASSET * E, TOTAL * E - hedged)
            n = min(n_cap, max(0.0, free) / (2 + entry_cost))
            px = self.m.spot_mid(a, t)
            step, smin, pmin = self._qty_step(a)
            q = math.floor(n / px / step + 1e-9) * step
            if q <= 0 or q < smin or q < pmin:
                self.counters["skipped_size" if n_cap > 0 and free > 0 else "skipped_cash"] += 1
                continue
            need = q * px * (2 + entry_cost)  # 2N + N * entry cost (S-H1-2)
            if need > free + 1e-9:
                self.counters["skipped_cash"] += 1
                continue
            # Erratum 001: retained V5 controls per entry (spot leg + perp leg; one pair = one position id)
            kept = [b for b in held if b in st.pos]
            gross_after = [n for b in kept for n in (st.pos[b].q * self._px(st, b, t, "spot"),
                                                     st.pos[b].pq * self._px(st, b, t, "perp"))]
            risk_after = [st.pos[b].risk() for b in kept]  # held pairs at their average fill prices
            gross_after += entered + [q * px, q * px]  # pairs sent earlier in this decision, then this pair
            risk_after += [v5risk.V5_LEG_STOP * n for n in entered + [q * px, q * px]]
            bad = v5risk.violations([q * px, q * px], gross_after, risk_after, E, positions_after=len(held) + 1,
                                    orders_before=[st.orders_today, st.orders_today + 1])
            if bad:
                self.counters["v5_vetoes"] = self.counters.get("v5_vetoes", 0) + 1
                continue
            if self._pair(st, a, q, t, "open"):
                entered += [q * px, q * px]
                self.counters["entries"] += 1
                rec["entries"].append(a)
                held.append(a)
                hedged += q * px
                free -= need
        decisions.append(rec)


def reprice_maker_bound(primary: RunResult) -> RunResult:
    """S-MAKER-BOUND (spec S-H1-6): PRIMARY's own orders (the same decisions, quantities and
    timestamps, incl. stops, resets and the terminal unwind) re-priced at the fill-time mid with the
    maker fees of each leg, no spread and no slip, 100 % filled. Funding is unchanged (same positions).
    Equity at t = PRIMARY equity + the cost saved by the fills stamped up to t."""
    from luna.v6.market import FUT_MAKER, SPOT_MAKER

    F = primary.fills.copy()
    rate = np.where(F["leg"] == "spot", SPOT_MAKER, FUT_MAKER)
    new_fee = rate * F["dq"].abs() * F["mid"]
    saving = (F["fee"] + F["spread_slip"]) - new_fee
    F["px"], F["fee"], F["spread_slip"] = F["mid"], new_fee, 0.0
    T = primary.ticks.copy()
    ts = F["ts"].to_numpy()
    order = np.argsort(ts, kind="stable")
    cum = np.cumsum(saving.to_numpy()[order])
    sts = ts[order]
    tt = T["t"].to_numpy()
    post = np.searchsorted(sts, tt, "right")
    pre = np.searchsorted(sts, tt, "left")
    add_post = np.where(post > 0, cum[np.maximum(post - 1, 0)], 0.0)
    add_pre = np.where(pre > 0, cum[np.maximum(pre - 1, 0)], 0.0)
    T["equity"] = T["equity"].to_numpy() + add_post
    T["equity_pre"] = T["equity_pre"].to_numpy() + add_pre
    daily = daily_table(F, primary.settlements, T, primary.start, primary.end, primary.equity0)
    return RunResult(primary.name, "S-MAKER-BOUND", primary.start, primary.end, primary.equity0, F,
                     primary.settlements, T, daily, primary.decisions, primary.events, primary.gate_records,
                     dict(primary.counters))
