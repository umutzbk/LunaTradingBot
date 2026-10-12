# V6 Spec Erratum 001: how the frozen V5 risk profile applies to H3 and H1

**Status:** Proposed. Option A was chosen by the user on 2026-10-12, as a narrow, formal erratum. It
takes effect only after an independent methodology reviewer states **INTERPRETATION FIX, NOT OUTCOME
TUNING**. Without that statement there is no code freeze.

**Scope:** this erratum changes nothing in the frozen spec or constants files and moves no tag. It
records how one frozen constant is to be read, because read literally it contradicts the frozen H3
construction.

## 1. Frozen artefacts (unchanged)

| Artefact | Identity |
|---|---|
| Spec tag | `v6-profitability-spec-frozen` (annotated tag object `1a491282f1590384a5208136d1639f2a70de65eb`, points at commit `9994cf9bc8d64d41333a35ec1022a5046778e80c`) |
| Spec `docs/superpowers/specs/luna-v6-profitability-spec.md` | sha256 `c659d0cd6928c6356be43cbe97eaa91075197640ef195d09aba1ac5d582b2c5f` |
| Constants `docs/superpowers/specs/luna-v6-frozen-constants.json` | sha256 `152af4cb19e688b3551feb8210aeec5c471aec78b708592fcbb6eeef08946974` |
| V5 risk profile referenced | `config/default.yaml` → `carry.risk` at commit `fabd419`, and the rule code `luna/risk/engine.py` at `fabd419` |

`git diff v6-profitability-spec-frozen -- <spec> <constants>` is empty: zero lines.

## 2. The contradiction

`common.risk_profile` in the constants reads **"V5 carry.risk unchanged (config/default.yaml at fabd419)"**.
That profile includes **R7 `max_positions: 4`**. In the V5 carry engine every perp leg has its own
position id, so R7 counts legs.

The frozen H3 construction needs at least **k' ≥ 3 legs on each side**, which is at least 6 legs:

- `h3.min_k_prime = 3`;
- the entry rule "fewer than 3 legs on a side → INFEASIBLE";
- `variants_k = [4, 6]` allows up to 12 legs.

Applied literally, R7 vetoes the 5th leg of every H3 book, so **no H3 book can ever exist**. This
follows from the constants alone; no data is involved.

The spec text itself names only R3/R4 and the V5 ineligibility exit (spec lines 287 and 330) and
defines its own sizing (S-H3-3 … S-H3-12, S-H1-2 … S-H1-4). The implementation's earlier declaration
D-E1 ("R5–R8 superseded") was a blanket reading. The conformance reviewer flagged it as contradicting
the constant (finding M8). This erratum replaces D-E1 with an exact, rule-by-rule mapping.

## 3. Chosen interpretation (principle)

1. **R3 (daily loss) and R4 (drawdown kill) are kept exactly as they are.**
2. A V5 rule is superseded **only where it directly contradicts** a frozen H3/H1 construction or risk
   rule, and **only to that extent**. The replacement is the frozen V6 rule it conflicts with.
3. **Non-conflicting V5 safety controls are kept** with their V5 values and their V5 way of measuring.
   - For H3 a breach makes the whole decision INFEASIBLE, because an H3 book is one atomic construction.
     V5 vetoes single orders, but dropping one leg would break the S-H3-7 beta, net-dollar and q
     invariants.
   - For H1, whose entries are independent pairs, a breach vetoes that entry, exactly as in V5.
4. This erratum does not exist to make trades happen. It resolves an internal contradiction of the
   frozen design. It makes the implementation *more* restrictive than before: R5, R6, R8, R9 and R14
   were not enforced before and now are.

## 4. Exact R3–R8 mapping

All V5 measurements come from `luna/risk/engine.py` and `luna/carry/core.py` at `fabd419`. An opening
order's risk is `carry.leg_stop_frac` (0.15) × order notional. E is account equity.

### R3 — daily loss limit

- **Exact V5 meaning (`carry.risk` at fabd419):** `daily_loss_limit` 0.02. If (day-start equity − equity) ≥ 0.02 × day-start equity, or the loss cannot be computed (fail closed), opens are vetoed for the rest of the UTC day (`BLOCK_ENTRIES_TODAY`). Closes and reduces are exempt.
- **Conflict with H3?** No.
- **Conflict with H1?** No.
- **Status:** **Retained** for both H3 and H1.
- **Frozen V6 rule / implementation:** R3 as in V5. No entries or growth until the next 00:00 (`engine.py` / `h1.py`, `r3_blocked`). This also applies to a 00:00 decision deferred within 6 h (D-X4).

### R4 — drawdown kill

- **Exact V5 meaning:** `max_drawdown` 0.15. If (peak − equity) ≥ 0.15 × peak: flatten all and kill (`FLATTEN_AND_KILL`); opens vetoed.
- **Conflict with H3?** No.
- **Conflict with H1?** No.
- **Status:** **Retained** for both H3 and H1.
- **Frozen V6 rule / implementation:**
  - identical to frozen `common.risk_max_drawdown_kill` 0.15;
  - peak is taken over the 15-minute equity;
  - exit all, no re-entry for the rest of the run;
  - feeds P10.

