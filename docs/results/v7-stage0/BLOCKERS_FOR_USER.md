# BLOCKERS FOR USER: V7 pre-freeze (2026-10-14)

## RESOLUTION STATUS: FINAL EDITORIAL CLARIFICATIONS (after red team round 4: 0 Critical / 0 Major)

| Item | Status | Spec |
|---|---|---|
| EC-1 instance-end `known_at` | **CLOSED:** close + 7 × 24 h, canonical. The < 24 h interval before calendar-day verifiability is decision-inert (U8 false, HALTED); population and D0 unchanged (`rt5-input-availability.md`) | S-2.2, DEC-44 |
| EC-2 A0 at all three horizons | **CLOSED** | S-10.6 |
| EC-6 E8 vs DEC-21 | **CLOSED:** explicit separation | S-1.7, S-14.1 |
| EC-7 R12 | **CLOSED:** optional, valid with zero rows | R12, S-5.4 |
| EC-8 recorder ID extraction | **CLOSED:** transport only | S-1.7, §15, DEC-45 |
| EC-9 funding flags | **CLOSED:** 22 transitions valid, 10 genuinely invalid; RT-5 ≤ 0.054 % PASS | S-2.6, R2, DEC-47 |
| EC-11 forward A0 | **CLOSED:** forward only; not applied to `W_HOLD` | §15, S-5.4, K11 |
| R2-8 / DEC-39 fee evidence | **CLOSED (explicit user decision, 2026-10-14):** the evidence standard is replaced by qualified official-source retrieval evidence; official Support article 360033544231 (en-AU, en-PH): maker 0.02 %, taker 0.05 %; BNB OFF. Caveat: not independently rendered in the V7 environment (regional redirect / AWS WAF). Record: `binance-fee-evidence.md` (sha256 `63a9fa95…`) | S-4.3, R9, DEC-39 |

## RESOLUTION STATUS: ROUND-3 PATCH (user decisions on M3-1 … M3-5, E6 / E8 / E10 / E11; 2026-10-14)

Applied as DEC-43 … DEC-47 plus E10. The round-3 items at the end of this file are kept as the historical record.

| Item | Status | Spec |
|---|---|---|
| M3-1 terminal settlement | **CLOSED (rule):** A events → automatic settlement at the official time and exact price (taker fee, no spread / slippage); B / C → no fill. A held B / C event makes that run `INVALID RUN — terminal settlement not reconstructible`; `W_DEV` stops before selection if any of the six candidates or A0 is invalid. **Audit:** 26 eligible `W_DEV` terminations: **A 0 / B 14 / C 12** (`m31/M3-1-settlement-feasibility.md`). There is no official settlement-price source and no 1-second index archive. 20 terminations aren't protected by a timely U6 exit | DEC-43, S-5.4, S-10.6, S-13.4, K11, R12 |
| M3-2 instance-end `known_at` | **CLOSED** | DEC-44, S-2.2, R1, L2 |
| M3-3 forward CMS recorder | **CLOSED:** write-only, sealed, unreadable before L7 | DEC-45, S-1.7, §15 |
| M3-4 network guards | **CLOSED:** exact endpoint + parameter allowlist N1–N6; Wayback exact-pair / no-redirect / `Memento-Datetime` rule; refusal tests | DEC-46, S-1.7, L10 |
| M3-5 funding interval text | **CLOSED:** DEC-29 restored exactly; RT-5 v3 0.054 % | DEC-47, S-2.6 |
| E6 / E8 / E10 / E11 | **CLOSED** as ratified | DEC-46, DEC-43, S-5.4 |
| m3-8 Wayback lag | **FIXED:** the timezone bug is corrected; 0 captures precede `releaseDate`; early captures verified by content | `m31/M3-1-settlement-feasibility.md` appendix |
| R2-8 fee snapshot | **OPEN (operational):** the freeze isn't permitted until it exists | DEC-39 |

## RESOLUTION STATUS: FINAL PRE-FREEZE PATCH for R2-1 … R2-9 (user decisions, 2026-10-14)

Applied to the spec as DEC-35 … DEC-42, plus the R2-5 deletion. The round-2 items further down are kept as the historical record.

