"""H1 historical replay (spec §6): qty-matched spot long + perp short, taker both legs at t + 15m
(S-H1-5), Kraken public Tier 1 fees, PIT perp spread, the frozen spot-spread assumption.

Accounts: ``usd`` (real USD: spot purchases/sales, fees of both legs, funding), the spot inventory
(valued at the spot mid) and the perp ledger (``pvc`` = -sum dq * px; perp P&L = pvc + q * mid).
E = usd + sum spot_q * spot_mid + pvc + sum perp_q * perp_mid. Perp collateral = perp notional at
entry (1x), reserved from usd; a new entry needs 2N + entry costs <= usd - reserved (S-H1-2).
Historical H1 can never be FINAL GO (the ladder caps the holdout at ADVANCE; forward needs S-H1-X).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.v6 import riskmodel as rm
from luna.v6.engine import FILL_COLS, SETT_COLS, RunResult, daily_table
from luna.v6.forecast import ModelWarmup
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
H = 720


@dataclass
class H1Config:
    name: str
    m: float
    row: str = "PRIMARY"


@dataclass
class _Pos:
    q: float  # spot qty (> 0); perp qty = -q
    spot_entry: float
    perp_entry: float
    collateral: float


@dataclass
class _St:
    usd: float
    pvc: float = 0.0
    pos: dict = field(default_factory=dict)
    acc: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)  # (fills tuple, intent)
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


class H1Engine:
    def __init__(self, market: HistMarket, model, insts: dict, cfg: H1Config,
                 equity0: float = float(_COMMON["capital_usd"])) -> None:
        if model.h != H:
            raise ValueError("H1 uses the 720 h H2a model")
        if market.spot is None:
            raise ValueError("H1 needs spot bars")
        self.m, self.model, self.insts, self.cfg, self.E0 = market, model, insts, cfg, equity0

    # --- valuation ---------------------------------------------------------------------------------
    def _px(self, st: _St, a: str, t: pd.Timestamp, leg: str) -> float:
        v = self.m.spot_mid(a, t) if leg == "spot" else self.m.mid(a, t)
        if math.isfinite(v):
            st.last[(a, leg)] = v
            return v
        return st.last.get((a, leg), math.nan)

    def equity(self, st: _St, t: pd.Timestamp) -> float:
        e = st.usd + st.pvc
        for a, p in st.pos.items():
            e += p.q * self._px(st, a, t, "spot") - p.q * self._px(st, a, t, "perp")
        return e

    def rt(self, a: str, t: pd.Timestamp) -> float:
        """All-in taker round trip per unit of hedged notional known at t (two legs, in and out)."""
        return 2 * (self.m.decision_cost(a, t, "spot") + self.m.decision_cost(a, t, "perp"))

    def _qty_step(self, a: str) -> tuple[float, float]:
        i = self.insts[a]
        step = max(i["perp"]["step"], i["perp"]["min_qty"], i["spot"]["step"], i["spot"]["min_qty"])
        return step, max(i["perp"]["min_qty"], i["spot"]["min_qty"])

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

    # --- orders (atomic two-leg) -------------------------------------------------------------------
    def _pair(self, st: _St, a: str, q: float, t: pd.Timestamp, intent: str) -> bool:
        """Open (q > 0) or close (q < 0) the pair through the market's pair execution: historical =
        atomic taker at t + 15m; forward = S-H1-X (maker-first only for the 00:00 economic orders)."""
        res = self.m.pair_fill(a, q, t, economic=intent in ("open", "exit"))
        if res is None or not res[0]:
            self.counters["unfilled_pairs"] += 1
            if intent != "open":
                st.retry.append((a, intent))
            return False
        st.pending.append(((res[0], res[1]), intent))
        if intent != "open":
            st.exiting.add(a)
        return True

    def _close(self, st: _St, a: str, t: pd.Timestamp, intent: str) -> None:
        if a in st.pos and a not in st.exiting:
            self._pair(st, a, -st.pos[a].q, t, intent)

    # --- run ---------------------------------------------------------------------------------------
    def run(self, start: pd.Timestamp, end: pd.Timestamp) -> RunResult:
        start, end = pd.Timestamp(start), pd.Timestamp(end).floor("15min")
        st = _St(usd=self.E0)
        st.day_start = self.E0
        self.counters = {"unfilled_pairs": 0, "funding_unknown": 0, "kill": 0, "liquidation": 0, "r3_days": 0,
                         "stops": 0, "hedge_resets": 0, "inelig_exits": 0, "decisions": 0, "warmup_decisions": 0,
                         "entries": 0, "economic_exits": 0, "missing_fc_exits": 0, "skipped_cash": 0, "skipped_size": 0}
        fills, sett, ticks, decisions, events, gates = [], [], [], [], [], []
        t = start
        while t <= end:
            E_pre = self.equity(st, t)
            if t > start:
                p = (t - BAR).floor("h")
                for a, ps in st.pos.items():
                    st.acc[(a, p)] = st.acc.get((a, p), 0.0) - ps.q * 0.25  # perp qty = -q
                if t.minute == 0:
                    pe = t - HOUR
                    for key in [k for k in st.acc if k[1] == pe]:
                        sp = getattr(self.m, "settlement", None) or self.m.funding
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
                a = sfs[0].asset
                dq = sum(f.dq for f in sfs)
                for f in sfs:
                    st.usd -= f.dq * f.px + f.fee
                for f in pfs:
                    st.usd -= f.fee
                    st.pvc -= f.dq * f.px
                ps = st.pos.get(a)
                if ps is None and dq > 0:
                    smid = sum(f.mid * f.dq for f in sfs) / dq
                    pq = -sum(f.dq for f in pfs)
                    pmid = sum(f.mid * abs(f.dq) for f in pfs) / pq if pq else math.nan
                    st.pos[a] = _Pos(dq, smid, pmid, pq * pmid)
                elif ps is not None:
                    q1 = ps.q + dq
                    if q1 <= 1e-12:
                        st.pos.pop(a, None)
                    else:
                        ps.collateral *= q1 / ps.q
                        ps.q = q1
                st.exiting.discard(a)
                for f in sfs + pfs:
                    fills.append({"decision_ts": f.decision_ts, "ts": f.ts, "asset": a, "leg": f.leg, "dq": f.dq,
                                  "px": f.px, "mid": f.mid, "mid_decision": f.mid_decision, "half": f.half,
                                  "fee": f.fee, "spread_slip": f.spread_slip, "fallback": f.fallback,
                                  "intent": intent, "notional": abs(f.dq) * f.mid})
            E = self.equity(st, t)
            hedged = sum(ps.q * self._px(st, a, t, "perp") for a, ps in st.pos.items())
            ticks.append({"t": t, "equity_pre": E_pre, "equity": E, "gross": hedged, "n_pairs": len(st.pos)})
            for a, ps in st.pos.items():
                perp_pnl = -ps.q * (self._px(st, a, t, "perp") - ps.perp_entry)
                if ps.collateral + perp_pnl <= 0:
                    self.counters["liquidation"] += 1
            if t.hour == 0 and t.minute == 0 and t > start:
                st.r3, st.day_start = False, E
            st.peak = max(st.peak, E)
            if not st.killed and E <= (1 - KILL_DD) * st.peak:
                st.killed = True
                self.counters["kill"] += 1
                events.append({"t": t, "event": "kill"})
                if t < end:
                    for a in list(st.pos):
                        self._close(st, a, t, "kill")
            if not st.r3 and E <= (1 - DAILY_LOSS) * st.day_start:
                st.r3 = True
                self.counters["r3_days"] += 1
            retry, st.retry = st.retry, []
            for a, intent in retry:
                self._close(st, a, t, intent)
            inelig_now = set()
            if t < end:
                for a, ps in list(st.pos.items()):
                    if a in st.exiting:
                        continue
                    sm, pm = self.m.spot_mid(a, t), self.m.mid(a, t)
                    if math.isfinite(sm) and math.isfinite(pm):
                        pnl = ps.q * (sm - ps.spot_entry) - ps.q * (pm - ps.perp_entry)
                        if pnl <= -STOP * ps.q * ps.perp_entry:
                            self.counters["stops"] += 1
                            st.cooldown[a] = t + COOLDOWN
                            events.append({"t": t, "event": "combined_stop", "asset": a})
                            self._close(st, a, t, "stop")
                            continue
                        if abs(sm / ps.spot_entry - 1) >= RESET:
                            self.counters["hedge_resets"] += 1
                            st.cooldown[a] = t + COOLDOWN
                            events.append({"t": t, "event": "hedge_reset", "asset": a})
                            self._close(st, a, t, "reset")
                            continue
                    if self._holding_ineligible(a, t):
                        inelig_now.add(a)
                        if a in st.inelig_prev:
                            self.counters["inelig_exits"] += 1
                            st.cooldown[a] = t + COOLDOWN
                            self._close(st, a, t, "inelig")
            st.inelig_prev = inelig_now
            if t.hour == 0 and t.minute == 0 and t <= end - BAR and not st.killed:
                self._decide(st, t, E, decisions, gates)
            if t < end and not st.killed:
                for a, since in list(st.missing_fc.items()):
                    if a in st.pos and a not in st.exiting and t - since > MISSING_FC:
                        self.counters["missing_fc_exits"] += 1
                        self._close(st, a, t, "missing_fc")
            t += BAR
        for a, ps in list(st.pos.items()):
            for leg, q in (("spot", -ps.q), ("perp", ps.q)):
                mid, fee, ss = self.m.unwind_cost(a, -q, end, leg=leg)
                if not math.isfinite(mid):
                    mid = st.last.get((a, leg), math.nan)
                px = mid + math.copysign(ss / abs(q), q)
                if leg == "spot":
                    st.usd -= q * px + fee
                else:
                    st.pvc -= q * px
                    st.usd -= fee
                fills.append({"decision_ts": end, "ts": end, "asset": a, "leg": leg, "dq": q, "px": px, "mid": mid,
                              "mid_decision": mid, "half": math.nan, "fee": fee, "spread_slip": ss,
                              "fallback": False, "intent": "terminal", "notional": abs(q) * mid})
        st.pos = {}
        E_end = st.usd + st.pvc
        ticks.append({"t": end, "equity_pre": E_end, "equity": E_end, "gross": 0.0, "n_pairs": 0, "terminal": True})
        F = pd.DataFrame(fills, columns=FILL_COLS)
        S = pd.DataFrame(sett, columns=SETT_COLS)
        T = pd.DataFrame(ticks)
        return RunResult(self.cfg.name, self.cfg.row, start, end, self.E0, F, S, T, daily_table(F, S, T, start, end, self.E0),
                         decisions, events, gates, dict(self.counters))

    # --- daily decision (S-H1-3 / S-H1-4) ----------------------------------------------------------
    def _decide(self, st: _St, t: pd.Timestamp, E: float, decisions: list, gates: list) -> None:
        from luna.v6.forecast import GateRecord

        self.counters["decisions"] += 1
        try:
            fc = self.model.forecast(t)
        except ModelWarmup:
            fc = None
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
        cands = sorted((a for a in elig if a not in st.pos and st.cooldown.get(a, t) <= t and float(fc[a]) > 0
                        and float(fc[a]) * H >= self.cfg.m * self.rt(a, t)), key=lambda a: (-float(fc[a]), a))
        if st.r3:
            cands = []
        hedged = sum(abs(st.pos[a].q) * self._px(st, a, t, "perp") for a in held)
        for a in cands:
            if len(held) >= MAX_ASSETS:
                break
            entry_cost = self.m.decision_cost(a, t, "spot") + self.m.decision_cost(a, t, "perp")
            n_cap = min(PER_ASSET * E, TOTAL * E - hedged)
            n = min(n_cap, max(0.0, free) / (2 + entry_cost))
            px = self.m.spot_mid(a, t)
            step, minq = self._qty_step(a)
            q = math.floor(n / px / step + 1e-9) * step
            if q < minq or q <= 0:
                self.counters["skipped_size" if n_cap > 0 and free > 0 else "skipped_cash"] += 1
                continue
            if 2 * q * px * (1 + entry_cost) > free:
                self.counters["skipped_cash"] += 1
                continue
            if self._pair(st, a, q, t, "open"):
                self.counters["entries"] += 1
                rec["entries"].append(a)
                held.append(a)
                hedged += q * px
                free -= 2 * q * px * (1 + entry_cost)
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
