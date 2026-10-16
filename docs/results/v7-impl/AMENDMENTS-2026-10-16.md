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

| Item | Status | Code / record |
|---|---|---|
| A-1 | implemented (Task 29d) | Rule 1: `luna/v7/data/notices.py` (`InstanceBases`, `base_match_reason`, `match_reason(..., bases)`, `NoticeVersion.base_matched`), `luna/v7/metaingest.py` (`build_notices(..., identities=...)`, R1 identities from `ingest_metadata`), `luna/v7/metastore.py` (carried match rebuilt from R11). Rule 2: `luna/v7/data/captures.py` `TOKEN_SWAP_PHRASES` = ("token swap", "rebranding"), reason `token_swap:<phrase>`. `scheduled_ts`, versioning, U6 unchanged. Record: AMB-42 RESOLVED by A-1 (list, rule, readings). C4 re-run (`c4-notice-validation.md`, at `183ccee`, C4 proxy identities): Stage-0 terminations matched with `scheduled_ts` / null / missed 6 (+1 not the termination) / 2 / 65 -> 7 (+1) / 14 / 52; W_DEV book-window B / C cessations with a notice known before cessation 2 of 16 -> 5 of 16 (red-team r2 basis; 3 -> 6 counting XEM through the AMB-42 (3) mixed notice). **Flagged, user decision: AMB-46** (literal rule 1 also matches BTC, ETH, BNB, USDC, NOT named as pairs / quotes in notices with a USDⓈ-M context: U6 active without a scheduled time). |
| A-2 | implemented (Task 29d) | `luna/v7/leakage.py` `L9_TESTS` = 6, `L9_Z` = `norm.ppf(1 − 0.05/12)` ≈ 2.6383; `FROZEN.l9_fwer_alpha` = 0.05 replaces `l9_se_mult`. Record: AMB-33 (a) note. |
| A-3 | implemented (Task 29d) | `luna/v7/book/engine.py` (`EngineInputs.tick_evaluable`, forward schedule only), `luna/v7/book/risk.py` (`skip_tick`), `luna/v7/forward_replay.py`. Record: AMB-43 (d) RESOLVED by A-3. |
| R57 (controller) | implemented (Task 29d) | `luna/v7/forward_model.py` (`bridge_decisions`, `BRIDGE_WINDOW`, bridge coverage in `bound_samples`), `luna/v7/runner.py` (`_holdout_labelled`). Record: AMB-43 (a) RESOLVED by R57. |
| A-6 | implemented (Task 29e) | `luna/v7/data/klines.py` `union_a6`; `luna/v7/ingest.py` (`ingest_bars`: `conflict` record, chunk minus the conflicting keys, `a6_record` = ledger status `conflict_resolved_A6` with keys, both value sets, source file sha256s, `rawstore.RULE_A6`; the same path for W_HOLD); `luna/v7/rawstore.py` (R55: a DEC-33 conflict resolved only by an A-6 record of the chunk held now; S-2.6 funding conflicts still block; `a6_resolutions(lo, hi)`); `luna/v7/runrecord.py` (`run_record(a6_resolutions=)`, required for raw-store manifests). Record: AMB-45 (A-6 supersedes "never auto-resolved"). The real BTCUSDT 2023-11 conflict in the dry-run store: `python scripts/v7/dryrun_ingest.py rechunk --table R3 --symbol BTCUSDT --month 2023-11` (controller). |

## A-5 (NORMATIVE REFINEMENT of A-1, user decision 2026-10-16) — AMB-46: same-sentence context for the base-asset match

The C4 re-run after A-1 showed that the base-asset rule, applied literally, also matched BTC, BNB, ETH, USDC and NOT. Delisting and token-swap notices mention these as trading pairs or examples, while a USDⓈ-M phrase appears elsewhere in the notice. None of these assets terminated, and with no scheduled time the U6 flag would never clear.

User decision: A-1 rule 1 applies only when the base-asset token and a USDⓈ-M context phrase are in the SAME sentence. Sentences are split as in DEC-37 / `_SENT_RE`, on R51 text. Everything else in A-1 is unchanged. C4 is re-run, and the gains and losses are reported before freeze.

## A-6 (NORMATIVE CLARIFICATION, user decision 2026-10-16) — resolving DEC-33 monthly/daily conflicts

The first real conflict appeared in the C6 dry run: R3 BTCUSDT 15m, 2023-11-10 from 15:15 UTC, 5 bars where the monthly and daily files disagree on `open`. Under DEC-33 and R55, such a conflict blocks every run that includes BTCUSDT.

User decision:
- The conflicting keys are removed and treated as MISSING bars. Neither value is chosen.
- The removal is recorded explicitly in the ingest ledger and in DT4 as "resolved by A-6", together with the keys and both value sets.
- The existing missing-data rules then apply: the fallback chain, the S-5.4 data-gap rule, and INVALID where a required price is missing.
- This applies to every DEC-33 conflict, in W_DEV and in W_HOLD.
- R55 still blocks a conflict that has not been resolved this way.
