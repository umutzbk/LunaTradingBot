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

**Verdict: READY TO FREEZE.** Critical = 0, Major = 0. All 19 checklist items PASS:

1. proxy produces no funding P&L
2. no executable hypothesis beyond the 7 configs
3. H3 is diagnostic only
4. H5 canonical rule is fixed
5. no price-return hypothesis
6. asset eligibility is general and outcome-free
7. DQ thresholds are outcome-free
8. H1 accounting is correct
9. H2 P&L decomposition is correct
10. funding timestamp, sign and unit are correct
11. basis accounting and sign are correct
12. no look-ahead
13. holdout is unread
14. no forward outcome peeking
15. Holm correction is frozen
16. bootstrap rules are frozen
17. evidence start is frozen
18. Critical = 0
19. Major = 0

Both optional minors were applied before the tag:
- H5 DQ validity uses H1 eligibility.
- This round-2 record is now filled in.
