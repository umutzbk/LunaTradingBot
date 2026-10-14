# V7 pre-freeze red team: round 3 (independent, adversarial)

- **Date:** 2026-10-14
- **Subject:** spec DRAFT 2 including the FINAL PRE-FREEZE PATCH (DEC-35 … DEC-42, R2-5 deletion), uncommitted: `docs/superpowers/specs/2026-10-14-luna-v7-ml-research-design.md` (1,046 lines). Diffed against `scratchpad/spec_before_r2_patch.md`. Line numbers below refer to the patched spec.
- **Also read:** red-team rounds 1 and 2, `BLOCKERS_FOR_USER.md`, `INCIDENT-2026-10-14-seal.md` (read only, not edited), `scripts/rt5_v2_dec35.py`, `scripts/rt5_availability.py`, `scripts/wayback_notice_lag.py`, the rt5v2 test output and the CRAN `bidask` 2.1.5 source (`R/ar.R`).
- **Boundaries kept:** no network access of any kind; no label, return, P&L, IC, Sharpe, backtest, model fit or spread level on real data; nothing dated ≥ 2025-07-01 read; nothing in `W_HOLD` / M6 touched; spec, incident file and data not edited; nothing committed; the running RT-5 process was not touched.
- **Outcome-free spot checks** (scratchpad `r3/`): `r3_deaths.py` (instance ends vs G_D0 eligibility, volume > 0 flags only), `r3_expost.py` (U8 vs ex-post instance end), `r3_recon.py` / `r3_recon2.py` (funding reconciliation, timestamps and interval fields only; rates not used), `r3_lead.py` (leading no-trade bars).
- **Severity discipline:** the user's decisions are authoritative. A finding is Major only where the text as written would leak, could not be implemented, or contradicts itself or a user decision.

---

## 1. Verification of the user's decisions

