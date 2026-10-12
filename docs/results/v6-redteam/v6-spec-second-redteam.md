# V6 exact spec: second independent methodological red-team

**Reviewer:** A fresh-context reviewer that did not write the plan or the spec. Read only: it edited no files, ran nothing, and opened no data dated on or after 2026-07-01. It read the committed outcome-blind feasibility outputs.

**Spec under review:** `docs/superpowers/specs/luna-v6-profitability-spec.md` with `luna-v6-frozen-constants.json`.

## Round 1 (draft, commit cd33ecb) — NOT READY: Critical 1 / Major 10 / Minor 12

| ID | Finding | Resolution |
|---|---|---|
| C1 | The H1 NO-GO-PRIMARY → FEAS path could not be reached or was ambiguous (P7 and P4 always fail when P1 ≤ 0) | FEAS defined explicitly in S-H1-F. P4/P7/P8/P9/P11/P2 do not exclude; S-MAKER-BOUND must pass P5 and P6 |
| M1 | Reviewer's hand estimate said H3 is mechanically near-infeasible at 265 USD. The drop/redo rule was undefined | Drop rule defined. Outcome-blind check (pre-2026-07-01 risk inputs, no P&L): k=4 feasible on 90.4% of days, k=6 on 100%, median gross 0.455/0.594. User kept 265 USD (D8). Infeasibility is not a bug |
| M2 | The long/short vol imbalance was not controlled; D1's justification was wrong | D1 corrected with measured values (worst standalone ratio 1.28 / 1.17). User added a hard ratio rule ≤ 1.5 |
| M3 | Limits were not enforced between daily rebalances | S-H3-10 intraday post-exit check (completed in rounds 2–3) |
| M4 | Early-success handling was inconsistent; m_F was chosen from holdout results | p = 1 for CLOSED and FINAL GO; forward family fixed to all 4 configs (m_F = 4), non-candidates p = 1 |
| M5 | The "exact 0.05" claim needed qualification; MBB tail p-values at tiny thresholds | Claim qualified ("given valid p-values"); B = 200000 forward; Newey–West add-on at interim looks |
| M6 | Maker-fill simulator biased upward (volume reuse, marketable orders, stale reference, risk exits) | Volume consumed once; marketable → taker; reference ≤ 180 s; risk exits are taker |
| M7 | H2 gate sample definitions were ambiguous | k'(s), slope sample, missing-label rule per side, 720 h evidence formula, paired bootstrap, H1 eligibility |
| M8 | The forward run needs holdout-window inputs; funding gaps; recv-time rule | Inputs only after the single holdout opening; official API for missing funding; known = recv_ts ≤ t |
| M9 | Forward contamination during implementation | User decision: T_F = code-freeze tag (D7) |
| M10 | PRIMARY, P7 and P11 undefined in forward windows | Forward PRIMARY, P7 and S-PIT10 defined; S-FB-CONS historical only |
| m1–m11, m8b | Constants, D2 sign-off, H1 sizing, M-b formula, P8d, P9 split, terminal unwind, risk-model details, band repair, references, timing, H1 exit note | All folded in; D2 = user decision 0.048 |

## Round 2 (r2, commit ee1d9ad) — NOT READY: Critical 0 / Major 1 / Minor 5

- **Earlier findings:** C1, M1, M2 and M4–M10 FIXED. M3 PARTIALLY FIXED.
- **User decisions:** consistent between the spec text and the JSON.

| ID | Finding | Resolution |
|---|---|---|
| N1 (Major) | The intraday scale-down could not fix the net-dollar and q checks; the INFEASIBLE state's interaction with the 00:00 build was unclear | S-H3-10 rewritten (β scale → heavier-side shrink → β again → exit all); 00:00 build always runs and clears INFEASIBLE; q added to post-rounding checks |
| N2 | The ratio rule is in effect an INFEASIBLE trigger | Note added; 1e-12 tolerance |
| N3 | Marketable-order and stale-reference details | `t_sub`; walk only levels ≤ limit; remainder rests as maker |
| N4 | Newey–West not fully defined | Bartlett, lag 28, one-sided, N(0,1) |
| N5 | Code-tag timing was discretionary | T_F = first 00:00 UTC ≥ 24 h after the implementation-review sign-off |
| N6 | CAND wording; S-STAT-F branches | CAND excludes INVALID RUN; if/elif chain |

## Round 3 (r3, commit b217052) — READY TO FREEZE: Critical 0 / Major 0 / Minor 2

- **Earlier findings:** N1–N6 FIXED. The revised S-H3-10 is reduce-only, adds no free parameter, and does not disturb the 24 h clock.

| ID | Finding | Resolution |
|---|---|---|
| N7 | S-H3-10 step 2 edge cases: conflicting heavier sides; minimum-size exits not re-checked | `a_L/a_S` closest to 1 in the intersection of both intervals, applied by shrinking only; empty → exit all; sub-minimum exits after every step, at most #legs repeats |
| N8 | Stale `h1.forward_marketable_rule` key in the JSON | Removed |

## Round 4 (r4, commit 866d99b) — pre-freeze confirmation

- N7 and N8 FIXED.
- The whole JSON was scanned against the text: no contradictions.

**Verdict: READY TO FREEZE — Critical 0 / Major 0 / Minor 0.**

## Non-negotiables A–F (final check)

| | Item | Result |
|---|---|---|
| A | Fee provenance | URL, access time, Tier 1 values and snapshot hash verified |
| B | H3 net-beta limit, scale-down, INFEASIBLE, risk-contribution report | Holds |
| C | IUT + Holm + sequential looks (FWER ≤ 0.050 given valid p-values) | Holds |
| D | Day 1–90 vs day 91–365 isolation | Holds |
| E | H2 gate | Holds |
| F | Forward data quality | Holds |

Every Major fix was verified by the reviewer in a later round. No Major was closed only by adopting the reviewer's own suggested wording without a check.

## First red-team (plan rev 2), for completeness

| Round | Result | Status |
|---|---|---|
| 1 | Critical 0 / Major 11 / Minor 14 | All fixed |
| 2 | Major 1 / Minor 6 | All fixed |
| 3 | Major 1 / Minor 1 | Fixed with the reviewer's text |

Those plan-level fixes were re-examined by this second red-team on the exact spec. Detail is in plan rev 2, §11.
