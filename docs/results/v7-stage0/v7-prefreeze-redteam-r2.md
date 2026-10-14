# V7 pre-freeze red team: round 2 (independent, adversarial)

- **Date:** 2026-10-14
- **Subject:** spec DRAFT 2 including the PRE-FREEZE BLOCKER RESOLUTION PATCH (DEC-22 … DEC-34), uncommitted: `docs/superpowers/specs/2026-10-14-luna-v7-ml-research-design.md` (958 lines). I diffed it against the pre-patch copy `scratchpad/spec_before_blocker_patch.md`.
- **Also read:** round-1 red team, `BLOCKERS_FOR_USER.md` (with its resolution table), `INCIDENT-2026-10-14-seal.md`, the Stage-0 audit, and the Stage-0 summaries.
- **Boundaries kept:**
  - I worked read-only; the only writes are this file and an append to `BLOCKERS_FOR_USER.md`.
  - I computed no label, return, P&L, IC or Sharpe, ran no backtest, fitted nothing, and did no spread-understatement computation. I computed no spread-proxy level on real data.
  - I downloaded nothing and made no CMS, catalog or Wayback request. I read nothing dated ≥ 2025-07-01.
  - **Spot checks** used only the local `W_DEV` archive data (`var/v7/stage0/k15_last`, `funding_archive`, `funding_rest`, `delist/bodies`, `summary/d0_eval.elig.npz`). They covered bar presence, volume > 0 flags, funding-record counts, and the structure of notice text (regex counts and field names only).
  - **The proxy-property check (M2-3)** is a purely synthetic simulation with no market data.
  - My scripts are in the session scratchpad (`r2_dorm.py`, `r2_onset.py`, `r2_sim.py`).
- **The user's decisions are taken as authoritative.** Where a decision interacts badly with the rest of the spec, I flag the interaction and propose options that leave the decision itself intact, unless the user chooses otherwise.

---

## 1. Verification of the user's decisions

