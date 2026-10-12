# V6 exact spec: freeze record

| Field | Value |
|---|---|
| Freeze timestamp (UTC) | 2026-10-12T02:09:47Z |
| Authorisation | User message 2026-10-12: freeze without asking again if the final second red-team returns Critical 0 / Major 0 |
| Final second red-team | READY TO FREEZE — Critical 0 / Major 0 / Minor 0 (4 rounds, `docs/results/v6-redteam/v6-spec-second-redteam.md`) |
| Spec commit | `86234bf` (branch `feat/v6-profitability-research`) |
| Tag | `v6-profitability-spec-frozen` (annotated, on the commit that adds this record) |

## SHA256 of committed content at `86234bf`

| File | sha256 |
|---|---|
| `docs/superpowers/specs/luna-v6-profitability-spec.md` (exact spec) | `c659d0cd6928c6356be43cbe97eaa91075197640ef195d09aba1ac5d582b2c5f` |
| `docs/superpowers/specs/luna-v6-frozen-constants.json` (frozen constants) | `152af4cb19e688b3551feb8210aeec5c471aec78b708592fcbb6eeef08946974` |
| `docs/results/v6-fee-source/kraken-fee-schedule-2026-10-12.html` (fee source) | `118e79185c1794f47593fbbf2a43898a69338dce90cc593f8a7b03d61e620ed8` |
| `docs/results/v6-fee-source/excerpt.txt` | `f76eea365ba48bc409aa332b6de4582c138a5321107992a5b1c5839dba05d602` |
| `docs/results/v6-redteam/v6-spec-second-redteam.md` (red-team report) | `14bbe0155bb64561d720d69d5ba731f6b0b0c8cd415c8d5fe77464190466c0be` |
| `docs/superpowers/specs/luna-v6-profitability-research-plan.md` (plan rev 2) | `73d148587c9194438dd4e3861f2f1779297f93b4a5461a4e33ac27bb655634a9` |
| `docs/results/v6-feasibility/h3_sizing_grid.json` | `625218c0d73c4127cac0ecdbab10ec1be11028b54ef5b6ec62ded5afc66f520b` |
| `docs/results/v6-feasibility/h3_sizing_tails.json` | `a4fd52d5e97d5aeafdd7c8342825c19330621b0916d91f4a1fb944b81ee5cafc` |

The fee source, the red-team report and the V5 results folders are marked `-text` in `.gitattributes`, so they are stored byte-identical.

## Fee source

- **URL and access time:** https://www.kraken.com/features/fee-schedule, accessed 2026-10-12T01:12:31Z.
- **Spot Crypto Tier 1:** maker 0.40 %, taker 0.80 %.
- **Futures Tier 1:** maker 0.02 %, taker 0.05 %.
- **Not primary:** the account-specific tier has not been verified, so these public Tier 1 rates are the conservative primary. The 0.38 % spot-rebate table is not used.

## State at freeze

- **No V6 outcome.** No V6 backtest, holdout or forward evaluation has been run.
- **Development data used only for:**
  - the V5 postmortem diagnostics (V5 single-run tables);
  - the outcome-blind H3 sizing feasibility check (risk inputs before 2026-07-01, no P&L).
- **Holdout [2026-07-01, 2026-10-09T11:52:48Z) is sealed:**
  - no V6 code or loader exists yet;
  - no holdout data was downloaded or read, and there is no holdout token or marker;
  - the frozen V5 inputs end before 2026-07-01;
  - opening the holdout requires the user's separate explicit approval (S-GUARD-1).
- **Forward evidence** starts at T_F, the first 00:00 UTC at least 24 h after the independent implementation-review sign-off. The `v6-code-frozen` tag is made then (D7).
- **V5 unchanged:** tag `v5-backtest-protocol-frozen`, results commit `fabd419`. Paper config unchanged; `live_enabled=false`.

## Erratum 001 (2026-10-12)

The frozen spec and constants are unchanged, and the `v6-profitability-spec-frozen` tag was not moved. The erratum records how `common.risk_profile` ("V5 carry.risk unchanged") is read for H3 and H1, rule by rule. It was needed because V5 R7 `max_positions 4` contradicts the frozen H3 leg count (at least 6 legs).

| Item | Value |
|---|---|
| File | `docs/results/v6-spec-erratum-001.md` (stored `-text`, byte-identical) |
| sha256 | `ca93982b6304096fe81071597a4d31988d2f63e90b96f7432094ab9f90dec13c` |
| Commit | `3de40ab3c9cf166f3f0726f6c6dd185195506ba5` (contains only the erratum and its `.gitattributes` line) |
| Annotated tag | `v6-profitability-spec-erratum-001` → tag object `4c8d4c4590b296d4e3c0f18270c9457edb80bc0e` |
| Implementation | `luna/v6/v5risk.py`, `tests/v6/test_v5risk.py` (commit `69bc6ca`) |
| Decision | Option A, narrow formal erratum (user, 2026-10-12) |
| Status | **Pending.** It takes effect only when an independent methodology reviewer states "INTERPRETATION FIX, NOT OUTCOME TUNING". There is no code freeze before that. |

### Erratum 001: independent methodology review (2026-10-12)

- **Statement on the erratum text** (methodology reviewer, re-review of exact HEAD `84e5ed6`):
  "INTERPRETATION FIX, NOT OUTCOME TUNING".
- **Condition:** the code must implement the erratum as written. The reviewer's E1 (R5–R9 apply only
  to opening orders; R6 measured at entry price as V5 `risk_amount`) is fixed in the next commit.
- **Reviewer's note on R12** (recorded here because the erratum file is immutable): V5's own
  historical replay at fabd419 used maintenance margin 0 from the same instrument file
  (`carry/backtest.py`). Under the V5 formula, R12 then needs leg ≤ 0.667 E, which R9 (0.30 E)
  already guarantees. With live margins, R12 cannot bind unless mm > 0.35, given R8/R9. "Not
  applicable" is accepted.
- **Still needs the user's sign-off** (the reviewer flagged both):
  - D-A1: Stage 0 coverage below 90 % in any asset or asset × hour-of-day cell = FAIL (INVALID RUN).
  - D-B5: spec-literal rounding at an exactly-binding cap or beta limit → INFEASIBLE.

### Erratum 001: final status (2026-10-12) — IN EFFECT

- **Pending condition closed:** the methodology reviewer stated **"INTERPRETATION FIX, NOT OUTCOME TUNING"** unconditionally on the final exact HEAD `576f5fac4b01de45f6b4a6c7cb7b380c881df5ab`.
- **The code implements the erratum exactly.**
  - R5–R9 and R14 apply only to opening orders.
  - **R6 is measured at the average entry FILL price**, the V5 `risk_amount` (`carry/core.py:691`, `portfolio._trade` on `fill.price` at fabd419).
    - The fill-time mid is only the stop/reset reference (D-E5, D-E12).
    - The earlier "fill-time mid, negligible" wording is withdrawn.
- **Final reviews on `576f5fa`:**
  - code review PASS (Critical 0, Major 0, Minor 3);
  - methodology review CONFORMS (Critical 0, Major 0, Minor 3).
- **User decisions, 2026-10-12:**
  - D-A1 ACCEPTED (strict Stage 0 coverage FAIL);
  - D-B5 ACKNOWLEDGED AND ACCEPTED;
  - remote push approved.
- **Review PASS timestamp (pinned):** `2026-10-12T12:30:03Z`.
- **T_F:** `2026-10-14T00:00:00Z`.
- **Record:** `docs/results/v6-code-freeze-record.json`.
