# V7 post-freeze amendments and clarifications — round 3 (2026-10-16)

| | |
|---|---|
| Frozen spec | `docs/superpowers/specs/2026-10-14-luna-v7-ml-research-design.md`, tag `v7-ml-spec-frozen` → `29959fc`. The tag and the spec file are unchanged. This document is the explicit post-freeze record the user required for any normative change. |
| Decided by | the user, 2026-10-16, after implementation red-team round 2 (`implementation-redteam-r2.md`) and C4 (`c4-notice-validation.md`) |
| Status | binding for `v7-code-frozen` |

## A-1 (NORMATIVE AMENDMENT) — DEC-40 / S-2.5 confident match: base-asset notices and token-swap notices

**Reason.** C4 and red-team r2 found that inside the W_DEV book test windows only 2 of 16 B/C cessations are protected by U6. Notices often name only the base asset (e.g. "Will Delist AKRO and DODO USDT-Margined Contracts") or announce a token swap or rebrand. Under the literal DEC-40, a W_DEV `INVALID RUN — historical terminal execution not reconstructible` is more likely than not.

**Amendment.** In addition to the frozen DEC-40 confident-match rule, which stays in force, a notice version is also a confident match for an instance when both of the following hold.

1. **Base-asset match.**
   - The instance's verified identity `baseAsset` (R1, E8 whitelist; for multiplier contracts the base including its prefix, e.g. `1000BTTC`) appears as a whole token (the same token boundaries as DEC-40) in the notice's title or body.
   - The notice has the same unambiguous USDⓈ-M context that DEC-40 already requires (the existing closed context phrases).
   - The COIN-M veto still applies.
   - A base token that maps to more than one live USD-M instance with quote USDT at the version's `known_at` is ambiguous, and gives no match.
2. **Token-swap / rebrand notices** are delisting-kind evidence.
   - They are identified by a closed phrase list that is derived from, and validated on, the C4 Stage-0 evidence (e.g. token swap, rebranding, migration notices for COCOS, MATIC, RNDR, STRAX, BNX, TOMO, COMBO).
   - Matching of the symbol or base still follows rule 1 or DEC-40.

`scheduled_ts` mapping, versioning, U6 activation and deactivation, and the 72 h clear are unchanged (DEC-36 / DEC-37, DK-06).

Validation: C4 is re-run on the Stage-0 set, outcome-free. The before → after U6 coverage of W_DEV terminations is reported before freeze.

## A-2 (CLARIFICATION, user-approved) — L9a multiple-testing threshold

- L9a runs 6 simultaneous tests (2 regressor families × 3 horizons).
- The canary fails if any test has |IC| > z · SE, where z = Φ⁻¹(1 − α/2) with α = 0.05 / 6 (two-sided Bonferroni, family-wise error rate 5 %), approximately 2.638.
- The code computes z from the exact normal quantile and does not hard-code it.
- Unchanged: the frozen seed, all 6 tests, deterministic reproducibility. There is no reroll and no new seed on restart.
- This replaces the per-test 2 SE threshold of DK-27(a).

## A-3 (CLARIFICATION, user-approved) — AMB-43(d), forward stage only

In the §15 forward replay, a 15-minute tick inside a non-evaluable (stale) quarter is skipped for risk and marking: no mark is taken, no risk action is taken, and no INVALID results. §15's coverage rules (INCONCLUSIVE below 144 evaluable days) handle such gaps. W_DEV and W_HOLD keep AMB-29(a) / DK-70: a missing required mark is INVALID.

## A-4 (CLARIFICATION, user-approved) — AMB-41

Kraken comparison #5 stays UNAVAILABLE and is labelled "exploratory (previously observed window)". The S-13.3 Holm family stays m = 6, with #5 counted as a non-rejection (DK-83). There is no Kraken rule amendment.

## Controller rulings recorded with this round (implementation clarifications)

- **R55 — ingest-completeness gate.** Unresolved ingest gaps and DEC-33 monthly/daily conflicts block every research entry point.
- **R56 — DEC-19 "pinned manifest".** It is the canonical JSON of the W_HOLD N1 file listings (key, size, published checksum). The opening requires its content-verified sha256, and post-opening W_HOLD ingestion must match it.

## Implementation status

Filled in as items land.
