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
    "D-E1": "SUPERSEDED by docs/results/v6-spec-erratum-001.md (tag v6-profitability-spec-erratum-001): the V5 "
            "carry.risk profile is applied rule by rule - R3/R4 retained; R5, R6, R8, R9, R14 retained with the V5 "
            "values (H3: book-level, a breach makes the decision INFEASIBLE, no partial book; H1: per-entry veto); R7 "
            "replaced for H3 by the frozen leg-count construction (k, min_k_prime), retained for H1 (pair = one id); "
            "R10 replaced for H3 by the S-H3-9 band; R12 not evaluable from the frozen inputs (no maintenance margin); "
            "R15/R16/R18 replaced by the frozen execution, S-DATA-5 and exact accounting; the rest retained.",
    "D-E1b": "Consequence of R3 (not a choice): the R3 day resets at 00:00 before the daily decision, so R3 can "
             "only block a 00:00 decision deferred within 6 h (D-X4) after an intraday loss; repairs are reduce-only.",
    "D-E3": "A window end that is not on the 15-minute grid (holdout end 11:52:48) is floored to the grid "
            "(11:45); the terminal unwind is valued there.",
    "D-E4": "S-H3-12 '> 24 h': checked at every 15-minute tick; a book infeasible since a 00:00 decision is "
            "exited at 00:15 one day later unless the 00:00 build that day succeeds.",
    "D-E5": "Leg-stop reference = the position's average entry mid (fill-time mid, weighted by opening "
            "quantity; reductions keep it).",
    "D-E6": "An exit or stop order without a fill price (no mid at t + 15m) is retried at the next tick; an "
            "open without a fill is dropped (counted as unfilled).",
    "D-V1": "When several classes of the same severity (MODEL INCONCLUSIVE, INSUFFICIENT, INCONCLUSIVE) fire, "
            "the displayed class is the first in that order; every reason is reported.",
    "D-V2": "P8 and P9 'not evaluable' raise INCONCLUSIVE only when P1 passed (P8 is evaluated only after P1; "
            "with P1 failed the config is NO-GO anyway).",
    "D-S1": "S-COST2X, S-SPOTSPREAD2X, S-MIXED and S-MAKER-BOUND change realised costs only (fills, terminal "
            "unwind); decisions keep the PRIMARY cost estimates (as V5 S-COST2X: the strategy does not know "
            "its costs are mis-estimated). S-PIT10 and S-FB-CONS change the information (spread known time, "
            "fallback value) and therefore the decisions.",
    "D-X1": "Forward taker fill (H3, risk exits, spot taker fallback for exits): if the first book snapshot "
            "after the order does not show enough depth, the remainder is filled on the following snapshots "
            "(same walk); no price is invented. For the H1 entry taker fallback the remainder is cancelled "
            "(spec S-H1-X).",
    "D-X2": "Forward terminal / hypothetical unwind on the latest book received by the end: a remainder beyond "
            "the visible depth is valued at the worst visible level (valuation only, no order).",
    "D-X3": "Forward funding known_at = receive time of the first collector round carrying the period; a later "
            "revision of the same period is ignored; missing periods come from the official API (S-DATA-1).",
    "D-R5": "D9 V5 W/E holdout reference: the V5 code and config run unchanged; only the V5 modules' cut-off "
            "constant is moved to the holdout end inside a context manager, only with the V6 holdout token, and "
            "restored afterwards (V5 files unmodified, tested). V5 run_arm needs a UTC-midnight end, so the "
            "reference covers [2026-07-01, 2026-10-09 00:00). Verdict-free.",
    "D-R6": "An entry selection that leaves fewer than 3 legs on a side is INFEASIBLE before the covariance is "
            "computed (the build would refuse it anyway); covariance needs >= 2 assets and non-zero variance.",
    "D-E2": "V5 ineligibility exit (S-H3-10): a held asset that is ineligible for a holding reason "
            "(no mid, decision-window spread > 40 bps, 24 h volume < 1M USD, funding coverage < 90 %, "
            "newest funding older than 3 h) at two consecutive 15-minute ticks (>= 900 s) is exited; "
            "values from the V5 config at fabd419 (exit_spread_bps 40, ineligible_exit_grace_s 900).",
    "D-E7": "Pending orders: a decision sizes from the position after the orders already sent (pending fills "
            "included); a leg with an exit in flight is neither resized nor re-entered until it is flat.",
    "D-E8": "S-H3-10 repair runs after any booked intraday exit (stop, ineligibility, missing forecast); after "
            "a kill the strategy stops but the 00:00 H2a gate records continue (the gate sample is PIT and "
            "does not depend on positions).",
    "D-E9": "Terminal unwind without a mid at the end: valued at the last known mid with the taker fee, the "
            "10 bps fallback spread (spot: max(10 bps, spot rule)) and the slip.",
    "D-E10": "invariant_mode (blind development runs only): R3, R4, leg/combined stops and hedge resets are off "
             "so that no reported mechanical invariant depends on performance; never used for an evaluation.",
    "D-X4": "S-DATA-5: a quarter is fresh when any asset's fut ticker snapshot is <= 180 s old (the collector "
            "polls all tickers in one request); a stale quarter is a no-op for risk and decisions; the 00:00 "
            "decision is made at the first fresh quarter within 6 h, else the day's decision is skipped.",
    "D-X5": "S-H1-X risk exits: both legs taker from the first snapshot after t, independently (neither waits for "
            "the other); 00:00 economic orders: maker-first spot, then the perp for the filled share on the "
            "first fut book after the spot leg completed.",
    "D-X6": "S-H1-X t_sub = max(t, receive time of the reference ticker); the reference is the latest ticker "
            "<= 180 s old at t, else the first one within 6 h (else skipped). A depth snapshot is used only if "
            "received at or before the ticker that triggers the fill; each level's volume is used once.",
    "D-X7": "Forward decision cost of H3/H1 = the same formula as historical (half spread + fee + 2 bps slip), "
            "with the half spread from the latest fresh ticker.",
    "D-X8": "Forward inputs: decision funding = history up to the holdout end, collector rounds from the holdout "
            "end on (no hole before T_F); evidence (fills, P&L) only from snapshots received at or after T_F.",
    "D-A1": "S-GUARD-1 'V5 Stage 0 audit; failure = INVALID RUN' on the holdout spread download: FAIL = labels off "
            "the 5-minute grid, duplicated or unordered; labels outside the window; observed cadence != 300 s; "
            "decision-window (>= 6/12) or fill-window (>= 2/3) point coverage < 90 % for any asset or any asset x "
            "hour-of-day cell. V5 flagged coverage for the user; V6 maps it to FAIL (the spec's failure rule). "
            "Repeats, gaps and invalid values are reported, not FAIL (V5).",
    "D-G1": "Holdout markers keyed by the canonical run identity RUN_ID = v6-<spec sha16>, independent of the checkout "
            "path: ~/.luna/v6_holdout/<RUN_ID>/ and <git common dir>/luna-v6/<RUN_ID>/ (every worktree of the repo); "
            "written exclusively, all-or-nothing; 'opened' = both present with approved = true and this spec's sha256. "
            "A different clone on another machine has neither marker: forward runs there are refused (fail closed).",
    "D-G2": "T_F = the first 00:00 UTC at least 24 h after the independent review PASS; the freeze record holds "
            "both times and is rejected when T_F does not equal that derivation.",
    "D-G3": "Gate bootstrap: a replicate whose ratio is undefined (zero denominator) is dropped from the "
            "percentile (nanquantile); the number dropped is reported.",
    "D-B4": "An asset is never moved from one side to the other in the same decision: a held leg whose side "
            "flips is exited at 00:00 and may enter the other side at the next decision.",
    "D-G4": "The holdout download is pinned at the authorised opening (sha256 of its manifest.json, written exclusively "
            "next to both markers). Forward looks read the holdout-window history only through load_holdout_inputs: "
            "forward-guarded (T_F + opening), pinned manifest required, every file hash verified, read only (no "
            "download, repair or write), no authorization token needed. A look whose model history does not reach the "
            "holdout end is refused (INVALID LOOK), never run on a silent warm-up.",
    "D-A2": "The Stage 0 audit also covers every other required input family (mark, trade, index, spot bars, funding): "
            "an empty download or one that starts/ends more than 24 h inside the window is a truncated download = FAIL; "
            "present shares are reported. Provenance = the pinned manifest and per-file sha256.",
    "D-S2": "S-DATA-1: unknown funding for a held period is an INVALID RUN only in forward looks (after the official API "
            "fallback); historically it is counted and settled as 0 (as V5).",
    "D-X9": "Forward S-PIT10: every decision input (tickers, spreads, volumes, stops, eligibility, funding visibility and "
            "the H2a models) uses the view at t - 5 min; equity marks, fill reference mids and unwind valuation use the "
            "snapshot at t (signal-known time and valuation time are separate). R3/R4 compare the marked equity.",
    "D-X10": "Historical perp mid = the mark bar; if the mark bar is missing the trade bar of the same quarter is used "
             "(valuation and decisions); no mid -> no decision for that asset, the last mid values a held leg.",
    "D-H1": "H1 hedge integrity: a pair whose legs differ by more than one quantity step (one leg filled, the other not) "
            "is reconciled at the next fresh tick by closing both legs, each by its own quantity (intent 'reconcile'); "
            "a one-legged remainder is closed through its own leg only, never through a zero-quantity pair.",
    "D-E11": "MODEL WARMUP does not start the S-H3-12 infeasible clock (there is no forecast to build from); held legs "
             "without a forecast exit after 24 h by the missing-forecast rule, which gives the same 24 h bound.",
    "D-B5": "Observation, spec-literal (D-B1): when the 0.2 net-dollar cap or the beta limit binds exactly, rounding "
            "toward zero can move the book beyond the post-rounding check (one max-step-notional allowance for dollars, "
            "none for beta) and the book is INFEASIBLE. Not changed (frozen).",
    "D-P1": "P8/P9 entry clusters are built from every opening from flat (per asset, both H1 legs together), "
            "not from merged episodes.",
}
