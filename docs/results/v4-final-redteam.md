# V4 final pre-freeze red-team (2026-10-09)

**Reviewer:** an independent agent that did not write the spec. Read-only, with no `var/` reads on or after 2026-07-01 and no return computation.

## Round 1 verdict: NOT READY (Critical 0, Major 8)

All eight Majors were text-only fixes. They were applied in this commit:

| id | fix |
|---|---|
| F1 | Mark price is not used in funding cash flow; T3 is restated with the abs rate |
| F2 | §8 signal rule changed to `T + 1h ≤ t` |
| F3 | Forward funding uses rows with period start `T ≥ collector_start_ts` in R1, §8 and the collector spec |
| F4 | §2a universe described as a static data-integrity filter (known anachronism); §8's rule scoped to dynamic eligibility |
| F5 | `basis_contribution` and `residual_delta_pnl` defined; T2 expected +0.44554 / +0.09901 |
| F6 | H2 concentration diagnostics added (max share, Herfindahl, per-asset P&L) |
| F7 | DQ validity unit defined: pair, asset and config levels; M2 per config |
| F8 | Collector spec now states `V4_EVIDENCE_START_TS = 2026-11-10T00:00:00Z` (not the tag time) |

Minors m1–m12 were applied too:
- forward set at config level
- Holm family per look
- bootstrap mechanics
- perp spread window [t−1h, t)
- long-leg liquidation uses mark low
- `openingDate`
- Sharpe √365 and ES estimator
- B30med sampling
- forward diagnostics only at look dates
- the "(eski H3)" label
- funding-semantics wording
- superseded header on the earlier review

The T1–T5 arithmetic was verified independently.

## Round 2 (re-check of the fixes)

The round-2 result is appended below.
