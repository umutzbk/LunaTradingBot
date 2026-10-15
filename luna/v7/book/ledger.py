"""Book cash, positions, 15-minute marks, funding flows and the daily table (spec S-8.1, S-8.3, S-4.6).

Equity E = cash + sum_j q_j * p_j, where p_j is the instrument's latest price event: the reference mid of its
last fill or its mark at the last 15-minute tick (S-8.3 event prices). Cash moves only by
* fills: ``-dq * ref_mid - fee - spread_cost - slip_cost`` (= -dq * fill price - fee, S-5.3 prices);
* funding at settlement: ``-q * M(tau) * r`` (a long pays when r > 0, S-4.6).
The price component accumulates the S-8.3 event sum ``q(e_k+) (p(e_{k+1}) - p(e_k))`` per instrument, so the
daily rows satisfy net = funding - fees - spread_slip + price (up to float rounding) and the price column
sums to PR. Daily rows are UTC days: the 00:00 tick (after the 00:00 settlement) closes a day; the run's last
row closes at the terminal unwind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from luna.v7.book.execution import ExecView, Fill
from luna.v7.timeutil import D

# Column schemas of the ledger tables (the rows built below), so an empty table still carries its columns
# (DK-113 / red-team m9)
FILL_COLS: tuple[str, ...] = (*Fill.__dataclass_fields__, "role", "side", "q_after", "entry_decision_ts")
FUNDING_COLS = ("ts", "symbol", "instance", "position_id", "role", "qty", "rate", "mark", "cash")
POSITION_COLS = ("ts", "symbol", "instance", "position_id", "role", "qty", "mark", "mark_source", "notional",
                 "pending_exit")
MARK_COLS = ("ts", "equity", "cash", "gross_legs", "hedge_notional", "btc_mark")
DAILY_COLS = ("day", "start_ts", "end_ts", "equity_start", "equity_end", "net", "funding", "fees",
              "spread_slip", "price")

Role = Literal["leg", "hedge"]


@dataclass
class Position:
    """Mutable engine state of one position episode (one instrument instance, E10)."""

    pid: str
    symbol: str
    role: Role
    view: ExecView
    side: int  # leg side (+1 long / -1 short); the hedge's current sign
    entry_decision_ts: int
    opened_ts: int
    beta: float | None = None  # beta_hat of the opening decision (S-8.3 beta_episode)
    q: float = 0.0  # signed base quantity (book units)
    avg_entry: float = 0.0  # average entry fill price
    p_last: float | None = None  # latest event price (fill ref mid / tick mark)
    pending_exit: str | None = None  # exit reason once an exit is issued
    waiting: bool = False  # S-5.4 wait without any executable bar before the window end
    # [post, end) of the E_M maker limit of the issued exit (DK-77): the exit rests as a maker order only
    # inside it (a forced exit may replace it then); None when no limit was posted
    maker_rest: tuple[int, int] | None = None

    def maker_resting(self, now: int) -> bool:
        """The issued exit is an E_M maker limit actually resting at ``now`` (DK-77, red-team m7)."""
        return self.maker_rest is not None and self.maker_rest[0] <= now < self.maker_rest[1]


@dataclass
class Ledger:
    cash: float
    start_ts: int
    pos: dict[str, Position] = field(default_factory=dict)
    fills: list[dict] = field(default_factory=list)
    funding: list[dict] = field(default_factory=list)
    marks: list[dict] = field(default_factory=list)
    positions: list[dict] = field(default_factory=list)
    daily: list[dict] = field(default_factory=list)
    _day: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._open_day(self.start_ts, self.cash)

    def _open_day(self, ts: int, equity: float) -> None:
        self._day = dict(day=int(ts) // D, start_ts=int(ts), equity_start=equity, funding=0.0, fees=0.0,
                         spread_slip=0.0, price=0.0)

    @property
    def day_start_ts(self) -> int:
        return int(self._day["start_ts"])

    def equity(self) -> float:
        return self.cash + sum(p.q * p.p_last for p in self.pos.values() if p.p_last is not None)

    def _price_event(self, p: Position, price: float) -> None:
        if p.p_last is not None:
            self._day["price"] += p.q * (price - p.p_last)
        p.p_last = price

    def apply_fill(self, f: Fill, p: Position, closes: bool) -> None:
        """Book a fill on position ``p`` (S-8.1: at its own timestamp; S-8.3: price event at ref_mid)."""
        self._price_event(p, f.ref_mid)
        self.cash -= f.dq * f.ref_mid + f.fee + f.spread_cost + f.slip_cost
        self._day["fees"] += f.fee
        self._day["spread_slip"] += f.spread_cost + f.slip_cost
        q0 = p.q
        if q0 == 0 or (q0 > 0) == (f.dq > 0):  # opening / increasing: average entry fill price
            p.avg_entry = (abs(q0) * p.avg_entry + f.qty * f.price) / (abs(q0) + f.qty)
        p.q = 0.0 if closes else q0 + f.dq
        if p.role == "hedge" and p.q != 0:
            p.side = 1 if p.q > 0 else -1
        row = {k: getattr(f, k) for k in Fill.__dataclass_fields__}
        row.update(role=p.role, side=p.side, q_after=p.q, entry_decision_ts=p.entry_decision_ts)
        self.fills.append(row)

    def apply_funding(self, ts: int, p: Position, rate: float, mark: float) -> None:
        cash = -p.q * mark * rate
        self.cash += cash
        self._day["funding"] += cash
        self.funding.append(dict(ts=int(ts), symbol=p.symbol, instance=p.view.inst.idx, position_id=p.pid,
                                 role=p.role, qty=p.q, rate=rate, mark=mark, cash=cash))

    def mark(self, ts: int, prices: dict[str, tuple[float, str]], btc_mark: float | None) -> float:
        """15-minute tick (S-8.1): every open position marked (tick before same-timestamp fills, S-8.3)."""
        gross = {"leg": 0.0, "hedge": 0.0}
        for pid in sorted(self.pos):
            p = self.pos[pid]
            price, src = prices[pid]
            self._price_event(p, price)
            gross[p.role] += abs(p.q) * price
            self.positions.append(dict(ts=int(ts), symbol=p.symbol, instance=p.view.inst.idx, position_id=pid,
                                       role=p.role, qty=p.q, mark=price, mark_source=src,
                                       notional=p.q * price, pending_exit=p.pending_exit))
        eq = self.equity()
        hedge = sum(p.q * p.p_last for p in self.pos.values() if p.role == "hedge" and p.p_last is not None)
        self.marks.append(dict(ts=int(ts), equity=eq, cash=self.cash, gross_legs=gross["leg"],
                               hedge_notional=hedge, btc_mark=btc_mark))
        return eq

    def close_day(self, ts: int, equity: float, final: bool = False) -> None:
        d = self._day
        self.daily.append(dict(day=d["day"], start_ts=d["start_ts"], end_ts=int(ts),
                               equity_start=d["equity_start"], equity_end=equity,
                               net=equity - d["equity_start"], funding=d["funding"], fees=d["fees"],
                               spread_slip=d["spread_slip"], price=d["price"]))
        if not final:
            self._open_day(ts, equity)