| Decision | Spec location | Faithful? | Notes |
|---|---|---|---|
| R2-1 U4 stays a coverage gate | l.187, l.207, DEC-28 l.974 | **yes** | |
| R2-1 U8 (volume > 0 15m bar closing within 60 min, else no entry/increase; U2 stays the long-horizon gate) | l.211, l.205, S-7.1 l.444, S-7.3 l.475, L2 l.647 | **yes** | Window [t − 60 min, t − 1 min] is correct PIT (bar close = known_at). Dead contracts can't enter: verified on W_DEV (rt5v2 test: `entry_halted` 267 → 0 after U8 + instance U1) |
| R2-1 instance boundary: 7 consecutive UTC days without a volume > 0 15m bar; zero-volume bars / funding don't prevent it; new instance at the first later volume > 0 bar; first_bar_ts, U1, U3, features reset; dormant data never bridges | l.180–186, S-2.6 l.249, §9 l.539, L2 | **yes**, with one PIT defect | The **end** of an instance is an ex-post fact used at decision time (M3-2). Relists: 6 split symbols in the eligible set (BNX, CTK, CVC, ICP, MAVIA, TLM; AERGO is excluded by DEC-30) — all 7 round-2 cases covered |
| R2-1 HALTED after 60 min without volume > 0; no retroactive fill; no orders/resizes; wait for the first executable volume > 0 bar or an official settlement price; stressed taker exit; none by terminal unwind → invalid, no synthetic fill | S-5.4 l.388–401, S-7.1 l.453, S-7.3 l.474–475 | **yes (literal)** | Two editor operationalisations make the "invalid" branch the norm for every delisting held without a PIT-known notice, and its W_DEV consequence is undefined (M3-1) |
| R2-1 re-run the structural audit; the 111 exit ≤ entry cases must disappear or be invalid | S-5.5 l.406, S0-14 l.911 | **yes** (see §4) | |
| R2-2 content known only from the earliest archived snapshot containing it; known_at = max(releaseDate, capture); no snapshot → never backdated; revisions = new versions; forward known_at = recv_ts + hash/time/source | S-2.5 l.217–222, R11 l.275, DEC-36 | **yes** | Wayback captures of the client-rendered announcement pages may not contain the body; `wayback_notice_lag.py` checks capture existence only (m3-8). Fail-closed, so not a leak, but it feeds M3-1 |
| R2-3 scheduled_delist_ts only if one exact timestamp maps to the exact instance, else null; entries blocked from known_at; open position exits at the next normal opportunity; postponement/cancellation lifts U6 from its known_at; releaseDate ≤ last-trade logic removed; 72 h clear only with unique ts | S-2.5 l.215, l.228–237, S-7.3 l.474, DEC-37 | **yes** | 72 h test operationalised as "not HALTED at any time in [ts, ts + 72 h]" (neutral). Edge case known_at > ts + 72 h unaddressed (m3-6) |
| R2-4 proxy only, λ = 2.0 in all windows, R6 diagnostic, not called conservative; published AR at window level; full spread / 2, × λ, 1 bp floor; hand-worked + synthetic tests | S-4.5 l.319–337, L8, DEC-38 | **yes** | Checked against `bidask/R/ar.R`: `s2 = 4*(c1-m1)*(c1-m2)` with `c1`,`m1` the previous bar and `m2` the current bar = l.327 π_k; `rmean` then `sign(ar)*sqrt(abs(ar))` → with negatives set to 0 equals l.329. "Up to 95 pairs" re-verified for τ = hh:05 and hh:20. **Exactly one estimator definition** (S-4.5); S-4.4, F6, F14, A0, S-6.2, DEC-22/38 only reference it |
| R2-4 P6: fees × 1.5, slippage × 1.5, spread × 3.0, stressed mean daily net ≥ 0; old and new side by side; only revised normative | P6 l.718, S-13.4 l.747, §15 l.828, DEC-38, history l.996–1001 | **yes** | Consistent everywhere P6 is used, incl. the §15 GO list. Repricing semantics is an editor choice (§2) |
| R2-5 remove the post-freeze K8/K2 sentence | S-5.5 l.406, DEC-27 l.973, l.994 | **yes** | |
| R2-6 forward evaluable quarter = primary replay inputs only; book/ticker never make a day non-evaluable; ≥ 90 % and 144/180 kept | §15 l.811–816, l.849, l.855, DEC-42 | **yes** | Exchange filters required only for D1–D3 = faithful reading of "where applicable". R11 is listed as a required input but its recording conflicts with the CMS block (M3-3) |
| R2-7 endpoint allowlist; CMS fully blocked before opening, no download-then-filter; Wayback < 2025-07-01 only; current exchangeInfo schema/filters only; W_HOLD filters at opening; forward filters from T_F7; guard tests; incident immutable; no reset unless outcome contamination | S-1.7 l.126–150, L10, S-14.1 step 6, DEC-41 | **partly** | Intent faithful; two enforcement holes (M3-4). Identity fields from current exchangeInfo and the CMS block "while M6 is sealed" go beyond the decision (§2) |
| R2-8 fees provisional until an official non-empty snapshot; freeze not permitted before it; 0.0200 / 0.0500 %, BNB off; no tier shopping; zero-byte files never evidence | S-4.3 l.300–307, S0-8, DEC-39, §20 l.1005 | **yes** | One unambiguous freeze requirement (l.303). Snapshot still absent: `var/v7/stage0/fees/futureFee.html` and `enBH_futureFee.html` are 0 bytes |
| R2-9 fill-time proxy PIT at fill (bars known by fill − 1 min); confident match; off-schedule ±60 s → invalid, never normalised; missing proxy rules | S-4.4 l.311–315, S-2.5 l.223–227, S-2.6 l.250–255, DEC-40 | **partly** | Proxy, match and missing-proxy rules faithful. The reconciliation text dropped DEC-29's mechanical inference and cascades (M3-5) |

**The eleven required checks**