| Decision | Spec location | Faithful? | Notes |
|---|---|---|---|
| B-1/RT-1: proxy only in every window for F6/F14, U5, A0, labels and the executed book; R6 diagnostic only | S-4.4, S-4.5 l.273, R6 row l.229, L8, S-6.2 l.361, F6, F14, A0 | **yes** | Every R6 (book_top) use is removed. S-5.4's "max(h_a, 25 bps)" now operates on the proxy. Name collision with R6 = V6 release (m2-1) |
| λ = 2.0 fixed | S-4.5 l.275, L8, DEC-22, Stage-0 branch table | **yes** | The λ test appears only as "superseded" (S0-5, l.846, DEC-11 row) |
| Abdi–Ranaldo full proportional spread /2; negatives → 0 before aggregation; × λ; then 1 bp floor | S-4.5 l.278–281 | **yes** | Log formula matches AR (2017) eq. for two-period S². Order is correct. Statistical consequence in M2-3 |
| Decision-time value = median of proxy observations fully known by t − 1 min | S-4.5 l.281–282 | **yes**, plus additions | The editor added a 24 h window (it was in the pre-patch S-4.4, so not new), a consecutive-bar rule, and "no pair → missing → staleness" (§2) |
| Documented log formula + unit tests | S-4.5 l.283 | **yes** | |
| B-2: maker 0.0200 % / taker 0.0500 %, BNB off; local snapshot before freeze as operational item | S-4.3, S0-8, DEC-23 | **partly** | The numbers are correct. The wording "must exist before freeze" vs "operational blocker only" is ambiguous about whether freeze may proceed without it. **The snapshot still does not exist:** both files in `var/v7/stage0/fees/` are 0 bytes (M2-5) |
| B-3: new instance after ≥ 7 consecutive calendar days with no archived 15m bars, then resumed trading; U1/U3/features reset; no bridging; no synthesis | S-2.2 l.166–172, S-2.6, §9, R1/R2, DEC-24 | **yes (literal)** | **Interaction defect:** 4 of the 7 `W_DEV` dormancy/resumption cases are not split, because the archive keeps publishing zero-volume bars (C2-1) |
| RT-2/R-5: PIT U6; activate at matched notice; deactivate on postponement/cancellation or 72 h of continued volume > 0 trading after the scheduled time; halt = 60 min without a volume > 0 bar after the scheduled time with no postponement known; no last-trade conditions | S-2.5, U6, S-5.4, DEC-26 | **partly** | The rule is implemented. Three gaps: "notice text known at `releaseDate`" is unobservable (M2-1); the scheduled-time parse is undefined (M2-2); the halt bar granularity is unspecified (m2-13). The OR with `last_trade_ts` in S-5.4 is an outcome definition, not a notice-validity condition (m2-14) |
| RT-3: accept granularity; E, rounding, hedge rule and D1 unchanged; missing filter → timestamped historical capture, else exclude & flag | S-14.1 steps 6 and 7, DEC-34 | **yes** | "At or before the relevant period" is a harmless tightening. The timing of capture retrieval for `W_HOLD` symbols interacts with the seal (M2-6) |
| RT-4: record the incident; S-1.7 strict; one pinned capture at opening; collector-integrity monitoring separated | S-1.3 l.98, S-1.7 l.129–131, DEC-25, INCIDENT file | **yes** | Enforceability gap (M2-6). `lastUpdateTime` is empty in every stored body (m2-20). The incident's "mid-difference tolerances" aren't in the S-1.3 allow-list (m2-21) |
| RT-5: option (a), outcome-free `W_DEV` input-availability audit before freeze | S-5.5 l.348, S0-14, DEC-27 | **partly** | Option (a) is implemented, but the editor also added a post-freeze "K8 / K2-type review" rule the user did not choose (M2-4). **The audit has not been run:** `rt5-input-availability.md` doesn't exist, although the blocker table points to it (M2-5) |
| RT-6: L1 identity whitelist only for verified-invariant identity fields at the instance's `first_bar_ts`; time-varying fields keep timestamps; unknown → exclude | R1 l.224, L1 l.586, S-2.1 l.160, DEC-31 | **yes** | "Verified invariant" is undefined (m2-19) |
| R-1 rejected: zero-volume exchange bars count as present for U4 | U4 l.190, DEC-28 | **yes** | Its stated rationale ("activity is controlled by U2") fails for up to about 15 days after trading stops (C2-1) |
| R-2: gap = spacing > max(prev, cur) + 60 s; explicit interval field for r₈; mechanical inference; off-schedule flagged | U3 l.189, S-2.6, §9, DEC-29 | **partly** | The rule is implemented. "Fail/flag" goes beyond the user's "flagged" (m2-6). "Locally established interval regime" is undefined (m2-7) |
| R-3: exclude the 5 unknown-type symbols | S-2.1 l.160, DEC-30 | **yes** | D0 is invariant: DOTECO and LEND are never eligible in 2021-01 (`d0_eval.elig.npz`) |
| R-4: archive primary, REST fills gaps, conflicts → blocker, instance-aware | S-2.6, R2 source column, DEC-32 | **yes** | |
| M-3: known limitation | S-2.2 l.172, DEC-32 | **yes** | |
| M-7: monthly ∪ daily by primary key, conflicts reported | S-2.2 l.179, R3, R5, DEC-33 | **yes** | |

**Leftover references:**
- R6 as a spread source: none remain.
- The λ test: only as "superseded".
- Provisional fees: none remain.
- Last-trade-based notice validity: none remain (S-2.5 explicitly replaces it).
- Pre-instance semantics: none remain in §2, §3 or §9.
- The R3/R4/R5 keys still lack `instance` (m2-18).

**D0 re-verification under DEC-24/28/30/33:**
- No instance split occurs before 2022-08.
- The excluded symbols were never eligible in 2021-01.
- The U4 change can only add bars.
- The 2022 monthly-tail merge does not touch 2021-01.
- Recount from `d0_eval.elig.npz`: the minimum over 2021-01 is still **31**. **D0 = 2021-01-01 (10 folds) stands.**

---

## 2. Editor choices the user did not specify