| Item | Status | Spec |
|---|---|---|
| R2-1 dead / dormant contracts (C2-1) | **CLOSED:** U8 activity gate; instances by activity (≥ 7 UTC calendar days without a volume > 0 15-minute bar); notice-independent HALT; no retroactive fill; waiting exit or invalid. U4 / DEC-28 unchanged. RT-5 re-run: see `rt5-input-availability.md` | DEC-35, S-2.2, S-2.3, S-5.4, S-7.3 |
| R2-2 mutable notice bodies | **CLOSED:** `known_at` = max(`releaseDate`, archived capture timestamp); no snapshot → no historical U6; forward `known_at` = `recv_ts` | DEC-36, S-2.5, R11 |
| R2-3 scheduled delist times | **CLOSED:** unique, explicitly mapped timestamp or null; U6 doesn't depend on it; 72 h clear only with a unique time | DEC-37, S-2.5, S-7.3 |
| R2-4 spread proxy | **CLOSED:** published AR window-level estimator (mean of products, then max(·, 0) and the root), ÷ 2, × 2.0, 1 bp floor; not called conservative; P6 revised to fees × 1.5, slippage × 1.5, spread × 3.0 (history kept) | DEC-38, S-4.5, P6 |
| R2-5 post-freeze sentence | **CLOSED:** deleted | S-5.5, DEC-27 |
| R2-6 forward evaluability | **CLOSED:** evaluable quarter = primary replay inputs only; book / ticker diagnostic | DEC-42, §15 |
| R2-7 seal guards | **CLOSED:** endpoint allowlist in code + refusal tests; incident file immutable, included unchanged | DEC-41, S-1.7, L10, S-14.1 |
| R2-8 fee snapshot | **OPEN (operational):** freeze not permitted until an official non-empty VIP 0 snapshot (content + UTC + bytes + SHA-256) exists. The Chrome extension was not connected on 2026-10-14, so no capture was possible | DEC-39, S-4.3 |
| R2-9 ratifications | **CLOSED:** fill-time proxy, confident match, unreconciled funding → invalid, missing-proxy rules | DEC-40 |

## RESOLUTION STATUS: PRE-FREEZE BLOCKER RESOLUTION PATCH (user decisions, 2026-10-14)

Applied to the spec as DEC-22 … DEC-34. The text below this table is kept as the historical record of what was raised.

| Item | Status | Spec |
|---|---|---|
| B-1 / RT-1 spread | **CLOSED:** proxy only in every window; λ = 2.0 fixed; exact Abdi–Ranaldo half-spread formula; R6 diagnostic only | DEC-22, S-4.4, S-4.5, L8 |
| B-2 fees | **CLOSED (numbers):** 0.0200 % / 0.0500 %, BNB off. **OPEN (operational only):** local official-page snapshot with UTC + SHA-256 | DEC-23, S-4.3 |
| B-3 relists | **CLOSED:** contract instances (≥ 7 UTC days without archived bars) | DEC-24, S-2.2 |
| RT-2 / R-5 delisting | **CLOSED:** point-in-time S-2.5 rule | DEC-26, S-2.5, S-5.4 |
| RT-3 L-265 | **CLOSED:** accepted as is; missing-filter fallback | DEC-34, S-14.1 |
| RT-4 seal | **CLOSED (governance):** permanent record `INCIDENT-2026-10-14-seal.md`; S-1.7 strict | DEC-25, S-1.3, S-1.7 |
| RT-5 invalid inputs | **CLOSED:** audit done, 36 / 1,538,625 = 0.0023 % < 0.5 % (`rt5-input-availability.md`) | DEC-27, S-5.5, S0-14 |
| RT-6 identity `known_at` | **CLOSED:** L1 identity whitelist; unknown → exclude | DEC-31, R1, L1 |
| R-1 zero-volume bars | **CLOSED (change rejected):** zero-volume bars count as present for U4 | DEC-28, U4 |
| R-2 funding intervals | **CLOSED** | DEC-29, U3, S-2.6 |
| R-3 unknown types | **CLOSED:** 5 symbols excluded | DEC-30, S-2.1 |
| R-4 funding source | **CLOSED:** archive primary, REST fills gaps | DEC-32, S-2.6 |
| M-3 survivorship | **ACKNOWLEDGED:** known limitation | DEC-32, S-2.2 |
| M-7 truncated monthly files | **CLOSED:** monthly ∪ daily by primary key | DEC-33, S-2.2 |