| # | Check | Result |
|---|---|---|
| 1 | Dormant/dead contracts can't enter | **yes** (U8 + halt + instance U1); W_DEV test confirms |
| 2 | Relists create clean instances | **yes** |
| 3 | No rolling feature/funding crosses an instance boundary | **yes** in the text (l.184–185, l.249, l.539); U1 = 60 d also bounds every ≤ 30 d look-back incl. U2 and β̂. But the funding reconciliation chain (M3-5) and the ex-post end (M3-2) need fixing |
| 4 | No exit ≤ entry structural label survives | see §4 |
| 5 | Delisting logic fully PIT | **yes**, except that `last_trade_ts` (instance end) has no `known_at` (M3-2) |
| 6 | Mutable CMS text never backdated | **yes** (l.218–219) |
| 7 | Exactly one spread estimator definition, = published AR | **yes** |
| 8 | Revised P6 internally consistent incl. §15 | **yes** |
| 9 | Forward evaluability independent of R6 | **yes** (l.813); R11 conflict M3-3 |
| 10 | Pre-holdout guards would actually refuse CMS / Wayback / W_HOLD | CMS and W_HOLD archive content: yes. Wayback captures and `fapi`: **not as specified** (M3-4) |
| 11 | S-4.3 has one unambiguous fee-freeze requirement | **yes** (wording nit m3-7) |

---

## 2. Editor choices the user did not explicitly make

| # | Choice | Verdict |
|---|---|---|
| E1 | Halt measured on 1-minute bars (l.389), U8 on 15-minute bars | **Neutral.** Matches the round-2 recommendation. Windows are offset (m3-2) |
| E2 | Waiting exit fills at the **close** of the first volume > 0 1-minute bar, at its close time (l.392) | **Faithful / neutral:** resumed trading is only known when the bar closes; PIT-correct |
| E3 | U6 doesn't truncate labels (l.238) | **Neutral.** Labels exist only for candidates with U6 inactive at `t`; a label is a fixed-horizon measurement |
| E4 | P6 = repricing of the same executed book (decisions, quantities, fill times, reference prices unchanged) | **Neutral, standard, deterministic** (a re-run would move U5, A0 and F14 too). One-line ratification recommended but not blocking |
| E5 | Decision exits and label exits also use the missing-proxy fallback (l.314) | **Neutral:** consistent with S-7.3 "exits still run" |
| E6 | CMS blocked also at the `W_HOLD` opening "while M6 is sealed" (l.142) | **Stricter than decided; needs ratification** — and it contradicts the forward announcement recorder (M3-3) |
| E7 | First instance starts at the first archived bar (l.183) | **Neutral:** zero W_DEV impact (0 symbols with ≥ 1 h of leading no-trade bars, `r3_lead.py`) |
| E8 | Current `exchangeInfo` immutable identity fields allowed (l.136, DEC-41) | **Needs ratification:** R2-7 said "only schema / filter feasibility". Identity of a currently listed instance doesn't reveal status, but sourcing identity from "currently listed" makes the DEC-30 exclusion correlate with later delisting (mild survivorship; 5 W_DEV symbols) |
| E9 | Reconciliation definition (l.250, l.252) | **Not neutral — must be fixed** (M3-5) |
| E10 | "A fill can't come from another instance" (l.396): an exit waiting across a ≥ 7-day dormancy is invalid even if trading resumes before the window end (e.g. MAVIA 2024-12-16 → 2025-03-26) | **Needs ratification:** resolves a tension between "first subsequent executable bar" and "never bridge instances" in the direction of invalidity |
| E11 | "§3 has no official settlement table, so only the executable-bar path exists" (l.395) | **Needs ratification:** it empties the user's second exit path; consequence in M3-1 |
| E12 | 72 h clear = "not HALTED at any time in [ts, ts + 72 h]" | Neutral |
| E13 | U6 exit at the first decision ≥ known_at + 1 min, filled at t + 15 min (l.474) | Faithful ("next normal decision") |

---

## 3. Findings

### Critical

None.

### Major