| # | Choice | Verdict |
|---|---|---|
| (a) | `h_fill(τ)` = the 24 h proxy median at τ, not a per-bar value | **Substantive; needs one-line user ratification.** It sets every label and book spread cost. It is PIT-safe, while a per-bar value at τ would need bar k+1 (look-ahead). But it makes `h_fill(t_e)` differ from `ĥ(t)` by one pair only, so a "realized" spread never reflects the fill moment (m2-3) |
| (b) | 24 h window; pairs must be consecutive bars | **Harmless clarification.** The 24 h window comes from the pre-patch S-4.4. Consecutiveness is inherent to AR. Checked: "up to 95 pairs" is arithmetically correct for every τ |
| (c) | "No valid pair → missing → staleness" | **Harmless for decisions; incomplete for fills.** Behaviour when the proxy is missing at a fill (label, forced exit, terminal unwind) is undefined (m2-4). In practice it almost never triggers, because flat zero-volume bars produce valid pairs at the 1 bp floor (C2-1) |
| (d) | Last-price 1m/15m bars added to the forward recorder | **Harmless and necessary.** Without them S-5.2 and S-4.5 can't be computed forward. Forward evaluability still keys on book/ticker snapshots (M2-7) |
| (e) | S-5.4 halted/delisted = S-2.5 halt OR the instance's `last_trade_ts` < `t_x` | **Mostly harmless.** The `last_trade_ts` branch is the pre-existing DEC-12 "no notice" path, and it is an outcome definition (ex post), not a gate. But it is undefined when `last_trade_ts` < `t_e` (C2-1) |
| (f) | ±60 s timestamp jitter in the S-2.6 overlap comparison | **Harmless clarification**, consistent with DEC-29's +60 s |
| (g) | The "confident match" definition | **New rule; needs ratification.** It is reasonable and fails closed. But it is defined on "text known at `releaseDate`", which the CMS can't supply (M2-1). Multiplier and renamed symbols, and multi-contract notices, are unaddressed (m2-15) |
| (h) | RT-5 post-freeze sentence ("triggers K8 / K2-type review") | **New rule with an undefined consequence; needs a user decision** (M2-4) |

Other unapproved constants or wording:
- "fail/flag" (m2-6) and "locally established interval regime" (m2-7);
- the 72 h "continue" semantics (M2-2);
- the 60-minute halt granularity (m2-13).

None of these is an economic parameter, but each changes outcomes mechanically.

---

## 3. Findings

### Critical

#### C2-1: Dead contracts that keep publishing bars aren't split, aren't stopped and get priced. The DEC-24 × DEC-28 × proxy-floor × S-5.4 interaction
**Evidence (spec):**
- S-2.2 l.166: an instance boundary needs "≥ 7 consecutive UTC calendar days with **no archived** 15-minute bar".
- U4 l.190: zero-volume bars count as present.
- U2 l.188: a 30-day **median** of daily quote volume.
- S-4.5 l.279–282: a flat bar gives S_k = 0, so the proxy sits at the 1 bp floor, and that pair is still "valid".
- S-5.4 l.337: stress applies only on the notice-based S-2.5 halt, or when the instance's `last_trade_ts` < `t_x`.
- S-7.3 l.415: the ineligibility exit covers U4, U5, U7 and delisting, but **not U2**.
- S-5.2: P(τ) = the open of the 1-minute bar at τ, with no volume condition.

**Evidence (W_DEV archive, outcome-free):**
- **Spans with no volume > 0 bar for ≥ 7 d, followed by resumed trading: 7.** AERGO 20 d, BNX 21.6 d, CTK 394 d, CVC 899 d, ICP 108.7 d, MAVIA 100.3 d, TLM 294 d.
- **Of those, only BNX, ICP and TLM contain a ≥ 7-day gap with no archived bar.** AERGO, CTK, CVC and MAVIA have bars archived throughout (1,883 / 37,788 / 86,268 / 9,563 zero-volume bars). Under DEC-24 as worded they **remain one instance**: U1 doesn't reset and FL30, persistence and vol features bridge the dormancy.
- For ICP and TLM the bar-absence gap is only the tail of a much longer zero-volume dormancy.
- **Funding records keep being published, non-zero at every settlement, throughout every dormancy checked:** CVC 2,701; CTK 1,181; MAVIA 599; AERGO 120; BTCST 4,510 (2021-03 → 2025-06); OMG 417 and BLZ 1,068 (post-delisting tails).
- So, during a dormancy, U3 and U7 pass, U4 passes (DEC-28), and U5 passes at the 1 bp floor. U1 passes too, because the instance is unsplit or old.
- **Only U2 blocks, and only once the 30-day median falls.** That takes up to about 15 days after trading stops. Onset count, outcome-free (`r2_onset.py`):
  - The `W_DEV` archive has **65 onsets** where volume > 0 stops for ≥ 24 h while zero-volume bars keep being archived.
  - While each contract was still dormant, U2 still passed:
    - **18** of them 1 d after onset (AGIX, BNX, BTT, COCOS, FTM, FTT, ICP, LEND, MATIC, OCEAN, RNDR, SRM, STRAX, TLM, TOMO, VIDT, WAVES, XEM);
    - **14** of them 7 d after onset;
    - **9** of them 14 d after onset (BNX, COCOS, FTM, ICP, SRM, STRAX, TLM, TOMO, VIDT).
  - Several of these are token migrations (MATIC, FTM, RNDR, AGIX, OCEAN, TOMO), which round 1 found are mostly unmatched to a futures notice.

