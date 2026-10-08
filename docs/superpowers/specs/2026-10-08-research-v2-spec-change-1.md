# Research V2: spec-change record SC-1 (cross-sectional order-level limits)

- **Amends:** `docs/superpowers/specs/2026-10-06-luna-research-v2-design.md` §4.2 and §4.3 only. Nothing else in the frozen spec changes.
- **Approved:** 2026-10-08 by the user, who replied "devam" after the implementation plan presented SC-1 as the recommended option (`docs/superpowers/plans/2026-10-08-luna-research-v2.md`, "Spec-change record required before Task 5").
- **Made before any V2 result existed.** No V2 code had run on market data.

## Problem

§4.2 keeps the V1 risk engine (R1–R19) "aynen" at order level for the cross-sectional book. With V1's values that book cannot trade at all:

| V1 rule (value) | Conflict |
|---|---|
| R5: risk per trade ≤ 1% | By §4.2's own definition, a leg's planned risk is the 1.5% catastrophic stop plus costs, so every leg is vetoed. |
| R6: open risk ≤ 3% | Allows at most 2 legs. |
| R7: at most 3 positions | Blocks a book of 2k ≥ 4 legs. |
| R9: spot leg notional ≤ 50% | Conflicts with the 40% spot asset cap in §4.3. |
| R10: pyramiding | Treats every rebalance resize of a held leg as pyramiding. |
| R11: every open needs a stop | §4.3 (spot top-k) defines no stop. |

## Change

1. **Cross-sectional orders only (perp and spot):**
   - R5, R6, R7 and R9 are set non-binding.
   - A rebalance resize is not flagged as pyramiding (`increases_abs_position = False`).
   - The §4.2/§4.3 portfolio controls replace them: gross, net, asset cap, correlation concentration, VaR budget and vol targeting. The weight projection enforces these before any order exists.
2. **All other rules keep their V1 values:** R0–R4, R8 at the V1 gross cap, R10 close validity, R11–R19.
3. **Spot top-k legs** use the same 1.5% per-asset catastrophic stop as §4.2, so R11 has a stop to check.
4. **TSMOM and breakout are unaffected:** the full V1 R1–R19 with V1 values still apply.

If this record had been rejected, the cross-sectional family would have been reported as "not executable under V1 limits".
