"""Implementation interpretations of the frozen V6 spec (spec §0: the code follows the spec word
for word; where the text leaves a mechanical detail open, the choice is written here, made before
any outcome, and goes to the independent reviews). None of these was chosen by looking at P&L.
"""

DECLARED = {
    "D-R1": "S-H3-1 '>= 60 days of mark history': first mark price at least 60 days before t and >= 90 % "
            "of the last 1440 hourly returns present (the same 90 % presence the covariance needs).",
    "D-R2": "S-H3-6 covariance rows: rows complete across the asset set; fewer than 50 % complete rows of "
            "the 1440, or an asset under 90 % present, or no EWMA vol -> covariance unavailable -> "
            "the book is INFEASIBLE (S-H3-12).",
    "D-R3": "S-H3-10 intraday checks use the risk model (Sigma, beta) of the latest 00:00 build; the mids "
            "and quantities are the realised ones at the 15-minute tick.",
    "D-Q1": "np.quantile default (linear interpolation) for the 1 % / 99 % winsor cut-offs (H2a, H2b).",
    "D-B1": "S-H3-7 post-rounding q check and beta check have no tolerance beyond the spec text "
            "(only the dollar check has the step-notional allowance); literal.",
    "D-B2": "S-H3-5 long candidates are tried from the lowest forecast upwards (the best long first); "
            "short candidates from the highest downwards; ties by asset name.",
    "D-B3": "S-H3-5 free slots = k' - kept legs on that side; when k' fell below the kept count no "
            "entry happens and the kept legs stay (S-H3-4 decides each kept leg).",
    "D-E1": "Risk profile: the spec names R3 (daily loss limit 2 %: no entries or growth until the next "
            "00:00) and R4 (drawdown 15 % from the running peak of the 15-minute equity: exit all, no "
            "re-entry for the rest of the run); the V5 position-count/size rules (R5-R8) are superseded "
            "by the H3/H1 construction limits and are not applied.",
    "D-E1b": "Consequence of D-E1 (not a choice): the R3 day resets at 00:00 before the daily decision, "
             "so R3 can only block intraday entries/growth, of which H3 and H1 have none.",
    "D-E3": "A window end that is not on the 15-minute grid (holdout end 11:52:48) is floored to the grid "
            "(11:45); the terminal unwind is valued there.",
    "D-E4": "S-H3-12 '> 24 h': checked at every 15-minute tick; a book infeasible since a 00:00 decision is "
            "exited at 00:15 one day later unless the 00:00 build that day succeeds.",
    "D-E5": "Leg-stop reference = the position's average entry mid (fill-time mid, weighted by opening "
            "quantity; reductions keep it).",
    "D-E6": "An exit or stop order without a fill price (no mid at t + 15m) is retried at the next tick; an "
            "open without a fill is dropped (counted as unfilled).",
    "D-E2": "V5 ineligibility exit (S-H3-10): a held asset that is ineligible for a holding reason "
            "(no mid, decision-window spread > 40 bps, 24 h volume < 1M USD, funding coverage < 90 %, "
            "newest funding older than 3 h) at two consecutive 15-minute ticks (>= 900 s) is exited; "
            "values from the V5 config at fabd419 (exit_spread_bps 40, ineligible_exit_grace_s 900).",
}