**Why it matters:**
1. **No-notice cases** (31 of the 63 `W_DEV` delistings are unmatched, and every delisting before 2022-02-17 has no catalog notice):
   - A candidate at t after trading has stopped can pass every gate.
   - It "enters" at a stale zero-volume price with a 1 bp spread.
   - If the instance never resumes, S-5.4 sets `t_x` := last volume > 0 bar + 1 min, which is **before `t_e`**: an undefined, negative holding period.
   - If the instance is unsplit and resumes later (CVC, CTK, MAVIA, AERGO), `last_trade_ts` is years later, so no stress applies. The label then exits at a stale price, with zero price variance, after collecting published funding.
2. **This is a mechanically riskless pseudo-carry.** It is perfectly identifiable from F8 (trades per day ≈ 0) and F9 (24 h realized vol = 0). An ML ranker is exactly the tool that would find it. It can inflate IC (P7), P1, CAC (P11) and the executed book. The book keeps such legs until U2 fails at a decision after the minimum hold, because U2 isn't an ineligibility exit.
3. **DEC-24's purpose is defeated** for 4 of the 7 known relists. DEC-28's stated rationale ("activity is controlled by U2") holds only with a lag of up to about 15 days.

**Resolution proposal (user decision; DEC-24's wording and DEC-28 for U4 can stay unchanged).** Choose one or more:
- **(i) Notice-independent operational halt for outcomes (S-5.4).**
  - A fill or reference price at τ needs a 1-minute bar with volume > 0 at τ or within the existing 60-minute data-gap window.
  - Otherwise, if no volume > 0 bar has occurred for ≥ 60 consecutive minutes, the instance is treated as halted: stressed exit at the last volume > 0 bar.
  - If the last volume > 0 bar is before `t_e`, the label is "no label" (structural, counted like S-5.5).
- **(ii) Proxy pair validity (S-4.5).** A pair containing a zero-volume bar isn't valid. A window with no valid pair already gives "missing → staleness → no entry" under the existing rule. This blocks entries into dead contracts point-in-time, without touching U4.
- **(iii) Instance boundary on activity (changes the B-3 wording, so only if the user wants it):** ≥ 7 consecutive UTC days without any volume > 0 bar starts a new instance at the first volume > 0 bar.
- **(iv) Add U2 to the S-7.3 ineligibility-exit list.**

Red-team preference: (i) + (ii) as the minimum, (iii) recommended. Each is outcome-free and decidable now.

### Major

#### M2-1: "Notice text known at `releaseDate`" (S-2.5 l.197) is unobservable: the CMS serves edited text
**Evidence:**
- The 266 stored `W_DEV` bodies contain 25 in-place "updated on YYYY-MM-DD" markers. All are dated < 2025-07-01; the latest is 2025-04-08.
- They include an edit that changed a delisting date (an update dated 2024-12-04, "the GFT delisting date had changed") and an edit that changed a contract's margin type (CRV, an update dated 2022-06-23, "USDT-Margined … to Coin-Margined").
- `lastUpdateTime` is empty in all 266 bodies.
- So the stored text is the latest version, not the version known at `releaseDate`.

**Why it matters:** matching (U6), the parsed scheduled time (72 h rule, halt) and postponement detection may use post-`releaseDate` information. That is look-ahead inside a universe gate, in both `W_DEV` and `W_HOLD`.

**Resolution (user decision):** pre-register an edit rule. For example:
- text inside an explicit "updated on D" block has `known_at` = D + 1 d 00:00 UTC (date-only, conservative);
- if the edit can't be separated from the original text, the whole notice takes `known_at` = D + 1 d;
- "the announcement has been updated" without a date → the notice is used only from its capture time (never, for history), so the instance falls back to the no-notice path.

Record the edit markers in the pinned opening capture (S-1.7) in place of the empty `lastUpdateTime`.

#### M2-2: "The scheduled delisting time parsed from the notice" is undefined
**Evidence:**
- S-2.5 l.201 (72 h deactivation) and l.202 (halt) both key on it.
- Among the 47 `W_DEV` futures notices with bodies, 42 contain a "YYYY-MM-DD HH:MM (UTC)" time, **32 contain more than one distinct time** (e.g. close-only, settlement and delisting times, or different symbols), and 5 contain none.
- "Valid volume > 0 bars continue for 72 h" doesn't say whether every bar, every hour, or simply trading at schedule + 72 h is meant.

**Resolution (user decision; mechanical):**
- Per matched contract, use the stated delisting/settlement time for that contract; if several apply to it, the latest.
- If none can be parsed: U6 stays active until a matched postponement or cancellation, or until the instance ends. The S-2.5 halt test doesn't apply; S-5.4 uses `last_trade_ts`, or the C2-1 (i) rule if adopted.
- 72 h test: ≥ 1 volume > 0 1-minute bar in each of the 72 hours after the scheduled time.
- Note: MAVIA stopped exactly at its schedule (2024-12-16 09:00) and resumed 100 days later. Under the rule as written its U6 never deactivates. That is conservative, but the spec's citation "MAVIA trading past its schedule" is wrong (m2-11).

#### M2-3: The specified proxy statistic is not conservative (synthetic evidence only)
**Evidence (S-4.5 l.279–282):** per-pair truncation at 0, then ×λ and the floor, then the **median** of 95 pairs. In a synthetic Roll-type simulation (random walk, bid/ask bounce, 150 trades per 15-minute bar, 400 replications, no market data):

| σ per 15-minute bar (bps) | True half-spread (bps) | Median proxy (bps) | Share of 24 h windows at the 1 bp floor | Share of positive pair products |
|---:|---:|---:|---:|---:|
| 10 | 3 | 2.94 | 0.12 | 0.57 |
| 25 | 3 | 2.47 | 0.42 | 0.51 |
| 25 | 8 | 8.34 | 0.04 | 0.58 |
| 50 | 3 | 1.22 | 0.49 | 0.50 |
| 50 | 8 | 6.49 | 0.35 | 0.52 |

At 15-minute frequency the pair products are noise-dominated, so about 50 % of them are ≤ 0. The median of the truncated values is therefore at or near the floor, and the estimate falls as volatility rises.

**Why it matters:**
- λ = 2.0 was adopted as a conservative decision. Under median aggregation with pre-truncation it does not deliver conservatism for volatile symbols.
- U5 (7.5 bps) can admit symbols whose true half-spread is higher.
- F6 is close to constant.
- Spread costs feed C_exec, CAC, P2, P6 and P11. Slippage of 2 bps per side offsets this only partly.

**This is the user's own formula, faithfully implemented, so it is not an editing error.** It needs explicit acknowledgement, or a change. One candidate change is to aggregate the 95 pair products (median or mean) before truncation and the square root, i.e. AR's aggregate-then-root form, with λ and the floor applied afterwards. Real-data proxy levels were deliberately not computed.

#### M2-4: Unapproved post-freeze rule in S-5.5 (l.348)
**Evidence:**
- The added sentence reads: "After freeze, the same 0.5 % threshold on realized invalid labels triggers K8 / K2-type review."
- The user chose RT-5 option (a) only; this sentence imports part of option (b).
- K8 covers only the holdout run, and K2 has no such row, so the consequence ("review") for `W_DEV` is undefined.
- Any post-label "review" that changes data rules would be outcome-informed.

**Resolution (user decision):** either delete the sentence (K8 already covers `W_HOLD`), or define it as "a K2-type STOP plus an explicit new-spec decision by the user, with no data-rule change within this spec", and add that to the K table.

#### M2-5: Freeze preconditions outstanding, and an ambiguous fee wording
**Evidence:**
- **S0-14 / DEC-27 has no result yet.** `docs/results/v7-stage0/rt5-input-availability.md` doesn't exist, yet the blocker table says "audit result in `rt5-input-availability.md`". An `rt5_availability.py` process was running during this review (I didn't touch it). It needs the `W_DEV` 1-minute klines (17.3 GB); C: currently has about 24 GB free.
- **B-2 snapshot is missing.** `var/v7/stage0/fees/futureFee.html` and `enBH_futureFee.html` are 0 bytes.
- **S-4.3 l.260 is ambiguous.** It says "must exist before freeze" and also "if it can't be obtained, it stays an operational blocker only", so it is unclear whether the freeze waits for the snapshot.
- **The RT-5 audit's denominator is undefined:** which candidates (G_D0? U1–U7?), which horizons, and whether the unit is per label (candidate × side × h) (m2-17). The 15-minute fallback can't apply at hh:20 (m2-2).