---

## Historical record (as raised before the patch)

Nothing here has been changed in the spec. Each item needs your decision or action. The options are proposals only.

## B-1: DEC-11 λ can't be resolved as written (Major)

**Fact (Stage 0):**
- R6 (USD-M bookTicker) exists only for 2023-05 → 2024-04: 270 symbols, **1.06 TB zipped** (roughly 7 TB CSV), and 23 % of eligible `W_DEV` candidates.
- `W_HOLD` has no R6.

**Gaps in the frozen rule:**
1. **Matched observation is undefined.** S-4.5 says "matched (venue, symbol, timestamp)", but the proxy is a per-15-minute-bar value while R6 is tick-level. Which R6 statistic is the "observed half-spread" for a bar: the snapshot at the bar close, the time-weighted mean over the bar, or the median? And is the comparison per bar, or on the 24 h ĥ?
2. **Scope.** "Mean over matched observations in `W_DEV`" means streaming about 1 TB. That is infeasible on the PC (6.8 GB free) and the 2 GB VPS. Any subsample would be a new design choice.

No understatement rate was computed on any sample, so the choice below stays outcome-blind.

**Options:**
- **(a)** Define the match as the time-weighted mean of (ask − bid)/2/mid over each 15-minute bar vs that bar's proxy, computed over **all** R6 symbol-bars on a larger machine. About 1 TB of download; days of compute.
- **(b)** Same matching, on a **pre-registered deterministic sample**, e.g. every R6 symbol on the 15th of each R6 month (12 days, about 35 GB), fixed before anything is computed.
- **(c)** Skip the test and fix λ = 2.0 (the conservative branch) for all proxy periods.

*My recommendation:* (b) or (c). (c) is simplest and conservative.

## B-2: official Binance USD-M VIP 0 fee snapshot not obtainable from the UK (Major; factual)

binance.com geo-redirects UK IPs to `en-GB` and hides the futures fee table. Scripted requests get an empty challenge. No workaround was used.

**Action:**
1. From Turkey, open https://www.binance.com/en/fee/futureFee (USDⓈ-M tab).
2. Save the page (Ctrl+S, "Webpage, complete" or PDF) into `LunaTradingBot-v7/var/v7/stage0/fees/`.
3. Write down the VIP 0 maker/taker numbers and the time.

I'll hash it and apply DEC-18 (a factual correction only if the numbers differ from 0.020 % / 0.050 %). Until then the fee constants are **unverified**.

## B-3: symbol-name reuse / relists aren't covered by S-2.2 / U1 (Major)

**Fact:** 12 names stopped trading for ≥ 7 days (up to 2.3 years) and then traded again under the same symbol: BNT, OG, BTT, RIF, BTCST (funding gaps of 1,632–20,016 h), and AERGO, BNX, CTK, CVC, ICP, MAVIA, TLM (no-trade spans in klines).

**Problems:**
- S-2.2 defines listing time as the first archived bar, so a relisted name passes U1 (listing age ≥ 60 d) on its first day back.
- U3 sees ≥ 30 d of old funding history.
- Features (FL30, 30 d vol, momentum) span the dormant gap.

**Options:**
- **(a)** Treat trading resumption after a ≥ 7-day span with no volume > 0 bar as a **new listing**: U1 and U3 restart, and features only use post-resumption data.
- **(b)** Exclude any symbol for 60 d after such a resumption (same effect on U1, simpler).
- **(c)** Leave as specified (not recommended: it mixes two different instruments under one name).

*My recommendation:* (a). The 7-day threshold is a data rule, not an economic parameter.

## Smaller rules to confirm (data semantics, not economic)

These are mechanical; I'd pre-register them unless you object.
- **R-1 Zero-volume bars:** U4 counts only bars with volume > 0. Delisted contracts keep publishing zero-volume bars for years. G_D0 is unaffected (U2 fails for them), but U4 as written would pass a dead symbol.
- **R-2 Funding interval at switches:** the nominal interval for a spacing check = max(interval of the two adjacent records). 40 of 45 apparent gaps are switch artefacts. When there is no archive interval (5,233 REST-only records), infer it from the spacing to the previous record.
- **R-3 Unknown `underlyingType`** (AERGO, BDXN, DOTECO, LEND, MBL): treat as COIN except DOTECOUSDT (an index, "DOT ecosystem"), or exclude all 5. D0 is the same either way.
- **R-4 Funding source:** REST ∪ archive, with the archive primary for LITUSDT, where REST is empty. Rates agree exactly on all 1.36 M overlapping records.
- **R-5 Delisting-notice matching:** symbol tokens in a notice's title or body, USDT-margined context only, `releaseDate` ≤ last trade. Token migrations (MATIC→POL, FTM→S, …) count only if the notice is in the official catalog.