#### M3-1: A position held into an instance end (delisting without a PIT-known notice) makes the run `INVALID RUN`; the W_DEV consequence is undefined and the event is common
**Evidence (spec):** l.395 (no official settlement source → executable-bar path only), l.396 (no fill from another instance), l.397 (no executable exit → label invalid; executed-book run `INVALID RUN`). S-13.4 / K8 define `INVALID RUN` for the holdout only; S-10.6 and S-10.7 (the six W_DEV book backtests and the development kill) have no rule for an invalid run. U6 can prevent the hold only when a matched notice has an archived capture *containing the content* before the death (DEC-36).

**Evidence (W_DEV, outcome-free, `r3_deaths.py`):** in the out-of-fold book span 2023-01 → 2025-06 there are **44 instance ends, 16 of which were G_D0-eligible (U1–U4, incl. U2 ≥ 20 M USD) right up to the end**. For 14 of them the last eligible decision was the 08:05 decision 0.67–0.92 h before the final traded bar (deaths at 08:45 / 09:00): COCOS, TOMO, STRAX, WAVES, AGIX, RNDR, MATIC, UNFI, XEM, DAR, FTM, BNX (2025-03), VIDT, EOS. The other two are BNX (2023-01, 7.7 h) and MAVIA (113 h). 15 of the 16 have no matched catalog notice in the Stage-0 coverage table, and under DEC-36 even matched notices count only from a content-bearing Wayback capture. The rt5v2 4-symbol test already shows 69 `no_executable_exit` labels.

**Why it matters (implementability, not a leak):** a 10-leg book that ranks on funding has a material chance of holding one of these ~16 contracts at its death in each of the six S-10.6 runs, and similarly in `W_HOLD` and forward. Each such event ends the run as `INVALID RUN`, with no defined continuation on W_DEV. In addition, the labels of these contracts are invalid and excluded from fitting, so the model never sees the cost of holding into a delisting, while the book is invalidated by it.

**Minimal fix (user decision; the R2-1 rule itself stays):**
1. State what an `INVALID RUN` in a W_DEV S-10.6 / S-10.7 run means. For example: a K2-type stop with an explicit new-spec decision, which is consistent with "no synthetic fill".
2. Decide whether the user's second path ("official settlement / delivery price under frozen data rules") is to be populated. This needs an outcome-free Stage-0 check for a timestamped official Binance source, plus a §3 table. Otherwise ratify E11 knowingly.
3. Before freeze, run an outcome-free exposure count: instance ends with G_D0 ∧ U8 eligibility within h\* before death and no content-confirmed archived notice known by the last decision. Then the decision rests on numbers.

#### M3-2: The instance **end** (`last_trade_ts`) is ex-post information used at decision time
**Evidence:**
- l.182: the instance "ends at its last volume > 0 15-minute bar before a dormancy". The dormancy is only established 7 days later.
- l.185: dormant-interval data "never enter a feature, gate". U1 (l.204) uses "the current instance".
- R1 (l.265) gives `last_trade_ts` no `known_at`.
- So a decision within 60 min after a contract's final trade has, ex post, no current instance and fails U1. A live system would see U8 = true and could enter.
- The RT-5 v2 script implements exactly this (`inst_of` → −1 → U1 false).
- `r3_expost.py`: among 513,427 U8-true G_D0 decisions, **2** are removed only by the ex-post end: **LUNAUSDT 2022-05-12 16:05** and BNXUSDT 2023-02-01 00:05.
- The bias is selective: it removes entries into contracts that are about to die, which are exactly the M3-1 cases.

**Minimal fix (one sentence, no economic change):**
- R1 `last_trade_ts` has `known_at` = close of the last volume > 0 bar + 7 d.
- At `t`, the current instance is the latest instance with `first_bar_ts` ≤ `t`. Its end is never used by a gate, feature or candidate filter before its `known_at`; until then, activity is controlled only by U8 and S-5.4.
- Add an L2 test.