**Resolution:**
- Run S0-14 with a pre-registered denominator before the freeze review (the user approves the denominator first).
- The user states whether the freeze may proceed with the fee snapshot pending.
- Fix the blocker-table pointer.

#### M2-6: Seal enforceability after DEC-25
**Evidence:**
- S-1.7 l.129 prohibits "CMS/catalog queries", but the guard list (l.132–137) and L10 enforce only timestamp overlap. The CMS endpoint has no date parameter (INCIDENT l.17), so no timestamp guard can block it, which is exactly how the breach happened.
- The incident's request 6 (Wayback CDX) returned `W_HOLD`-dated capture records. S-1.7's wording ("announcement or CMS metadata") doesn't clearly cover Wayback CDX or captures.
- S-14.1 step 6 would fetch `W_HOLD`-period captures for symbols missing from current `exchangeInfo`, with no "only at the opening" timing.
- Current `exchangeInfo` (allowed by S-2.2) carries current statuses that implicitly reveal post-`W_HOLD`-start delistings. No rule says these status fields are unused before the opening.

**Resolution:** a clarification of the decided DEC-25 policy, but it needs the user's acknowledgement because it adds a timing rule.
- Extend L10 with a pre-opening endpoint denylist plus refusal tests: CMS `article/list` and `article/detail`, Wayback CDX, and captures dated ≥ 2025-07-01.
- S-14.1 step 6: `W_HOLD`-dated captures only at the legitimate opening.
- Current `exchangeInfo` status and delivery fields: not used before the opening.