## Resource prerequisite (not a decision)

- **1-minute klines** (S-5.2 reference prices) need 17.3 GB (`W_DEV`) + 9.4 GB (`W_HOLD`, only at the opening). The PC has 6.8 GB free.
- The `W_DEV` build needs about 25 GB of free disk. Options: free space on C:, a personal external disk, or a VPS volume.

## Red-team additions

Added 2026-10-14 by the independent pre-freeze red team. Details and evidence are in `v7-prefreeze-redteam.md`. Nothing has been changed in the spec. The options below are proposals only.

### RT-1: spread source policy and exact proxy formula (extends B-1; Critical C-1)
- "Real R6 wherever it exists" needs the full 1.06 TB, even if λ is fixed. It also gives three different spread sources: `W_DEV` (proxy plus 12 months of R6), `W_HOLD` (proxy only) and forward (R6 only). Those sources feed F6/F14, U5, A0 cost and every label's costs.
- **Decide:**
  - **(i)** Features and gates use the bar proxy in every window, including forward, where it is computed from the recorded bars.
  - **(ii)** Costs use either real R6 where it exists, or the proxy everywhere.
  - **(iii)** Confirm the exact formula: half-spread = Abdi–Ranaldo/2; negative per-pair estimates → 0; λ applied before the 1 bp floor; median over the bar pairs fully known by `t` − 1 min.
- *Red-team preference:* B-1 (c) λ = 2.0, plus proxy-only for features, gates and costs in every window, for consistency. R6 then serves only as a post-freeze diagnostic that changes nothing.

### RT-2: U6 / S-5.4 semantics for postponed or cancelled delistings (Major M-2)
- OMG was postponed twice. MAVIA and BLZ were named in delisting notices and kept trading for months or years.
- U6 as written ("ineligible from that time") has no end. R-5's "`releaseDate` ≤ last trade" uses future information.
- **Decide:**
  - a point-in-time rule for when U6 ends (e.g. a postponement or cancellation notice, or the symbol still trading 72 h after the scheduled time parsed from the notice);
  - an operational "halted" test for S-5.4.

### RT-3: L-265 BTC hedge granularity (Major M-5; deployment layer only)
- At E = 265 a hedge is required once |β_book| > 13.25 USD.
- The smallest BTC order is max(`MIN_NOTIONAL` 50 now / 100 in 2023-11, 1 step = 0.001 BTC ≈ 34–112 USD in 2023-11 → 2025-06). So most required hedges are infeasible, and the frozen rule then forbids entries. D1 ≥ 90 % will very likely fail mechanically.
- **Decide:**
  - accept this (an expected "GO — not deployable at 265 USD"); or
  - pre-register an L-265-only rounding rule; or
  - change E for the deployment gate.
- Also confirm the filter fallback for symbols missing from current `exchangeInfo`: the last Wayback capture, otherwise exclude.

### RT-4: seal incident (Major M-1)
- `delist/catalog161_index.json` holds 142 delisting-notice titles dated in `W_HOLD` and 32 dated in M6 (latest 2026-10-09).
- `futures_delist_announcements.json` holds 30 more (2 in M6).
- S-1.7 allows `W_HOLD` file listings only, and §18 forbids anything in M6. No body or outcome was read.
- **Decide:**
  - acknowledge the incident;
  - quarantine or redact those entries into a sealed, hash-pinned file;
  - choose whether S-1.7 should explicitly allow `W_HOLD` announcement index metadata (id, `releaseDate`, no titles);
  - choose how `W_HOLD` notice bodies are captured at the opening (the CMS is mutable).