### R5 — risk per trade

- **Exact V5 meaning:** `max_risk_per_trade` 0.04. Every opening order needs 0.15 × notional ≤ 0.04 E, i.e. order notional ≤ 0.2667 E.
- **Conflict with H3?** No conflict in value. V5's per-order veto would conflict with the atomic S-H3-7 book, so the mechanism is adapted, not the value.
- **Conflict with H1?** No. Each leg is ≤ ⅙ E (S-H1-2), so the risk is 0.025 E ≤ 0.04 E.
- **Status:**
  - H3: **Retained**, checked at the book level.
  - H1: **Retained**, never binds given S-H1-2.
- **Frozen V6 rule / implementation:** `v5risk.violations` (R5).
  - H3: a breach makes the decision INFEASIBLE "V5 R5"; no partial book.
  - H1: a breach vetoes the entry.

### R6 — open risk

- **Exact V5 meaning:** `max_open_risk` 0.16. The sum over all open legs of 0.15 × notional must be ≤ 0.16 E, i.e. open gross ≤ 1.067 E.
- **Conflict with H3?** No. Frozen `gross_cap` 1.0 E gives ≤ 0.15 E at a build; drift can exceed it, so it is checked.
- **Conflict with H1?** No. Spot + perp ≤ 2 × 0.5 E, so ≤ 0.15 E.
- **Status:** **Retained** for both H3 (book level) and H1.
- **Frozen V6 rule / implementation:** `v5risk.violations` (R6), over all legs held after the decision.

### R7 — maximum positions

- **Exact V5 meaning:** `max_positions` 4. Open position ids must be ≤ 4. Each V5 carry perp leg is one id; the legs of a pair share one id ("pair legs share one slot", `check` docstring).
- **Conflict with H3?** **Yes, direct.** H3 needs ≥ 2·k' ≥ 6 legs (`min_k_prime` 3); R7 would forbid every H3 book.
- **Conflict with H1?** No. One hedged spot+perp pair is one position id, and there are ≤ 3 pairs (`h1.max_assets` 3).
- **Status:**
  - H3: **Replaced.**
  - H1: **Retained**, never binds.
- **Frozen V6 rule / implementation:**
  - H3: the leg count of the frozen construction, `h3.variants_k` [4, 6] and `h3.min_k_prime` 3 (S-H3-4/5/12).
  - H1: `v5risk.violations` (R7, `positions_after`).

### R8 — gross leverage

- **Exact V5 meaning:** `max_gross_leverage` 1.25. Gross notional, including the new order, must be ≤ 1.25 E.
- **Conflict with H3?** No. Frozen gross ≤ 1.0 E.
- **Conflict with H1?** No. Spot + perp ≤ 1.0 E.
- **Status:** **Retained** for both H3 (book level) and H1.
- **Frozen V6 rule / implementation:** `v5risk.violations` (R8).

### The other V5 rules (R0–R2, R9–R19)

The same principle applies to the rest of `carry.risk`. Every V5 rule is listed so that the constant
is read completely.

| Rule | Exact V5 meaning | H3 | H1 | Status | Frozen V6 rule / implementation |
|---|---|---|---|---|---|
| R0 | Unknown order intent → veto | no | no | Retained (structural) | Engines emit only fixed intents |
| R1 | Kill switch active → opens vetoed | no | no | Retained | Consequence of R4 (`killed`): no opens for the rest of the run |
| R2 | Operator pause → opens vetoed | n/a | n/a | Not applicable | A replay or forward evaluation has no operator pause |
| R9 | `max_leg_notional_frac` 0.30: opening order notional ≤ 0.30 E | no | no (⅙ E) | **Retained** | `v5risk.violations` (R9); H3 book-level INFEASIBLE, H1 per-entry veto |
| R10 | No pyramiding: an open may not grow an existing position; closes must not flip | **yes** (S-H3-9 size band rebalances legs *to target*, which grows legs) | no (one entry per asset, never grown) | H3: **Replaced**; H1: Retained (structural) | H3: S-H3-9 band rule (frozen `size_band` 0.25, `band_repair`); closes never flip (both engines) |
| R11 | An open needs a valid stop | no | no | Retained (structural) | Every H3 leg has the 15 % leg stop; every H1 pair has the combined stop |
| R12 | Liquidation distance ≥ 10 × stop, computed from the instrument maintenance margin | no | no | **Not applicable: not evaluable from the frozen inputs** | The frozen instrument file (`v4_instruments.json`) has no maintenance margin, so the V5 formula cannot be computed point-in-time. Liquidation is still detected (equity ≤ 0; H1 collateral ≤ 0) and reported. Stated, not hidden. |
| R13 | A spot sell may not take holdings below zero | n/a | no | Retained (structural) | H1 spot only sells its own holding |
| R14 | `max_orders_per_day` 30: an open is vetoed once 30 orders were sent that UTC day | no | no | **Retained** | `v5risk.violations` (R14); order count resets at 00:00 UTC |
| R15 | Limit price within 0.3 % of mid (IOC band) | n/a | n/a | Replaced (execution model) | V6 has no limit-band orders: S-H3-11 / S-H1-5 taker fills, S-H1-X maker at the reference ticker |
| R16 | Opens vetoed on stale bars | no | no | Replaced | S-DATA-5 freshness (stale quarter = no-op, decision deferred ≤ 6 h) and missing-mid ineligibility |
| R17 | Exchange minimum quantity and notional; spot buy ≤ cash | no | no | Retained | S-H3 drop rule (max(min_qty·mid, 10 USD)); H1 both legs ≥ their minimums; S-H1-2 cash rule |
| R18 | Opens vetoed on a reconciliation mismatch | n/a | n/a | Replaced | Replay accounting is exact (residuals 0); forward H1 hedge reconciliation closes a one-legged pair at once |
| R19 | Live trading vetoed when `live_enabled` is false | no | no | Retained | No live path in V6; `live_enabled=false` |

