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

import math

V5_LEG_STOP = 0.15  # carry.leg_stop_frac at fabd419: the R5/R6 stop distance
R5_MAX_RISK_PER_TRADE = 0.04
R6_MAX_OPEN_RISK = 0.16
R7_MAX_POSITIONS = 4
R8_MAX_GROSS = 1.25
R9_MAX_LEG = 0.30
R14_MAX_ORDERS_PER_DAY = 30


def violations(opening: list, gross_after: list, risk_after: list, E: float, positions_after: int | None = None,
               orders_before: list = ()) -> list[str]:
    """V5 rules violated by the opening orders of one decision (luna/risk/engine.py at fabd419).

    ``opening``: notionals (USD) of the opening orders (a new leg, or the growth of one); closes and
    reductions are exempt from R5-R9 and R14, as in V5, so with no opening nothing is checked.
    ``gross_after``: notionals of every leg after the decision (R8). ``risk_after``: the V5 risk amount of
    every leg after the decision, 0.15 x qty x ENTRY price for held quantity (scaled down on a reduction)
    and 0.15 x notional for opened quantity (R6, as V5 ``risk_amount``). ``positions_after``: open
    position ids after the decision (R7; None = not applied). ``orders_before``: for each opening order,
    the orders already sent that UTC day before it (R14: an open is vetoed once 30 were sent).
    Non-finite inputs fail closed."""
    if not opening:
        return []
    vals = list(opening) + list(gross_after) + list(risk_after) + [E]
    if not all(math.isfinite(float(x)) for x in vals) or not E > 0:
        return ["V5 inputs not finite"]
    out = []
    if any(n * V5_LEG_STOP > R5_MAX_RISK_PER_TRADE * E for n in opening):
        out.append("R5_risk_per_trade")
    if sum(risk_after) > R6_MAX_OPEN_RISK * E:
        out.append("R6_open_risk")
    if positions_after is not None and positions_after > R7_MAX_POSITIONS:
        out.append("R7_max_positions")
    if sum(gross_after) > R8_MAX_GROSS * E:
        out.append("R8_gross_leverage")
    if any(n > R9_MAX_LEG * E for n in opening):
        out.append("R9_leg_notional")
    if any(k >= R14_MAX_ORDERS_PER_DAY for k in orders_before):
        out.append("R14_orders_per_day")
    return out