- **Done overnight (containment, not a decision):** the 174 + 30 later-dated entries were **deleted** (not quarantined) from both files, and 1,330 related-article links were stripped from the stored `W_DEV` bodies. They can be re-fetched from the CMS at the opening if needed. The Stage-0 report's false "nothing in M6 was read" claim is corrected, including the 5 newest titles seen in the first API probe. The remaining decisions above (acknowledge, the S-1.7 wording, the opening-time capture) are still yours.

### RT-5: S-5.5 "reopen before freeze" can't be evaluated (Major M-6)
- Labels can't be computed before freeze.
- **Decide:**
  - **(a)** an outcome-free label-availability audit before freeze (only the existence of reference prices, funding records and mark fallbacks; needs the 1-minute data); or
  - **(b)** turn the 0.5 % rule into a post-freeze K2-type stop with a user decision.

### RT-6: metadata `known_at` vs L1 (Major M-4; mechanical, please confirm)
- `underlying_type`, `contract_type`, base and multiplier come from present-day or Wayback metadata (`known_at` = `retrieved_at`), which fails L1 for every historical decision.
- *Proposal:*
  - treat them as instrument identity, with `known_at` = `first_bar_ts`, via an explicit L1 whitelist;
  - exclude any symbol whose type is unknown (this replaces R-3's "COIN except DOTECO");
  - make an instrument eligible only from a known type change (e.g. pre-market → perpetual), otherwise exclude it.

### Also please acknowledge (no option needed)
- **M-3 survivorship:**
  - Old instances of BNT, BTT, OG and RIF have REST funding but no archived bars, so they can't be reconstructed.
  - LIT's `W_DEV` instance has no REST funding (the archive has it).
  - Proposal: document this as a known limitation; archive is the primary funding source and REST only fills the missing months (R-4 reversed); U3 and the funding features count only records from the current instance's first bar.
- **M-7:** truncated monthly files (2022-02-26..28, 2022-04-01..02 for about 49 symbols). Pre-register R3/R5 = monthly ∪ daily files (mechanical).

## Red-team round 2 additions

Added 2026-10-14 by the independent round-2 red team. Details and evidence are in `v7-prefreeze-redteam-r2.md`. Nothing has been changed in the spec. The options below are proposals only. None of them re-opens DEC-22 … DEC-34 unless you choose so.

### R2-1: dead contracts that keep publishing bars (Critical C2-1)
**Fact (`W_DEV` archive):**
- Only 3 of the 7 dormancy/resumption cases (BNX, ICP, TLM) contain a ≥ 7-day gap with no archived bar. AERGO, CTK, CVC and MAVIA keep zero-volume bars throughout, so DEC-24 doesn't split them.
- Funding records keep being published, non-zero, during every dormancy checked.
- During a dormancy, U1, U3, U4 (DEC-28), U5 (a flat bar gives the 1 bp floor) and U7 all pass. U2 still passed 1 / 7 / 14 d after onset in 18 / 14 / 9 of the 65 dormancy onsets.
- Labels and the book can enter at stale zero-volume prices and collect published funding with no price variance. In the no-notice case, S-5.4 can set `t_x` before `t_e`.

**Decide** (any combination):
- **(i)** Notice-independent operational halt for outcomes: a fill or reference price needs a volume > 0 1-minute bar within the 60-minute data-gap window; otherwise stressed exit at the last volume > 0 bar; if that is before `t_e`, no label.
- **(ii)** A proxy pair that contains a zero-volume bar is invalid, so there is no valid pair, the proxy is missing (staleness) and no entry is made.
- **(iii)** Instance boundary on ≥ 7 UTC days without any volume > 0 bar. This changes the B-3 wording.
- **(iv)** Add U2 to the S-7.3 ineligibility exits.

*Red-team preference:* (i) + (ii) minimum; (iii) recommended.

### R2-2: notice text edited after `releaseDate` (Major M2-1)
- The stored `W_DEV` bodies contain 25 "updated on D" in-place edits, including a changed delisting date (GFT) and a margin-type change (CRV).
- `lastUpdateTime` is empty in all bodies.

**Decide:** an edit rule. For example:
- a dated update block has `known_at` = D + 1 d 00:00 UTC;
- if the update can't be separated from the original text, the whole notice takes `known_at` = D + 1 d;
- an undated update → the notice isn't usable historically (the no-notice path applies).

### R2-3: scheduled-time parsing and the 72 h test (Major M2-2)
- 32 of the 47 `W_DEV` futures notices state more than one time; 5 state none.

**Decide:**
- Use the stated delisting/settlement time per matched contract; if several apply to it, the latest.
- Unparseable → U6 stays active until a matched postponement or cancellation, or until the instance ends; no halt test.
- "72 h continued trading" = ≥ 1 volume > 0 1-minute bar in each of the 72 hours after the scheduled time.

### R2-4: proxy statistic is not conservative (Major M2-3; acknowledge or change)
- In a synthetic simulation (no market data), per-pair truncation followed by the median sits at the 1 bp floor in about 35–49 % of 24 h windows at realistic 15-minute volatility.
- Example: σ = 50 bps per bar with a true half-spread of 3 bps gives a proxy of 1.2 bps.
- So λ = 2.0 doesn't deliver conservatism.

**Decide:**
- **(a)** Acknowledge it as a known property and keep the formula; or
- **(b)** Aggregate the pair products before truncation and the square root (Abdi–Ranaldo's aggregate-then-root form), then apply λ and the floor. λ stays 2.0.

### R2-5: the post-freeze sentence in S-5.5 (Major M2-4)
"After freeze … triggers K8 / K2-type review" was not part of option (a), and its consequence is undefined.

**Decide:**
- **(a)** Delete it (K8 already covers the holdout); or
- **(b)** Define it as a K2-type STOP plus an explicit new-spec decision, with no data-rule change within this spec.

### R2-6: forward evaluability (Major M2-7)
§15 decides evaluable days and decision skips by book/ticker snapshot freshness, while R6 is "diagnostic only"; bar coverage isn't in the rule.

**Decide:**
- **(a)** Evaluability = recorded 1m/15m bar coverage + funding freshness; or
- **(b)** Keep snapshot liveness, with an explicit carve-out saying no snapshot value is used.

### R2-7: seal guard and timing (Major M2-6; please acknowledge)
- Add a pre-opening endpoint denylist with refusal tests: CMS list/detail, Wayback CDX, and captures dated ≥ 2025-07-01.
- `W_HOLD`-dated Wayback filter captures (S-14.1 step 6) are fetched only at the opening.
- Current `exchangeInfo` status fields are not used before the opening.

### R2-8: preconditions and wording (Major M2-5)
- The S0-14 result file doesn't exist yet.
- Pre-register the audit denominator before running it: the candidate set, side × horizon, the unit per label, and P(τ) 1-minute only.
- The fee snapshot files are 0 bytes.

**Decide:** whether the freeze may proceed with the fee snapshot still pending (S-4.3 says both "must exist before freeze" and "operational blocker only").

### R2-9: one-line ratifications of editor choices
- `h_fill(τ)` = the 24 h proxy median at τ.
- The "confident match" definition, extended to multiplier and renamed symbols and to multi-contract notices.
- Off-schedule settlements are flagged but stay cash flows (not "fail").
- Halt test on 1-minute bars.
- A missing proxy at a fill → the label is invalid; the book uses the last valid value with the S-5.4 floor.

## Red-team round 3 additions

Full report: `v7-prefreeze-redteam-r3.md`. RT-5 v2 passes as implemented: 0.054 % invalid, all 111 old exit ≤ entry cases removed, D0 = 2021-01-01 holds. No seal reset is needed. Items that need your decision or ratification:

| ID | Issue | Your decision |
|---|---|---|
| M3-1 | A leg held into a contract death with no point-in-time-known notice has no executable exit, so the run is `INVALID RUN` (S-5.4 l.395–397). This is not rare: 16 G_D0-eligible instance ends in the 2023-01 → 2025-06 out-of-fold span, most at 08:45 / 09:00 just after the 08:05 decision, and 15 of them with no catalog-matched notice. S-10.6 / S-10.7 don't say what an invalid W_DEV run means | (1) the W_DEV consequence of `INVALID RUN` (e.g. a K2-type stop with a new-spec decision); (2) whether to populate your "official settlement / delivery price" path (needs an outcome-free source check plus a §3 table), or ratify E11 that it stays empty; optionally (3) an outcome-free exposure count before freeze |
| M3-2 | The instance end (`last_trade_ts`) is ex-post but drops decisions at decision time (W_DEV: LUNAUSDT 2022-05-12 16:05, BNXUSDT 2023-02-01 00:05) | Approve the text fix: `last_trade_ts` `known_at` = last traded bar close + 7 d; gates never use the end before then |
| M3-3 | "CMS blocked while M6 is sealed" (S-1.7 l.142) contradicts the forward announcement recorder (§15 l.801, R11 in evaluability l.812) | Ratify E6 with a carve-out: the forward recorder may poll announcements write-only from T_F7 (S-1.3 raw recording); research reads stay blocked until R_V6 |
| M3-4 | Allowlist holes: `fapi` is allowed by parameter, not endpoint (current-state endpoints ignore `endTime`); Wayback `/web/<ts>/` redirects to the nearest, possibly later, capture | Approve: enumerate `fapi` paths; fetch Wayback only by exact CDX-returned timestamps, in `id_` form, with no redirects and a timestamp check before parsing; add guard tests |
| M3-5 | S-2.6 l.250 / l.252 dropped DEC-29's mechanical interval inference and cascades: 4,885 unreconciled W_DEV records vs 33. Under the literal text the RT-5 rate would be ≈ 0.52 % > 0.5 % | Approve restoring DEC-29 inference inside DEC-40 (the RT-5 script's reading) |
| E8 | Current `exchangeInfo` identity fields are used, but R2-7 said "schema / filters only" | Ratify or forbid |
| E10 | No exit fill from a later instance, so a position waiting across a ≥ 7-day dormancy is invalid | Ratify |
| R2-8 | Official VIP 0 fee snapshot still missing (both files 0 bytes) | Operational; freeze is not permitted until it exists |

## Red-team round 4 additions

Full report: `v7-prefreeze-redteam-r4.md`. Result: 0 Critical, 0 Major, 11 Minor.
- The round-3 decisions (DEC-43 … DEC-47, E6 / E8 / E10 / E11) are faithfully implemented.
- The M3-1 audit (A 0 / B 14 / C 12) is re-verified against the raw captures (SRM, COCOS, XEM, AKRO, BNX #0).
- RT-5 v3: 0.054 %.
- No seal reset is needed.
- **The only remaining blocker is R2-8 / DEC-39, the official VIP 0 fee snapshot (operational).** The spec forbids the freeze until it exists, but it does not block the freeze review.

One-line ratifications requested. None is a blocker; each can be settled in the freeze review.

| ID | Issue | Your decision |
|---|---|---|
| EC-6 | E8 says "never backdate filters", yet S-14.1 branch 2 (DEC-21 / DEC-34) still applies current filters to W_DEV / W_HOLD L-265 / L-10k as a labelled approximation. No effect on P1–P11 | Keep the labelled diagnostic, or drop the historical D1–D3 approximation |
| EC-7 | R12 `terminal_settlements` is defined but empty, although you said "a table only if a real official source exists" | Keep R12 as the only vehicle for future exact A events, or delete it |
| EC-8 | M3-3 "no parsing" is written as "no title, body or symbol parsing". To record bodies, the recorder must extract article identifiers from the raw catalog response | Allow mechanical identifier extraction solely to schedule raw detail fetches (otherwise forward U6 works on titles only) |
| EC-9 | S-2.6 flags a spacing-inferred regime switch and one successor of an undetermined or off-schedule record. This is bounded and doesn't cascade: 50 W_DEV records, 181 labels | Ratify |
| EC-11 | Forward `INVALID RUN` if A0 holds a B / C event (l.869), although A0 isn't required in the forward run | Keep, or limit the forward run to ML\* (A0 then voids only its own comparison) |
| EC-1 / EC-2 | `instance_end_known_at` = 00:00 UTC after the 7th inactive day (never before close + 7 d); A0 must be valid at all three horizons before selection | Confirm |
| EC-10 (info) | 6 genuine B notices name "`<BASE>` USDT-Margined Contract" with no `XXXUSDT` token, so strict DEC-40 gives no U6 protection for them | No action unless you want to widen DEC-40 |

Recommended editorial fixes (no decision needed): m4-1 to m4-11 in the report. The most useful are:
- N2 `marker` pagination;
- an explicit post-opening allowlist (N1–N6 with the bound moved to 2026-07-01);
- E10 promoted to a top-level rule, with an instance-scoped halt test;
- A-event label exits without spread or slippage.