The V5 ineligibility exit (40 bps exit spread, 900 s grace) is retained as declared in D-E2.

## 5. Why this is not outcome-informed tuning

1. **The contradiction is visible in the constants alone.** 2 × `min_k_prime` = 6 > `max_positions`
   = 4. No return, P&L or data distribution is needed to see it. The conformance reviewer found it by
   reading code against spec text (finding M8), not from any outcome.
2. **No outcome exists.** No V6 backtest, holdout or forward evaluation has been run. The only run on
   development data is the masked blind invariant run (§7).
3. **The erratum has no effect on the development result.**
   - In the blind run (before and after this erratum) every H3 decision ended at the entry rule
     ("entry rule left fewer than 3 legs on a side", 174 decisions) or in MODEL WARMUP, before the
     risk checks.
   - H1 had 0 entries.
   - R7 never got a chance to bind, so resolving it cannot change what development showed.
4. **The erratum only tightens.**
   - Previously enforced: R3/R4 only.
   - Now additionally enforced: R5, R6, R8, R9, R14 (and R7 for H1).
   - Nothing that previously restricted trading was loosened, except the one rule that made H3 impossible
     by construction.
5. **No economic parameter was changed after the 0-position development result.** Unchanged (frozen
   constants, zero diff):
   - entry hurdle;
   - cost multiplier m ∈ {1.5, 2.5};
   - forecast horizons 168 h / 720 h;
   - k ∈ {4, 6}, `min_k_prime` 3;
   - σ* 0.08;
   - `standalone_side_ratio_max` 1.5;
   - net-dollar cap 0.2;
   - beta limit 0.05 E;
   - fee model (Kraken public Tier 1);
   - spread/slippage model;
   - `min_leg_usd` 10;
   - evidence gates.
   
   The result "these strategies do not trade / NO-GO" is accepted as a possible correct V6 outcome.

## 6. Holdout seal (evidence at the time of writing)

- No opening markers exist:
  - `~/.luna` does not exist;
  - `<git common dir>/luna-v6` does not exist.
- No holdout pin or token exists.
- No holdout download directory exists. A search of the user's Documents tree for `*holdout*` data
  directories found none.
- `holdout_data.download` / `load_holdout` require the `HoldoutAuthorization` from
  `guard.authorize_holdout`. That needs:
  - the code-freeze tag;
  - a clean tree;
  - the spec and constants hashes;
  - the exact approval phrase from the user.
  
  None has been given.
- The frozen development inputs end before 2026-07-01 (`inputs_end_before_holdout: true` in the blind
  report).

## 7. Blind development run: nothing outcome-related was seen

The blind runner (`luna v6-blind-dev`, `luna/v6/blind.py`) writes only mechanical invariants:
- data availability, decision shares and infeasibility reasons;
- leg counts, min-quantity drops and risk-check shares;
- the beta and vol-ratio invariants;
- accounting residuals and PIT checks.

`_assert_masked` refuses any key containing pnl, net, ret, equity, carry, sharpe, profit, loss,
drawdown, kill or stop. There is no unmasking option. Development runs use `invariant_mode`: R3, R4,
stops and resets are off, and sizing uses the initial capital, so no counter carries a P&L signal.

## 8. Effect on the implementation

- `luna/v6/v5risk.py`: R5, R6, R7, R8, R9 and R14 with the V5 values. Tests in `tests/v6/test_v5risk.py`
  cover each rule at its V5 boundary, H3 "INFEASIBLE, no partial book" and H1 per-entry vetoes.
- `luna/v6/declared.py`: D-E1 is superseded by this erratum.
- This erratum's sha256, commit and tag are recorded in the freeze record.