#### M3-3: The CMS block "while M6 is sealed" contradicts the forward announcement recorder and R11 forward evaluability
**Evidence:**
- l.142: "The mutable CMS stays blocked while M6 is sealed".
- l.137: CMS "nothing" allowed.
- But §15 l.801 records "immutable snapshots of new Binance futures announcements" from `T_F7`, and `T_F7` lies inside M6 (l.809).
- l.222 and R11 give these snapshots `known_at` = `recv_ts`, and l.812 lists R11 as a required primary replay input for evaluability.
- As written, either the guard refuses the recorder (forward U6 can't exist, and R11 is "missing"), or the recorder violates S-1.7.

**Minimal fix:**
- Carve-out: the forward recorder may poll the announcement endpoints from `T_F7` as S-1.3 outcome-free raw recording (write-only, hashed, `recv_ts`, nothing derived or displayed before L7).
- The research fetch layer keeps the CMS blocked until R_V6.
- Add a guard test that research code can't read the recorder's R11 before L7.
- Also state that an R11 quarter with no new announcement isn't "missing".

#### M3-4: Two holes in the pre-opening allowlist (S-1.7 l.135, l.138, l.141)
- **(a) `fapi` is allowed by a parameter, not by endpoint.** The rule is "requests with a server-side `endTime` < 2025-07-01". Current-state endpoints (e.g. `premiumIndex`, `ticker/*`, `fundingInfo`, `openInterest`) ignore `endTime` and return live M6 market data while passing the rule.
  - **Fix:** enumerate the permitted `fapi` paths that honour `endTime` (the kline families, `fundingRate`, `aggTrades` as needed). Refuse every other path. Add a guard test for a current-state path carrying an old `endTime`.
- **(b) A Wayback capture can't be refused "before any network I/O" by its requested timestamp.** `web.archive.org/web/<ts>/<url>` redirects to the *nearest* capture, which can be dated ≥ 2025-07-01. That is a download-then-filter path, which R2-7 forbids.
  - **Fix:** fetch only exact (timestamp, URL) pairs returned by a server-bounded CDX query, in `id_` form, with redirects disabled.
  - Check the response's capture timestamp (Memento-Datetime / final URL) before parsing; discard and log otherwise.
  - Add a guard test "redirect to a later capture".

#### M3-5: The S-2.6 reconciliation text cascades, silently drops DEC-29's mechanical inference, and isn't what RT-5 audited
**Evidence:**
- l.250: a record without an explicit interval inherits "the previous record's nominal interval when the record is reconciled with it".
- l.252: the first record of an instance is reconciled only with an explicit interval.
- So once one record in a REST-only (no explicit interval) run is unreconciled, every later record in that run has no nominal interval and is also unreconciled.
- DEC-29 (user) said "mechanical inference if missing"; the patched l.250 removed it.
- `r3_recon.py` (W_DEV, timestamps and interval fields only): **spec-literal 4,885 unreconciled records vs 33 under the RT-5 v2 implementation**, which uses DEC-29's spacing-based inference. The literal rule invalidates the whole first instances of TLM (2021-07 → 2023-02, 1,778 records), ICP (2021-05 → 2022-08, 1,433) and BNX (2022-03 → 2023-01, 1,006), plus OMG, BTT, RIF, BNT, UNFI and others.
- D0 is unaffected: the 2020-12-28 cascades are symbols that fail U1 in 2021-01.
- **This decides RT-5 (`r3_recon_labels.py`, an upper bound on the G_D0 grid):** under the literal text, 7,179 more labels are invalid (TLM 2,544, ICP 3,156, BNX 1,446, OMG 33). That is ≈ 0.47 % of 1,536,297 on its own. Added to the run's 831 (0.054 %), the total is ≈ **0.52 % > 0.5 %**, so under DEC-27 the frozen text would reopen the data decisions. RT-5 passes only under the DEC-29 reading that the script implements.

**Minimal fix (restores DEC-29 inside DEC-40):**
- A record without an explicit interval has nominal interval = the standard interval (1, 2, 4 or 8 h) matching its spacing to the previous record within ±60 s.
- A record is reconciled when its spacing matches the previous record's nominal interval or its own explicit interval, within ±60 s.
- An unreconciled record is invalid and reported, but it still passes its mechanically inferred (or, if none, the previous) nominal interval to its successor.
- State the rule so the frozen text matches what S0-14 measured.

### Minor

| ID | Finding | Proposal |
|---|---|---|
| m3-1 | S-5.4 "a label whose instrument is halted at `t_e`" doesn't say whether the BTC pro-forma hedge reference is covered. The audit treats a halted BTC as invalid (3 BTC labels in the test run) | State that it applies to B(τ) too |
| m3-2 | U8 (15m bar closing in [t − 60, t − 1]) and the entry halt test at `t_e` (1m in [t − 45, t + 15)) use different windows, so some U8-passing candidates get "no order" / invalid labels | Acknowledge; no change needed |
| m3-3 | F6 "its 7 d median" doesn't fix the sampling grid of ĥ (decision times vs every 15 min) | Fix the grid (e.g. ĥ at each 15m close known by t − 1 min) |
| m3-4 | Exchange-supplied flat zero-volume bars count as present (U4) and enter 𝒫(τ) with π = 0, pulling Ŝ toward the floor. U8, U2 and max(h_fill, 25 bps) on halted exits limit the effect | Acknowledge in S-4.5 text |
| m3-5 | l.142 "where populated, each article's `lastUpdateTime`": the CMS is blocked at the opening, so the field can only come from inside archived captures | Reword |
| m3-6 | 72 h clear when known_at > scheduled_delist_ts + 72 h is unaddressed | State: evaluated at known_at; U6 then never activates if not halted in the 72 h |
| m3-7 | l.303 "Freeze requirement (the only one)" could be read as the only freeze precondition overall | "the only fee-related freeze requirement" |
| m3-8 | `wayback_notice_lag.py` converts with `time.mktime(...) − time.timezone`, which is 1 h off for summer timestamps on a BST machine (use `calendar.timegm`). It measures capture existence, not that the capture contains the notice text, which DEC-36 requires. Result file (`wayback_notice_lag.json`): 46 / 47 notices have a capture before 2025-07-01, median lag 4.1 h, 38 within 24 h. But **8 lags are negative** (a "capture" before `releaseDate`), so the `original:.*code.*` filter also matches pages that can't hold the notice. Three lags exceed 37 d | Fix the conversion; verify content containment (outcome-free) before relying on U6 coverage. Under `known_at` = max(…) nothing is backdated, so this isn't a leak |
| m3-9 | `INCIDENT-2026-10-14-seal.md` is untracked; "unchanged" isn't verifiable yet. Today: SHA-256 `beb3cb085397c6f5d8b2481bf899ce304b202b16a5679c9a5d1e32c85152380e`, 7,550 bytes, mtime 2026-10-14 11:11:49 +01:00 | Record the hash in the freeze record |
| m3-10 | `data.binance.vision` prefix listings return M6-dated key names; the spec is silent (the incident file shows they were dropped in memory) | State "M6 keys dropped before storage or display"; add a guard test |
| m3-11 | In rt5v2, `new.exit_at_or_before_entry` is 0 by construction (the exit time is `t_x` or a bar ≥ `t_x`); the substantive check is the `old111` classification | Report it as such |

---

## 4. RT-5 re-run verification (DEC-35)

**Script review (`rt5_v2_dec35.py`, unmodified by me).**
- The U8 window (bar open in [t − 75, t − 16] min ⇔ close in [t − 60, t − 1]) is correct.
- The halt test (1m volume > 0 bars opening in [τ − 60, τ)) is correct.
- The waiting exit (first volume > 0 1m bar ≥ `t_x`, same instance, < 2025-07-01, filled at its close) is correct.
- Instance U1 is correct.
- The mid-run vectorisation (`exact_x = v1.present(t1, lx)`) is equivalent to the per-label `np.isin`, because `t1` is sorted by `minutes_1m` / `add_daily`.
- Data requests stay inside W_DEV.
- **Two deviations from the frozen text:**
  - reconciliation uses DEC-29 inference, not the literal l.250 (M3-5);
  - U1 uses the ex-post instance end (M3-2; affects 2 decisions).

**Result (`var/v7/stage0/rt5v2/summary.json`, 381 symbols, W_DEV, h ∈ {24, 72, 168}):**

| Quantity | New population (G_D0 ∧ instance U1 ∧ U8) | Old population under new rules |
|---|---:|---:|
| labels | 1,536,297 | 1,538,625 |
| invalid | **831 (0.054 %)** | 2,598 |
| entry_halted | **0** | 1,764 |
| no_executable_exit (halted at `t_x`, no executable bar before instance end / window end) | 716 | 2,483 |
| exit_waited_filled | 0 | 0 |
| funding_missing / funding_unreconciled | 172 / 181 | 202 / 181 |
| mark_missing, entry/exit gaps, entry proxy missing | 0 | 0 |
| exit_proxy_fallback (reported, valid) | 1,074 | 1,158 |
| btc_input_missing | 291 | — |
| exit_at_or_before_entry | 0 (by construction, m3-11) | 0 |

- **`old111`:** n = 111. **All 111 are excluded by the new population** (U8 / instance U1); 0 remain valid and 0 remain invalid. So the structural exit ≤ entry labels are gone. They can't be split between U8 and the ex-post U1, but M3-2 affects only 2 decisions overall.
- **Invalid rate 0.054 % < 0.5 % → RT-5 PASSES as implemented.**
  - By year: 2021 66, 2022 344, 2023 79, 2024 214, 2025 128.
  - Worst symbols: ANC (30 / 54), VIDT, COCOS, DAR, STRAX, BNX. These are the deaths of M3-1.
  - Every halted-at-`t_x` label was a death (716 no executable exit, 0 filled).
- **Caveat:** the pass depends on the M3-5 fix. Under the literal l.250 the rate is ≈ 0.52 %.
- **BTC:** 3 BTC labels with BTC halted at `t_e` (m3-1); 291 leg labels lack BTC inputs.
- **Activity instances:** BNX, CTK, CVC, ICP, MAVIA, TLM are split. MAVIA ends 2024-12-16 09:15 and restarts 2025-03-26 17:00, matching the spec text.
- **`d0_recheck`:** the first month with min ≥ 30 is **2021-01 (min 31)**. **D0 = 2021-01-01 and 10 folds hold.**

---

## 5. Seal incident: is a reset needed?

**No reset is needed.**
- Nothing since round 2 adds outcome exposure.
- The rt5v2 and wayback scripts request only W_DEV content: months capped at 2025-06, daily files only for required W_DEV minutes, and CDX bounded at `to=20250630235959`.
- No rule in the patched spec cites exposed M6 information.
- The MAVIA / OMG / BLZ examples are W_DEV facts; MAVIA's last volume > 0 bar at 2024-12-16 09:00 is confirmed.

Conditions:
1. The incident file is included unchanged with its hash (m3-9).
2. The M3-4 enforcement fixes are made, because the Wayback-redirect path is a residual breach route.

---

## 6. Summary

- **Critical: 0. Major: 5** (M3-1 … M3-5). **Minor: 11.**
- **Decisions R2-1 … R2-9:** R2-1 … R2-6 and R2-8 faithful. R2-7 partly (M3-4, plus editor choices E6 / E8). R2-9 partly (M3-5).
- **RT-5:** passes as implemented (0.054 %, all 111 old exit ≤ entry cases gone), conditional on the M3-5 fix. D0 = 2021-01-01 holds. No seal reset is needed.
- **Remaining blockers:**
  - **M3-1** (user decision: the W_DEV consequence of `INVALID RUN`, and the settlement-price path);
  - **M3-2, M3-3, M3-4, M3-5** (text fixes inside the user's decisions; M3-3 also needs ratification of E6);
  - **R2-8 / DEC-39** (operational: the official VIP 0 fee snapshot; both files are still 0 bytes; the spec itself forbids freeze without it).
- **Ratifications requested:** E6, E8, E10, E11 (E4 optional).
- **R2-8 is not the only remaining blocker.**

NOT READY — BLOCKERS