#### M2-7: Forward evaluability uses book/ticker snapshots, contradicting "R6 diagnostic only"; bar coverage is missing from it
**Evidence:**
- §15 l.751 defines an evaluable day by full-universe ticker and BTC book-snapshot freshness, and l.786 skips decisions without a "fresh snapshot".
- S-4.5, the R6 row and L8 say book-top data "never alters any V7 verdict", and evaluability drives INCONCLUSIVE.
- After DEC-22 the replay actually depends on the recorded 1m/15m bars and funding, and their coverage isn't in the evaluability rule.

**Resolution (user decision):** define evaluability and decision skipping on recorded bar coverage (1m/15m) plus funding freshness. Or keep snapshot liveness explicitly as a recorder-liveness criterion, with a carve-out in S-4.5 and L8 saying it uses no values.

### Minor

| ID | Finding (evidence) | Proposal |
|---|---|---|
| m2-1 | "R6" means both the V6 release time (S-1.2, L7 = max(…, R6)) and the `book_top` table (§3, L8 "R6 never feeds … verdict") | Editorial rename (e.g. `R6_BOOK` / `R_V6`); no semantic change |
| m2-2 | S-5.2 fallback "15-minute bar starting at τ" can never apply at hh:20, at arbitrary forced-exit minutes or at terminal-unwind minutes | State that P(τ) is 1-minute only; the RT-5 audit must not count the fallback |
| m2-3 | `h_fill(τ)` = 24 h median at τ (choice a) ≈ `ĥ(t)` | User ratifies, or picks an alternative |
| m2-4 | Proxy missing at a fill: behaviour undefined | E.g. label invalid (S-5.5); in the book, use the last valid proxy value with the S-5.4 25 bps floor; pre-register |
| m2-5 | "No other sufficiency threshold": one pair suffices at fills of legs that fail U4 | Acknowledge, or add a minimum |
| m2-6 | S-2.6 "fail/flag": user said "flagged" | Off-schedule settlements stay real cash flows in labels and book; they are flagged, never fail labels |
| m2-7 | "Locally established interval regime" undefined | E.g. the explicit interval of the nearest preceding record in the instance, else the spacing to the previous record |
| m2-8 | ±60 s jitter (choice f) | Accept |
| m2-9 | "≥ 7 consecutive UTC calendar days": full calendar days vs ≥ 168 h | Define. `W_DEV` gaps are 21–29 d, so no current impact |
| m2-10 | The new instance's `first_bar_ts` may be a zero-volume bar before trading resumes (ICP: dormant from 2022-06-10, bar gap 08-31 → 09-26) | Define listing time as the first bar of the instance vs its first volume > 0 bar |
| m2-11 | S-2.5 l.204 cites "MAVIA trading past its 2024-12-16 schedule"; the archive shows its last volume > 0 bar at 2024-12-16 09:00, 100 d of zero-volume bars, and resumption on 2025-03-26 | Correct the citation (`W_DEV` fact; the rule is unaffected) |
| m2-12 | After a postponement, U6 deactivates with no re-activation at the new date (user's choice). The halt test's "scheduled time" after a postponement is unclear (OMG) | Clarify: the halt test uses the latest known scheduled time; otherwise S-5.4 via `last_trade_ts` |
| m2-13 | Halt "60 consecutive minutes": 1m or 15m bars? | 1-minute (R5) |
| m2-14 | S-5.4's `last_trade_ts` branch is ex post | State that it is an outcome definition only, never a gate input |
| m2-15 | Confident match (choice g): multiplier symbols (1000X vs X), renames, multi-contract notices with different times | Ratify; add those cases |
| m2-16 | Forward bars (choice d): `known_at` of recorded bars | `known_at` = max(`recv_ts`, bar close); no backfill (consistent with §15) |
| m2-17 | RT-5 audit denominator undefined | Pre-register before running |
| m2-18 | R3/R4/R5 are keyed without `instance` | Instance is derived from R1 boundaries; state this |
| m2-19 | "Verified invariant for the instance" undefined; old instances (BNX, ICP, TLM pre-gap) need a source dated within that instance | ≥ 1 timestamped source within the instance and no conflicting source; else exclude |
| m2-20 | S-1.7 requires each article's `lastUpdateTime`; empty in all 266 stored bodies | "If populated"; also record the in-text update markers (M2-1) |
| m2-21 | INCIDENT says "mid-difference tolerances" were computed between hosts; S-1.3's allow-list omits value differences | Allow "cross-host value differences on identical events, reported as pass/fail only", or drop them |
| m2-22 | Section order S-2.3 → S-2.5 → S-2.6 → S-2.4 | Cosmetic |

---

## 4. Seal incident: is any reset needed beyond the recorded governance incident?

**No reset beyond the governance incident is required.** My reasoning:
- **The exposure is non-outcome metadata:** 5 M6 titles naming no USD-M perpetual, counts, and the latest date.
- **No frozen parameter depends on `W_HOLD` or M6 notice frequency.** The S-5.4 stress parameters predate the incident. DEC-26 cites only `W_DEV` examples (one of them mis-described, m2-11, but still `W_DEV`).
- **The stored `W_DEV` bodies are clean.** The `relatedArticles` lists are replaced by a purge note. Three bodies published in `W_DEV` mention scheduled dates in 2025-07…09: that is forward-looking content known at release, not later-dated metadata.
- **D0, the folds and F7/F15 come from pre-incident, outcome-free data and are unaffected.**

**Conditions:**
1. The INCIDENT file is committed unchanged with the freeze.
2. M2-6's guard and denylist are adopted, because the breach path is still open in code terms.
3. The residual unverifiable round-1 display is acknowledged, as the INCIDENT already does.
4. The Stage-0 audit's quotation of the M6 futures title stays as-is (permanent record), and no rule may cite it.

---

## 5. Summary

- **Critical: 1** (C2-1).
- **Major: 7** (M2-1 … M2-7). Of these:
  - M2-1, M2-2, M2-3 (acknowledge or change), M2-4 and M2-7 need user decisions;
  - M2-5 is outstanding preconditions plus a wording decision;
  - M2-6 is a guard clarification needing acknowledgement.
- **Minor: 22.**
- The new user decisions are appended to `BLOCKERS_FOR_USER.md` under "Red-team round 2 additions".

NOT READY FOR SPEC FREEZE — BLOCKERS
