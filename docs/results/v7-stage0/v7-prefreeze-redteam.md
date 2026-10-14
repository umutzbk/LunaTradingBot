# V7 pre-freeze red team (independent, adversarial)

- **Date:** 2026-10-14
- **Inputs:** spec Draft 2 + PRE-STAGE-0 DECISION PATCH (uncommitted); `docs/results/v7-stage0-audit.md`; `docs/results/v7-stage0/*`; Stage-0 scripts in `~/.claude/jobs/34c13fa2/tmp/s0/`; raw `W_DEV` data in `var/v7/stage0/`.
- **Boundaries kept:** read-only, apart from this file and an append to `BLOCKERS_FOR_USER.md`. I computed no label, return, P&L, IC, spread proxy level or understatement rate. I downloaded nothing. My only spot checks used `W_DEV` availability data, plus BTC price levels, which I used only to size the exchange step. My scripts are in `%TEMP%\v7rt\`, outside the repo.
- **Spec unchanged.** Every resolution below is a proposal for the user and the freeze review.

## Independent re-verification of Stage 0

| Claim | My check | Result |
|---|---|---|
| D0 = 2021-01, min 31 | I wrote a separate recount (`d0_check.py`) using REST ∪ archive funding, R-2 interval rule, two U4 denominators (strict 95-bar window and 96-bar), plus a U3 variant that also fails an open gap at `t`. I ran it on 2021-01-01 00:05, 01-06 16:05, 01-07 00:05, 01-15 08:05 and 02-10 16:05 | **31 / 31 / 31 / 42 / 55**, the same symbol sets as `d0_eval.elig.npz` under every variant. **Confirmed.** |
| D0 margin | U2 medians at 2021-01-01 00:05 | 2 eligible symbols sit within 5 % of the 20 M threshold: SNXUSDT 20.61 M and XTZUSDT 20.97 M. The next ineligible one is ZECUSDT at 19.12 M. Losing both near-threshold symbols would give 29, so D0 would move to 2021-02 and leave 9 folds. The spec still passes either way. |
| Funding gap classification (40 switch + 5 real) | I reclassified all 45 using the previous record's archive interval | 34 are clean switches (spacing equals the previous interval). 6 are off-schedule switches (FLM, FTT, OMG, SIGN, T, YGG: 8 h→2 h or 4 h→1 h, with spacing smaller than the previous interval, so no record is missing). 5 are big gaps. **Confirmed.** R-2's "max of adjacent" rule covers all 40. |
| F15 coverage 81.8 %, F7 60.0 % | I recomputed from `elig_wdev.elig.npz` and the daily listings | F15 is 0.8178 (day of `t` and day `t` − 7 present), an exact match. F7 is 0.598–0.602 depending on the day rule. **Both are below 95 % under any reading, so the drops stand.** |
| Delisting notice bodies `W_DEV`-only | I compared each body file's `releaseDate` with the catalog | 225 bodies, all < 2025-07-01. The `W_HOLD` entries hold only the placeholder "not fetched". **Confirmed.** |

## Errors found in the Stage-0 audit itself

- **E-1 (Major → M-1). "Nothing in M6 was read" is false.**
  - `var/v7/stage0/delist/catalog161_index.json` stores **32 article titles dated ≥ 2026-07-01** (the latest is 2026-10-09) and **142 dated in `W_HOLD`**.
  - `futures_delist_announcements.json` stores 30 `W_HOLD`-or-later titles, 2 of them in M6.
- **E-2 (Major → M-7). The common 288- and 192-bar gaps are truncated monthly files, not real gaps.**
  - 2022-02-26 → 02-28 is missing for 48 symbols, and 2022-04-01 → 04-02 for 49.
  - The daily 15-minute files for those days exist (checked for FILUSDT).
  - The audit's "31,314 missing bars" and the 2022-02 → 04 breadth dips (min 63–68) are therefore partly artefacts.
- **E-3 (Major → M-3). The relist / name-reuse diagnosis is incomplete and partly wrong.**
  - For BNT, OG, BTT and RIF, the old instance has REST funding but **no 15-minute klines in the archive**. That is a survivorship issue, not a same-name relist with continuous bars.
  - **BTCST is not a relist.** Its last volume > 0 bar is 2021-03-12 and it never traded again in `W_DEV`. Its funding gap lies inside a zero-volume dead contract whose funding records kept being published. B-3 lists it as "traded again".
- **E-4 (Minor → m-13). `d0_eval.py` U3 logic.**
  - If no funding record is known in [`t` − 7 d, `t`], `sp` is empty and `np.all([])` is True, so U3 passes a symbol whose funding stopped.
  - It never checks the open gap from the last record to `t`.
  - It uses REST only, so LITUSDT can never pass.
  - Impact on D0: none. My recount, with all three fixed, gives identical counts. **This code must not be reused for implementation.**
- **E-5 (Minor → m-12). Unreproducible outputs.**
  - No script is saved for `f7_f15_coverage.json`, `r6_coverage.json` or `delisting_coverage.csv`.
  - `bar_audit.json` labels `delisted_in_wdev: 27` with a last-bar rule that ignores volume (the spec rule gives 70).
  - `delisted_underlyingType_unknown: 27` uses current `exchangeInfo` only, while the report says 5.
- **E-6 (Major → M-2). Delisting matching: "32 matched" overstates coverage.**
  - BLZ (859 d) is a confirmed mismatch.
  - OMG's 56-day "lead" is a postponed delisting.
  - MAVIA was named in a delisting notice and kept trading. The audit does not mention postponements or cancellations.

---

## Critical

### C-1: DEC-11 (spread source) can't be implemented as written, and it creates a train/holdout/forward source inconsistency
**Evidence.**
- R6 exists only from 2023-05 to 2024-04: 270 symbols, 1.06 TB zipped (`r6_coverage.json`; audit S0-5). There is none in `W_HOLD`.
- S-4.5 says "Real R6 half-spreads are used **wherever R6 exists**." So even with λ fixed (B-1 option c), the `W_DEV` build still has to ingest about 1 TB of tick data to produce `ĥ(t)`, `h_fill`, U5 and F6/F14 for those 12 months. That is infeasible on the PC (6.8 GB free) and on the 2 GB VPS.
- §15 records real book data forward, so `ĥ(t)` there comes from R6 (S-4.4).
- The result is three different spread sources: `W_DEV` (proxy + 12 months of R6), `W_HOLD` (proxy only) and forward (R6 only). They feed:
  - the features F6 and F14 that the frozen model consumes;
  - the U5 gate (7.5 bps) and the 20 bps exit;
  - A0's `cost(a)` and hurdle;
  - every label's `ss_in`, `ss_out` and `hedge_cost`.
- **The proxy formula is underspecified:**
  - It doesn't say whether the Abdi–Ranaldo output is the full spread or the half-spread. That is a factor of 2 against the 7.5 bps U5 gate.
  - It doesn't say how negative per-pair estimates are handled.
  - It doesn't fix the order of floor (1 bp), λ and the 24 h median.
  - AR on bar *b* needs bar *b* + 1, and the spec doesn't require both bars to be fully known by `t` − 1 min.

**Why it matters.**
- A frozen model trained with F6 from mixed sources, then applied to proxy-only (`W_HOLD`) and R6-only (forward) features, gets a built-in distribution shift.
- The labels' costs change regime in 2023-05 and again in 2024-05.
- It's the costs, not the model, that make up most of the carry-after-cost (CAC) question. This affects P2, P6, P11 and U5 directly.

**Proposed resolution (user decision; not applied).** Add to B-1 (see RT-1):
- **(i)** One spread source for all features and gates in every window: the bar-based proxy, including forward, where it is computed from the recorded bars.
- **(ii)** Real R6 used for **costs** only where it exists, or nowhere (proxy-only costs).
- **(iii)** Pre-register the exact proxy formula: half = AR/2; per-pair negatives → 0; λ applied before the 1 bp floor; median over the 96 bar-pairs fully known by `t` − 1 min.

B-1's λ choice is still needed, but it doesn't settle C-1 on its own.

---

## Major

### M-1: Holdout seal / M6 boundary breached by stored delisting titles (S-1.7, §18)
**Evidence.** See E-1. S-1.7 allows only "file listings (name, size, published checksum)" of `W_HOLD`, and §18 forbids "anything inside M6". The titles name which symbols are delisted inside `W_HOLD`, which is survivorship information about the holdout. The audit report also claims nothing in M6 was read.

**Why it matters.**
- No outcome was computed, so the leakage impact is low.
- But the guard's letter is broken, and the stored file is future information sitting in the working tree.
- Separately, `W_HOLD` U6 at the opening will need notice **bodies** from a mutable CMS. Articles can be edited or removed between now and the opening.

**Resolution (user decision, RT-4).**
- Write an incident note.
- Move or redact entries ≥ 2025-07-01 from the working copies into a sealed, hash-pinned location.
- Amend S-1.7 / §18 explicitly to allow (or forbid) announcement **index metadata** (id, `releaseDate`, no titles) for `W_HOLD`.
- Pre-register how `W_HOLD` notice bodies are captured at the opening, accepting the CMS-drift risk or doing a blind hash-only capture now.
- The Stage-0 report's "Boundaries kept" table should be corrected.

### M-2: U6 / S-5.4 delisting semantics fail on postponed or cancelled delistings, and the R-5 matching uses look-ahead
**Evidence.**
- **OMG:** delisting notice 2024-12-06 (for 2024-12-16), "Postponed" 2024-12-14, "Further Postponed" 2024-12-20; last trade 2025-01-31.
- **MAVIA:** in the same 2024-12-06 notice, but it traded until 2025-06-30, with a no-trade span and then resumption.
- **BLZ:** notice "Will Delist USDT-Margined BLZ" 2022-08-17, yet it traded continuously (largest funding gap 8 h) until 2024-12-23.
- S-2.3 U6 says "ineligible **from that time**" with no end condition. MAVIA and BLZ would be ineligible for months or years while trading.
- R-5's condition "`releaseDate` ≤ last trade" uses the future last-trade date to decide matching.
- S-5.4 "delisted or halted before `t_x`" has no operational test. "Last 1-minute bar with volume > 0" fires on an illiquid symbol with no trade in the minute before `t_x`, and doesn't handle a halt followed by resumption before `t_x` (MAVIA, ICP 26 d, TLM 29.5 d, BNX 21.6 d).

**Resolution (user decision, RT-2).** Pre-register a point-in-time U6 rule. For example:
- ineligible from `releaseDate`;
- eligibility restored if a postponement or cancellation notice is published, or if the symbol still trades 72 h after the scheduled delisting time parsed from the notice;
- matching on symbol tokens in the title or body only, with no last-trade condition.

Also pre-register a "halted" test for S-5.4. For example: no volume > 0 1-minute bar in [`t_x` − 60 min, `t_x` + 60 min] **and** no resumption within the label window, otherwise the S-5.4 data-gap rule applies.

### M-3: Survivorship. Old instances of reused names have funding but no bars, and REST serves only the current instance
**Evidence.**

| Symbol | REST funding (old instance) | 15-minute klines start | Archive funding start |
|---|---|---|---|
| BNTUSDT | 2020-12-28 → 2021-04-27 | 2023-08-10 | 2021-01-01 |
| BTTUSDT | 2020-09-14 → 2020-11-05 | 2021-04-06 | 2021-04-06 |
| OGUSDT | 2023-04-09 → 04-21 | 2025-05-12 | 2025-05-12 |
| RIFUSDT | 2023-05-09 → 05-24 | 2023-10-21 | 2023-10-21 |

- LITUSDT's `W_DEV` instance has **0 REST records** but 4,747 archive records. LIT has `W_HOLD` and later files, which is consistent with REST serving only the newest instance of a reused name.
- My Wayback cross-check: all 236 USDT perpetuals in the 20 `W_DEV`-dated captures exist in the kline archive. So the purge looks limited to short-lived old instances.
- Only BNT's old instance (about 4 months, early 2021) could plausibly have passed U1.

**Why it matters.**
- S-2.2's "every symbol that has ever had USD-M files" can't see instruments whose bars were purged.
- REST and the archive are each incomplete, in opposite directions.
- U3 and the funding features would count an old instance's records toward a new instance's history.

**Resolution.**
- Document this as a known survivorship limitation, with the list above and a count of REST-funding-without-bars spans.
- Bound U3 and all funding features to records at or after the current instance's first archived bar. This extends B-3.
- Archive is the primary funding source; REST only fills the 28 missing archive months (this is R-4, reversed).

### M-4: R1 `known_at` for classification metadata contradicts L1
**Evidence.**
- R1 sets `known_at` = `retrieved_at` (2026-10-14) for "current metadata".
- `underlying_type`, base and multiplier come from current `exchangeInfo` and the Wayback captures. 506 of the 511 classifications rely on them.
- L1 asserts `max_known_at` ≤ `t` − 1 min for every input of DT1. Taken literally, every historical decision fails L1. If the rule is quietly bypassed in code, the guard means nothing.
- `W_HOLD` contains about 110 names that are now `TRADIFI_PERPETUAL` (equities, commodities, pre-market) and only 5 names missing from current `exchangeInfo` (AERGO, BDXN, BTCST, MDT, SXP). Classification can only come from present-day metadata.

**Resolution (mechanical, RT-6).**
- Declare `underlying_type`, `contract_type`, base and multiplier as **instrument identity**, with `known_at` = `first_bar_ts`. An L1 whitelist lists exactly these fields.
- Unknown type → excluded (fail closed; see the R-3 comment).
- A type change over an instrument's life (pre-market → perpetual) → the instrument is eligible only from the change date where it's known, otherwise excluded.

### M-5: L-265 BTC hedge is structurally almost infeasible, and branch 2 lacks a filter fallback
**Evidence.**
- At E = 265 the hedge target is non-zero only when |β_book| > 13.25 USD, and |H_B| ≤ 132.5 USD.
- BTCUSDT `stepSize` is 0.001 BTC. BTC closed between 34,183 and 111,846 USD from 2023-11 to 2025-06 (`W_DEV`), so one step is 34–112 USD.
- `MIN_NOTIONAL` is 50 now, and was 100 in the 2023-11 Wayback capture.
- So any target in roughly (13, 50–112) USD is infeasible. Under §14 ("a required hedge is infeasible → the beta rule forbids the entries"), D1 ≥ 90 % is very likely to fail mechanically.
- Legs are 26.5 USD each.
- Branch 2 ("current filters") is undefined for symbols absent from current `exchangeInfo`. At least 27 `W_DEV` delistings are absent (`bar_audit.json`).

**Why it matters.** The user should know before freeze that the deployability layer probably can't pass at 265 USD under the frozen hedge rule. The likely end state is "GO — not deployable at 265 USD", whatever the science says.

**Resolution (user decision, RT-3).** Either:
- accept this; or
- pre-register an L-265-only hedge rounding rule (nearest feasible size; 0 allowed with a residual-beta tolerance); or
- change E for the deployment gate.

In every case, add a fallback for missing current filters, such as the last available Wayback capture, else exclusion. None of this touches L-norm or P1–P11.

### M-6: S-5.5's "reopened before freeze" rule can't be enforced
**Evidence.**
- S-5.5: "> 0.5 % of `W_DEV` labels invalid → Stage 0 data decisions reopened **before freeze**."
- Labels may only be computed after the code freeze (spec header and §18), so the condition can't be evaluated before freeze.

**Resolution (RT-5).** Either:
- **(a)** run an outcome-free **label-availability audit** before freeze. It checks only whether P(`t_e`), P(`t_x`), the funding records in (`t_e`, `t_x`] and the mark fallbacks exist, never their values. This needs the 1-minute data; or
- **(b)** rewrite the rule as a post-freeze stop: > 0.5 % → K2-type stop and a user decision.

### M-7: Truncated monthly archive files, with no source-merge rule for R3/R5
**Evidence.**
- See E-2: 2022-02-26 → 28 (48 symbols) and 2022-04-01 → 02 (49 symbols) are missing from the monthly 15-minute files, while the daily files exist.
- The 1-minute monthly files (R5) may have the same defect. I couldn't check without downloading them.

**Why it matters.**
- U4 fails for about half the universe for roughly 4 days.
- `t_e` / `t_x` reference prices are missing, which adds to S-5.5's invalid-label count.
- The Stage-0 gap statistics overstate the holes.

**Resolution (mechanical).**
- Pre-register R3/R5 = monthly ∪ daily archive files, checksum-verified, with conflicts resolved to the monthly file and logged.
- Re-run the S0-9 gap count on the merged source.
- REST klines are optional, as a third source for `W_DEV` only.

---

## Minor

| ID | Finding | Evidence | Proposed resolution |
|---|---|---|---|
| m-1 | U3 "no gap > 1 nominal interval" has no tolerance. Settlement timestamps jitter by up to a few ms, so a literal reading fails every record | `funding_audit.json` (offset ≤ 1 s); audit used + 60 s | Pre-register spacing ≤ nominal + 60 s, with nominal = max of the adjacent intervals (R-2) |
| m-2 | U4 denominator is ambiguous: 95 bars fully inside (`t` − 24 h, `t` − 1 min] or 96 bars to the hour start | `d0_eval.py` uses 96 / ≥ 92; my strict 95 / ≥ 91 variant gives the same D0 | Fix one definition |
| m-3 | r₈ at interval-switch records: the archive labels the first post-switch record with the new interval while it covered the old spacing, so r·8/interval_h can mis-scale that record by 2–8× | 40 switch records | Normalize by min(label interval, actual spacing), or by label; pre-register (features only) |
| m-4 | Units and headers are only verified for `W_DEV`. Klines have mixed headers (10,336 / 1,976 files), aggTrades headers vary by year, and `W_HOLD` content can't be inspected before the opening (µs possible) | audit S0-9, S0-10 | Per-file unit and header detection with fail-closed refusal on ambiguity; L12 tests with ms / µs / header fixtures; any naive `bookDepth` / `metrics` timestamps → UTC if ever used |
| m-5 | R9 has `effective_from` / `effective_to` (point-in-time fees), but S-4.3 applies one current VIP 0 schedule to 2021–2026 | spec §3 vs S-4.3 | State that the current schedule is applied to all history (conservative if historical ≤ current) and keep R9 as a snapshot record |
| m-6 | The F5 fallback (mark − index)/index fails where both the premium and index klines are missing | `W_HOLD` listing: index 6 and premium 5 symbol-month holes; `W_DEV` premium 29 | State: missing → model-native missing handling (indicator plus imputation); never interpolated |
| m-7 | R1 `last_bar_ts` / last-trade for a `W_DEV`-active symbol that delists in `W_HOLD` would need `W_HOLD` content | S-2.2, R1 | Build R1 per window, with last trade censored at the window end; S-5.4 only within the window |
| m-8 | D0 margin of 1 (31 vs 30); 2 symbols within 5 % of the U2 threshold | see verification table | Record D0 = 2021-01-01 in the freeze record with the `d0_eval.elig.npz` sha256. If the implementation re-derives a different D0, raise an integrity stop, never a silent change |
| m-9 | Resources: 1-minute klines total 17.3 GB (`W_DEV`) + 9.4 GB (`W_HOLD`); about 600 MB free RAM; the VPS hosts V5 | blockers file "Resource prerequisite" | Stream and extract only the needed 1-minute rows (hh:20 opens, delisting and risk-exit windows) and delete each zip after extraction. That needs bandwidth, not 25 GB of disk. Build features per symbol in float32 partitions; never use the VPS |
| m-10 | Kraken inputs aren't audited in Stage 0 (no S0 item), but Holm family #5 includes a Kraken test | §18, S-13.3 | Pre-register: if the Kraken arm can't be built, test #5 is reported as NA and the Holm family stays at 6 (conservative) |
| m-11 | Token migrations and redenominations (MATIC→POL, FTM→S, BTT→1000BTTC, LUNA→LUNA2/1000LUNC, RNDR→RENDER…) are implicitly a delisting plus a new listing | `delisting_coverage.csv` | State this explicitly: no continuity; U1 restarts; stressed unwind where no notice is matched |
| m-12 | Missing scripts and mislabeled JSON fields (E-5) | — | Save the generating code for every Stage-0 output in the manifest |
| m-13 | `d0_eval.py` U3 bug (E-4) | — | Fix in any reuse; add a unit test where funding stops more than 7 d before `t` |
| m-14 | U5 breadth is unknown (D0 excludes U5, U6 and U7 by design). With AR on 15-minute bars, U5 pass rates could be low | S-1.8 | After the spread decision, publish an outcome-free U5 pass-rate report. It changes no K or D0 (DEC-15); it only informs |
| m-15 | `W_HOLD` terminal: daily returns at 2026-07-01 00:00 must use the flat (cash) equity, never an M6 mark | S-8.1, S-13.1 | Guard test: no price read with timestamp ≥ 2026-07-01 in `W_HOLD` accounting |
| m-16 | §3 R6/R7/R8 and the F7/F15 rows now refer to absent or dropped data; "W_HOLD uses R6 if it exists" (S-4.5) is moot | Stage-0 branch results | Mark these as resolved by Stage 0 in the freeze text (factual note, no rule change) |
| m-17 | `mark_at_settle` mixes sources: REST markPrice from 2023-10-31 (median symbol 2023-12-17) and the 15-minute mark-kline close before that | audit S0-2 | Acceptable under S-4.6. Report the switch date in the data manifest |

---

## Position on the existing blockers

| Item | Position | Comment |
|---|---|---|
| **B-1** λ | **Agree, but incomplete** | The matching undefinedness and the 1 TB scope are real. Option (c) λ = 2.0 is acceptable and outcome-blind. It doesn't resolve "R6 wherever it exists" or the cross-window source inconsistency (C-1), so the decision has to cover the source policy and the exact proxy formula too (RT-1). |
| **B-2** fees | **Agree** | Factual; it blocks freeze until the official snapshot is hashed. No workaround. |
| **B-3** relists | **Agree with (a), with amendments** | BTCST isn't a relist (it's dead since 2021-03-12, with funding on zero volume). BNT, OG, BTT and RIF old instances have no bars, so detection from klines alone misses them. U3 and the funding features must be bounded at the current instance's first bar (M-3). |
| R-1 | Agree | Needed for U4; also the reason BTCST-type contracts must never pass. |
| R-2 | Agree | Verified: 40 / 40 switch artefacts, 6 of them off-schedule. Add the m-1 tolerance. |
| R-3 | **Partly disagree** | "COIN except DOTECO" is a judgement call. S-2.1 requires `underlyingType = COIN` positively, so **excluding all 5** fails closed and costs nothing (D0 invariant). Apply the same rule to `W_HOLD` unknowns (M-4). |
| R-4 | **Agree, reversed priority** | Archive primary; REST only for the 28 missing archive months. REST is unreliable for reused names (LIT: 0 records). |
| R-5 | **Disagree (insufficient)** | The "≤ last trade" condition is look-ahead, and there's no handling of postponements or cancellations (OMG, MAVIA, BLZ) and no end to U6 (M-2, RT-2). |

## Stage-0 FAIL conditions
None is hit. D0 = 2021-01-01 and the 10 folds were independently reproduced. Funding, the symbol history and the bars can be reconstructed, subject to M-3 and M-7.

**Recommendation: NOT READY FOR SPEC FREEZE — BLOCKERS** (C-1 / B-1, B-2, and the user decisions RT-1 to RT-6).
