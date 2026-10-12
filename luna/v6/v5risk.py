"""The retained V5 ``carry.risk`` controls (Erratum 001, docs/results/v6-spec-erratum-001.md).

The frozen constants say ``risk_profile: V5 carry.risk unchanged (config/default.yaml at fabd419)``.
The erratum keeps every V5 rule that does not contradict the frozen H3/H1 construction, with the
values and the measurement of ``luna/risk/engine.py`` at fabd419:

- R5 risk per trade: an opening order's notional x leg_stop_frac (0.15) <= 0.04 E;
- R6 open risk: sum over open legs of notional x 0.15 <= 0.16 E;
- R7 positions <= 4 (H1 only: one hedged pair = one position id; H3: superseded);
- R8 gross notional <= 1.25 E;
- R9 an opening order's notional <= 0.30 E;
- R14 orders per UTC day < 30 before an opening order.

V5 vetoes single orders. An H3 book is one atomic construction (S-H3-5..7: beta, net dollar and the
side-risk ratio hold only for the whole book), so for H3 a violation makes the whole decision
INFEASIBLE instead of opening a partial book (Erratum 001, R5/R6/R8/R9/R14). H1 entries are
independent pairs, so a violation vetoes that entry as in V5.
"""

from __future__ import annotations

V5_LEG_STOP = 0.15  # carry.leg_stop_frac at fabd419: the R5/R6 stop distance
R5_MAX_RISK_PER_TRADE = 0.04
R6_MAX_OPEN_RISK = 0.16
R7_MAX_POSITIONS = 4
R8_MAX_GROSS = 1.25
R9_MAX_LEG = 0.30
R14_MAX_ORDERS_PER_DAY = 30


def violations(opening: list, open_after: list, E: float, positions_after: int | None = None,
               orders_today: int = 0, new_orders: int = 0) -> list[str]:
    """V5 rules violated by a set of opening orders. ``opening``: notionals (USD, >= 0) of the opening
    orders (new legs, or the growth of a leg); ``open_after``: notionals of every open leg after the
    orders (held legs at the decision mid, plus the new ones); ``positions_after``: open position ids
    after the orders (None = R7 not applied); ``orders_today``/``new_orders``: R14."""
    out = []
    if not E > 0:
        return ["R5-R9: equity <= 0"]
    if any(n * V5_LEG_STOP > R5_MAX_RISK_PER_TRADE * E for n in opening):
        out.append("R5_risk_per_trade")
    if sum(n * V5_LEG_STOP for n in open_after) > R6_MAX_OPEN_RISK * E:
        out.append("R6_open_risk")
    if positions_after is not None and positions_after > R7_MAX_POSITIONS:
        out.append("R7_max_positions")
    if sum(open_after) > R8_MAX_GROSS * E:
        out.append("R8_gross_leverage")
    if any(n > R9_MAX_LEG * E for n in opening):
        out.append("R9_leg_notional")
    if opening and orders_today + new_orders > R14_MAX_ORDERS_PER_DAY:
        out.append("R14_orders_per_day")
    return out
