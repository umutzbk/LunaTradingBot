# V7 ML RESEARCH SPEC — DRAFT 2

**ML-assisted perp funding-carry challenger. Binance USD-M primary, Kraken exploratory control.**

| | |
|---|---|
| Status | **DRAFT 2 (incl. the PRE-STAGE-0 DECISION PATCH, the PRE-FREEZE BLOCKER RESOLUTION PATCH, the FINAL PRE-FREEZE PATCH for R2-1 … R2-9, the ROUND-3 PATCH for M3-1 … M3-5 and the FINAL EDITORIAL CLARIFICATIONS EC-1, EC-2, EC-6 … EC-9, EC-11, and the DEC-39 fee-evidence decision). Not committed, not frozen.** It merges Draft 1, REV 2 and REV 2.1 (REV 2.1 overrides REV 2). No implementation, market-data download, training, tuning or holdout inspection is allowed under this draft. |
| Date | 2026-10-14 |
| Worktree / branch | `../LunaTradingBot-v7`, `feat/v7-ml-research` (from `a664aca`) |
| Freeze path | Stage 0 data-availability audit (§18) → user review → independent red-team → every open decision in §20 closed → commit + annotated tag `v7-ml-spec-frozen`. Implementation starts only after the tag; training only after the code freeze. |

Identifiers: `S-…` are spec rules, `L-…` leakage controls, `P-…` acceptance criteria, `D-…` deployment gates, `K-…` kill criteria, `DEC-…` closed decisions, `O-…` open decisions. Every numeric threshold here is fixed before any V7 outcome is seen. A number that Stage 0 must confirm is marked; it is closed before freeze by a rule written here.

---

## 0. Purpose, scope, boundaries

**S-0.1 Goal.** Find **repeatable positive post-cost expected value** in perp funding carry. ML is used only to rank or veto candidates.
- Increasing trade count is not a goal.
- A positive cumulative P&L alone is never sufficient for PASS (§13).
- **"No viable edge" is a valid and acceptable final result.**

**S-0.2 Separation.** V7 never modifies, and never reads the results of:
- **V6:** spec tag `v6-profitability-spec-frozen`; code tag `v6-code-frozen` → `ea78102`; Erratum 001; sealed holdout; masked forward looks.
- **The frozen V5 carry bot:** `bot-carry` at `a664aca`, paper only, on the VPS.
- **Any paper/live configuration.** `live_enabled=false` stays.

V7 imports the frozen V5 decision function read-only, as a baseline (§7).

**S-0.3 In scope.**
- One structure family: **hedged perp legs.** ML scores individual perp legs; a deterministic constructor builds a market-neutral long/short book; at most **one book-level residual BTC hedge** (S-7.4).
- Venues: Binance USD-M USDT-margined perpetuals (primary); Kraken perps (exploratory control, S-1.6).
- Horizons **{24 h, 72 h, 168 h}**, fixed.

**S-0.4 Model-family policy (CLOSED).** The confirmatory family is exactly:
1. Elastic Net regression;
2. logistic regression with an elastic-net penalty;
3. LightGBM, the single nonlinear challenger (regressor and classifier).

No Prophet, neural networks, reinforcement learning, CatBoost or XGBoost. If LightGBM fails, no other booster is tried for this spec.

Rationale: V7 is a cross-sectional panel tabular problem, not a univariate trend/seasonality forecast. Extra families add researcher degrees of freedom and multiple-testing burden.

Pure time-series baselines (rolling mean, EWMA, AR(1); DEC-20) are reference arms, not model-selection families.

**S-0.5 Non-goals.**
- Spot–perp cash-and-carry.
- Cross-venue arbitrage.
- ML-driven sizing, leverage or stops.
- Live trading.
- Any use of V5/V6 outcome numbers as inputs, features or selection signals.
- **Per-leg executed hedges** (a per-leg-hedge strategy would be a separate pre-registered family; none exists here).

**S-0.6 Deterministic control boundary.** ML output may only:
1. **order** candidates that already passed the deterministic gates (ranker), or
2. **remove** candidates (veto).

ML can never:
- add a gate-failing candidate;
- change sizing, leverage, gross or net limits, the beta limit or stops;
- override a forced exit, kill switch, staleness rule or liquidity cap;
- open the residual hedge or change its size.

These boundaries are enforced in code and unit-tested.

---

## 1. Data windows, V6 embargo, D0

**S-1.1 Canonical V6 boundaries.** V7 never hard-codes V6 boundaries. Its guards read them at runtime from the V6 artefacts at tag `v6-code-frozen` (`ea78102`), and refuse to run if a hash differs:

| Quantity | Canonical source | Derived value (verified 2026-10-14) |
|---|---|---|
| V6 holdout | `docs/superpowers/specs/luna-v6-frozen-constants.json` → `holdout.start` / `holdout.end` (sha256 `152af4cb…`) | [2026-07-01T00:00:00Z, 2026-10-09T11:52:48.861565107Z) |
| V6 guarded gap | same file → `holdout.guard_window` ("[2026-07-01, T_F) refused without the holdout token") | [holdout end, T_F) |
| V6 T_F | `docs/results/v6-code-freeze-record.json` → `T_F` (sha256 `2f844995…`) | 2026-10-14T00:00:00Z |
| V6 looks | `luna/v6/verdict.py` `LOOKS` and `luna/v6/forward.py` `look_date(T_F, d)` at `ea78102` | day 90 = 2027-01-12T00:00Z; day 180 = 2027-04-12T00:00Z; day 365 = 2027-10-14T00:00Z |
| V6 masking | constants → `forward.pnl_masked_until`; V6 spec S-GUARD-3; S-STAT-X (terminal at day 365; FINAL GO monitored until day 365) | outcomes released only by scheduled looks |

**S-1.2 The V6-masked span M6 = [V6 holdout start, R_V6)**, where **R_V6** is the earliest of:
- **(a)** completion and report of V6's day-365 look;
- **(b)** completion and report of a V6 look after which every one of the four V6 slots is CLOSED, with no PENDING slot and no FINAL GO under monitoring;
- **(c)** an explicit written release by the user.

R_V6 also requires the V6 holdout to have been opened and reported, or released in writing. If the V6 holdout is never opened, M6 stays masked until (c).

**S-1.3 Strict V6 embargo (DEC-1).** No V7 code path may generate any **outcome** for any timestamp inside M6 before R_V6, from **any venue (Binance or Kraken)**:
- labels;
- P&L or paper equity;
- fills;
- IC;
- any metric computed from post-decision prices or funding.

Rules:
- Guards refuse a request whose window **overlaps** M6, not only one that lies inside it.
- **Outcome-free raw recording** (public market data stored with `recv_ts`) is allowed inside M6, provided nothing derived from it is computed or displayed before R_V6.
- There is **no Binance exception.**
- **Collector-integrity monitoring is not research (DEC-25).** Checks between collector hosts (e.g. PC ↔ VPS) may compare coverage, cadence, hashes, timestamps and exact-event equality (including tolerance-based equality of the same event's values, reported as pass/fail only). They must never expose or summarize market direction, returns, strategy P&L, funding profitability, or V7/V6 evidence.

**S-1.4 V7 windows.**

| Window | Definition (UTC, half-open) | Use |
|---|---|---|
| `W_DEV` | [`D0`, 2025-07-01) | development, walk-forward validation, model selection |
| `W_HOLD` | [2025-07-01, 2026-07-01) | **V7 sealed historical holdout**; opened once, under the DEC-19 condition |
| `W_FWD7` | [`T_F7`, `T_F7` + 180 d) | V7 forward test: outcome-free recording from `T_F7`, delayed-reveal replay at a single look (§15) |

`W_DEV` and `W_HOLD` do not overlap M6. `W_FWD7` does; its outcomes are computed only at L7 ≥ R_V6 (§15).

**S-1.5 No outcome reads past its window's end.** Every label, P&L, backtest and terminal unwind in a window uses data with timestamps strictly before that window's end:
- `W_DEV`: before 2025-07-01;
- `W_HOLD`: before 2026-07-01, which is also the M6 start;
- `W_FWD7`: before `T_F7` + 180 d.

Consequences:
- A decision whose label window would cross its window's end has **no label**. This is structural and handled by the purge (S-10.3); it is not selective dropping.
- Each window's book backtest ends with the terminal unwind (S-8.1) at the last reference minute before the window's end.

Feature look-backs may reach into earlier windows (point-in-time inputs, not outcomes); for `W_HOLD` decisions they are computed only at the opening.

**S-1.6 Kraken is exploratory.**
- Kraken spread data exist only from 2025-10-06 (the frozen V5 copy, manifest `eecdc6a2…`).
- 2025-10-06 → 2026-06-30 was already observed in the V5 outcome run.
- Every Kraken result is labelled **exploratory (previously observed window)**, never enters the primary test, the acceptance or the deployment gates, and is computed only at the `W_HOLD` opening (DEC-17).

**S-1.7 Holdout seal and pre-opening endpoint allowlist (DEC-25, DEC-41).**
- Before the opening, only file listings (name, size, published checksum) of `W_HOLD` may be read, for the Stage 0 completeness check.
- No file content, no REST queries for `W_HOLD` timestamps, no `W_HOLD` features or labels.
- **No announcement or CMS metadata dated at or after the `W_HOLD` start.**
- **Exact endpoint allowlist (DEC-41 as amended by DEC-46), enforced in code.** Permission is granted per exact endpoint and parameter set, never per host and never by assuming that a time parameter bounds an endpoint. Before the opening, every V7 research request goes through one fetch layer that refuses, before any network I/O, everything not listed here:

| # | Exact endpoint | Allowed use and parameters before the opening |
|---|---|---|
| N1 | `https://data.binance.vision/data/futures/um/{monthly,daily}/<dataset>/<SYMBOL>/[<interval>/]<file>.zip` (+ `.CHECKSUM`) | file content only when the file's whole period (from its name) is < 2025-07-01. `W_HOLD` and M6 file content is refused |
| N2 | `https://s3-ap-northeast-1.amazonaws.com/data.binance.vision` with `delimiter=/` and `prefix=data/futures/um/…` (bucket listing) | names, sizes and published checksums. Keys whose period is in M6 are dropped in memory before anything is stored or displayed; `W_HOLD` keys keep name, size and checksum only |
| N3 | `https://fapi.binance.com/fapi/v1/fundingRate` | `symbol`, `startTime` and `endTime` both required with `endTime` < 2025-07-01, `limit`. This is the only Binance REST market-data endpoint allowed; it honours `endTime` |
| N4 | `https://fapi.binance.com/fapi/v1/exchangeInfo` (no parameters) | current snapshot, used only as E8 allows (below) |
| N5 | `https://web.archive.org/cdx/search/cdx` | `to` required and ≤ 20250630235959; `url`, `matchType`, `from`, `filter`, `fl`, `collapse`, `limit` |
| N6 | `https://web.archive.org/web/<ts>id_/<original>` | only an exact (`ts`, `original`) pair returned by an N5 query, with `ts` < 20250701000000. Redirects are disabled. The response must be HTTP 200 with `Memento-Datetime` equal to `ts`. Any redirect or timestamp substitution is a guard failure: the response is discarded unparsed and logged |

  - **Refused:** every other host, path or parameter. In particular: every Binance CMS / `bapi` endpoint (no workaround that downloads later-dated metadata and filters it locally); every current-state `fapi` endpoint (e.g. `premiumIndex`, `ticker/*`, `openInterest`, `fundingInfo`), even with an old `endTime`; and Wayback `/web/<ts>/` requests that are not exact N5 pairs.
  - **E8: current `exchangeInfo` (N4), narrow scope.** It may establish only explicitly whitelisted immutable contract identity / schema fields (base, contract type, underlying type, multiplier; DEC-31) of an instance whose invariance is verified (S-2.1, R1). It may never backdate current status, listing or delisting state, filters, quantity or notional limits, or any other time-varying field. Unknown identity fields → exclude; never guess. Current filters are never backdated or represented as historical truth. Their only other permitted use is the separate DEC-21 approximation layer of S-14.1 (EC-6), which is not historical metadata.
- **Guard tests** attempt every refused class and prove refusal: CMS catalog list and article detail; a current-state `fapi` path carrying an old `endTime`; `fundingRate` without `endTime` or reaching ≥ 2025-07-01; N5 without `to` or with `to` ≥ 20250701000000; an N6 request not returned by a bounded N5 query; an N6 response that redirects or carries a different `Memento-Datetime` (refused, content discarded); a `W_HOLD` and an M6 archive file; an N2 listing whose M6 keys must be dropped before storage; a universe reconstruction reading `exchangeInfo` status, listing or filter fields as historical state.
- **E6: at and after the legitimate `W_HOLD` opening, while M6 is sealed (before R_V6).** The research CMS stays blocked. `W_HOLD` delisting information comes only from immutable captures whose capture timestamps are < 2026-07-01, fetched once under the N5 / N6 rules with the bound raised to 20260630235959, pinned and hashed (S-2.5). Any `lastUpdateTime` comes only from inside such captures. The `W_HOLD` historical exchange-filter captures (S-14.1 step 6) follow the same rule.
- **M3-3: forward announcement recorder, write-only (DEC-45).** From `T_F7` the dedicated forward recorder (§15) may fetch raw Binance announcement snapshots as S-1.3 outcome-free raw recording:
  - every response gets `recv_ts`, source URL and SHA-256, and its raw bytes go directly into the sealed forward store;
  - **transport-level ID extraction (EC-8):** the recorder may do the minimum mechanical parsing needed to extract the article ID / code / URL from the raw catalog response and to fetch the corresponding raw article body. Both raw responses are stored with `recv_ts`, URL and SHA-256. This is transport orchestration only;
  - before the reveal (L7) it may not classify an article, inspect semantic content, extract symbols, extract delisting times, display titles or bodies, or generate any derived research table. No search, metric or outcome is permitted before L7 either. Semantic parsing happens only at the legitimate reveal / opening boundary;
  - researcher-facing tooling can't read the store before L7 (guard test);
  - this is not permission for Stage-0, `W_DEV` or `W_HOLD` research code to query the mutable CMS.
- **Forward exchange filters** are recorded only from `T_F7`, with `recv_ts` and hash (§15).
- **Protocol incident 2026-10-14 (DEC-25):** non-outcome metadata exposure, recorded permanently in `docs/results/v7-stage0/INCIDENT-2026-10-14-seal.md`. The file is immutable and is included unchanged in the freeze record, with its SHA-256 (`beb3cb085397c6f5d8b2481bf899ce304b202b16a5679c9a5d1e32c85152380e` on 2026-10-14). No future V7 decision may use the exposed M6 information. No reset is required unless an independent reviewer identifies actual outcome contamination.
- Guards follow the V6 pattern:
  - a token from an explicit approval phrase;
  - two exclusive markers keyed by a canonical run id;
  - a pinned manifest;
  - read-only loaders;
  - refusal tests.

**S-1.8 D0 (DEC-2).**
- D0 is the first calendar-month start M ≥ 2021-01-01 such that at **every** decision time t in M (00:05, 08:05, 16:05 UTC), the number of USD-M symbols satisfying the **breadth gates G_D0 = U1 ∧ U2 ∧ U3 ∧ U4** (S-2.3) is ≥ 30.
- It uses metadata and data availability only: no label, return, IC or P&L.
- The rule is frozen before Stage 0; Stage 0 evaluates it.
- U5 (spread; S-4.5 proxy, DEC-22), U6 (announcements), U7 (transient freshness) and U8 (transient trading activity, DEC-35) are excluded by design.
- U1 and U3 use the activity-based contract instances of S-2.2 (DEC-35). D0 = 2021-01-01 was re-verified under them (`docs/results/v7-stage0/rt5-input-availability.md`).

---

## 2. Instruments and point-in-time universe

**S-2.1 Instrument set (Binance).** USD-M contracts are included only if all of the following hold:
- `contractType = PERPETUAL`;
- `quoteAsset = USDT`;
- single-coin underlying (`underlyingType = COIN`; index contracts such as `BTCDOMUSDT` excluded).

Excluded:
- stablecoin bases: USDC, FDUSD, TUSD, BUSD, USDP, DAI, USDE;
- `BTCUSDT`, which is the hedge instrument only, never a candidate.

Multiplier symbols (for example `1000PEPEUSDT`) are included with quantity scaling.

**Unknown identity is excluded (DEC-30, DEC-31).** An instance whose `underlyingType` or contract identity can't be established from a timestamped source without guessing is excluded. Types are never inferred. Stage 0 found 5 such symbols: AERGOUSDT, BDXNUSDT, DOTECOUSDT, LENDUSDT, MBLUSDT.

**S-2.2 Symbol history is reconstructed, never taken from today's list.** The symbol set is the union of:
- every symbol that has ever had USD-M files in the public archive;
- every symbol in current `exchangeInfo`.

**Contract instances (DEC-24 as amended by DEC-35).** Instances are defined by trading activity, not by bar presence.
- **Dormancy:** a run of ≥ 7 consecutive UTC calendar days in each of which the symbol has no 15-minute last-price bar with volume > 0. Missing bars and exchange-supplied zero-volume bars both count as no activity, so zero-volume bars or funding records continuing through the run don't prevent it.
- The current instance **ends** at its last volume > 0 15-minute bar before a dormancy (`last_trade_ts`). For historical reconstruction it is then dormant/ended.
- **The end is known only after the dormancy (M3-2, DEC-44, EC-1).** Canonical definition: **`instance_end_known_at` = close_time(last volume > 0 15-minute bar) + 7 × 24 h.** No later midnight boundary is substituted.
  - Acknowledged property: when that close is not at 00:00 UTC, this time precedes the end of the seventh inactive UTC calendar day by less than 24 h. The instance is then necessarily untradable throughout that interval, because there is no volume > 0 bar after the close: U8 is false (no entry or increase) and the instrument is HALTED (S-5.4). The `W_DEV` check (`rt5-input-availability.md`, EC-1) found that it changes no label-population member and no D0 result.
  - U8 is independent of this rule and blocks new entries 60 minutes after the last trading activity.
  - No decision before `instance_end_known_at` may use the knowledge that the instance has permanently ended. At `t`, the current instance is the latest instance with `first_bar_ts` ≤ `t`; until its end is known, trading activity is controlled only by U8 and the S-5.4 halt, which never assert a permanent end.
  - If trading resumes before the seven-day boundary, it is the same instance. If it resumes after it, the first new volume > 0 bar begins a new instance.
- The first later volume > 0 15-minute bar **starts a new instance**. Its open time is the new instance's `first_bar_ts`. A symbol's first instance starts at its first archived 15-minute bar.
- At a new instance, `first_bar_ts`, U1 listing age, U3 funding history and every rolling funding statistic and feature (§9) reset; they use the current instance only.
- Bars and funding records dated inside a dormant interval belong to no instance. They never bridge two instances, never enter a feature, gate, label or book cash flow, and are never synthesized.
- Instance boundaries are derived from R3 alone and stored in R1. R2–R5 rows are assigned to instances through these boundaries, so their keys need no instance column.
- U4 is unchanged (DEC-28): a valid exchange-supplied zero-volume bar is still a present bar. Trading activity is controlled separately by U8 (S-2.3) and by the halt rule (S-5.4).

Historical instances that can't be reconstructed (e.g. old instances with funding records but no archived bars) are **not synthesized**. They simply fail the relevant data and eligibility requirements (known limitation, DEC-32).

Field sources (per instance):
- **Listing time:** the instance's `first_bar_ts` (above).
- **Last trading time `last_trade_ts`:** the instance's last archived bar with volume > 0.
- **Delisting notices:** only timestamped archived snapshots, matched point-in-time (S-2.5, DEC-12, DEC-26, DEC-36).

**Kline reconstruction (DEC-33).** R3 and R5 = the union of the monthly and daily archive files by primary key (symbol, `open_ts`). Daily files fill keys missing from monthly files, which handles the known truncated monthly tails. If a key exists in both with conflicting OHLCV values, the conflict is reported as a data-integrity blocker; neither value is chosen silently.

Delisted symbols stay in every historical sample.

**S-2.3 Point-in-time eligibility at decision time `t`.** All inputs must have `known_at` ≤ `t` − 1 min.

| Gate | Rule |
|---|---|
| U1 listing age | the current instance's `first_bar_ts` ≤ `t` − 60 d (S-2.2) |
| U2 liquidity | median daily quote volume over the 30 UTC days fully ended before `t` ≥ 20,000,000 USD (the long-horizon liquidity gate) |
| U3 funding history | ≥ 30 d of settled funding records of the current instance known by `t`, with no gap in the last 7 d. A gap between adjacent settlements exists when their spacing > max(previous nominal interval, current nominal interval) + 60 s (S-2.6, DEC-29) |
| U4 bar coverage | ≥ 95 % of last-price 15-minute bars present in the last 24 h. A valid bar supplied by the exchange counts as present **even with zero volume**: U4 measures data coverage, not trading activity (DEC-28). Trading activity is controlled by U8 and the S-5.4 halt rule (DEC-35) |
| U5 spread | decision-time half-spread estimate `ĥ(t)` (S-4.4) ≤ 7.5 bps |
| U6 delisting | active for the instance while a matched notice version is in force at `t` under the point-in-time rule of S-2.5; otherwise inactive |
| U7 data freshness | the last settled funding record ≤ 1 nominal interval + 15 min old at `t` |
| U8 trading activity (DEC-35) | at least one 15-minute last-price bar with volume > 0 whose close lies in [`t` − 60 min, `t` − 1 min]. If U8 is false, no new entry or increase is allowed. U8 is not an exit trigger: open positions follow S-5.4 |

**S-2.4 Kraken.** The frozen 20-symbol V6 universe with the same gate definitions on Kraken data, using the V5 gate values where they differ (minimum 24 h quote volume 1,000,000 USD). Kraken `BTC` is the Kraken hedge instrument.

**S-2.5 Point-in-time delisting notices (DEC-26 as amended by DEC-36, DEC-37, DEC-40).** Only information known at time `t` is used. No last-trade or later-history condition may decide whether a notice was valid; the old forward-looking "`releaseDate` ≤ last trade" logic is permanently removed.
- **Source:** official Binance announcements (delisting, postponement and cancellation notices), each stored as versions in R11.
- **Notice content is not assumed known at `releaseDate` (DEC-36).** CMS bodies are mutable and `lastUpdateTime` is unpopulated.
  - **History:** a version's content becomes known only at the timestamp of the earliest immutable, timestamped archived snapshot that actually contains that content: `known_at` = max(`releaseDate`, archive capture timestamp).
  - If no such snapshot exists, current CMS text is never backdated and can't activate historical U6.
  - A later archived revision is a new version, known at its own capture timestamp. Each version is evaluated by itself.
  - Snapshots are fetched only inside the S-1.7 allowlist (`W_DEV`: captures < 2025-07-01; `W_HOLD`: at the opening).
  - **Forward:** our own immutable snapshot has `known_at` = `recv_ts`, and is kept with its hash, retrieval timestamp and source (§15).
- **Confident match (DEC-40).** A version matches a contract instance only when both hold:
  1. the exact contract symbol (e.g. `XXXUSDT`, including any multiplier prefix such as `1000`) appears as a whole token in the archived title or body;
  2. the notice unambiguously refers to Binance USDⓈ-M futures / perpetuals, or to the exact relevant derivatives product.

  An ambiguous token or context is no match. No fuzzy or semantic inference is permitted. A match applies to the instance live at the version's `known_at`.
- **Activation:** U6 activates for the instance at the matched version's `known_at`. From then on the contract is ineligible for new entries and increases, and an already-open position exits at the next normal decision opportunity (S-7.3).
- **Scheduled delisting time (DEC-37).** `scheduled_delist_ts` is set only when one exact UTC timestamp can be deterministically associated with the exact contract instance from the archived notice.
  - If several timestamps appear, one is used only when the notice explicitly maps that timestamp to that exact contract or product; otherwise `scheduled_delist_ts` = null.
  - If no timestamp appears, `scheduled_delist_ts` = null.
  - Activation and the exit of open positions never depend on parsing a scheduled time.
- **Deactivation:**
  - at the `known_at` of a later matched, timestamped postponement or cancellation version for that instance;
  - **only when `scheduled_delist_ts` is uniquely defined:** if the instrument is not HALTED (S-5.4) at any time in [`scheduled_delist_ts`, `scheduled_delist_ts` + 72 h], U6 deactivates at `scheduled_delist_ts` + 72 h. With `scheduled_delist_ts` = null, only an explicit later postponement or cancellation clears U6;
  - if the version's `known_at` is after `scheduled_delist_ts` + 72 h, the 72 h test is evaluated at `known_at`; if it already passed, U6 never activates;
  - U6 also ends with its instance.
- **No notice known:** U6 is inactive. Halts and exits follow the notice-independent S-5.4.
- **Labels:** U6 governs candidates and executed-book positions. A label is a fixed-horizon scoring measurement (S-5.3) and changes only through S-5.4.
- The methodology rests on `W_DEV` examples only:
  - OMG: notice 2024-12-06, postponed 2024-12-14 and again 2024-12-20, last traded 2025-01-31;
  - MAVIA: named in the 2024-12-06 notice for 2024-12-16. Its last volume > 0 bar is at its scheduled time (2024-12-16 09:00); zero-volume bars then continue until trading resumes on 2025-03-26, which is a new instance under DEC-35;
  - BLZ: notice 2022-08-17.

  No exposed M6 information is used (S-1.7).

**S-2.6 Funding records and interval semantics (DEC-29, DEC-32).**
- **Source:** the archive monthly `fundingRate` files are primary. REST `/fapi/v1/fundingRate` fills only periods the archive lacks.
- **Overlap:** keys, rates and timestamps are compared. If they agree, archive provenance is kept. A material conflict is reported as a data-integrity blocker. One source never silently overwrites the other.
- **Instance-aware:** funding records, U3 and funding features never cross an instance boundary (S-2.2).
- **r₈ normalization (§9):** uses the record's nominal interval (below). Never from prices or returns.
- Timestamps are compared with the frozen ±60 s tolerance.
- **Nominal interval (DEC-29, restored by DEC-47).**
  - The explicit historical `funding_interval_hours` field controls where available.
  - Where it is missing, the interval is inferred mechanically from the adjacent settlement spacing and the locally established interval regime: the standard Binance interval (1, 2, 4 or 8 h) that matches the spacing from the previous settlement of the same instance within ±60 s. If no standard interval matches, the record inherits the previous record's nominal interval (the local regime) and is flagged. The first record of an instance without an explicit field has no adjacent spacing, so its interval is undetermined and it is flagged.
  - Inference never uses prices, returns or records of another instance. An anomalous record never removes its successors' own explicit or spacing-inferred intervals: one bad record can't poison later records.
  - U3's gap test uses the approved adjacent-interval semantics: a gap exists when spacing > max(previous nominal interval, current nominal interval) + 60 s.
- **Off-schedule settlements (DEC-40, EC-9).** A record is reconciled when its spacing from the previous settlement of the same instance equals, within ±60 s, the previous record's (determined) nominal interval or the record's own explicit interval. The first record of an instance is reconciled only if it carries an explicit interval.
  - **Regime transitions (EC-9):** a record without an explicit field whose spacing matches a standard interval within ±60 s, but not the previous nominal interval (or the previous interval is undetermined), is successfully reconciled by the DEC-29 inference. It stays **valid**, carries the provenance flag `interval_transition = true`, and its successors reconcile against the new inferred interval.
  - A genuinely unreconciled / off-schedule record (including the six off-schedule Stage 0 cases) is individually marked **invalid** and reported. It is never silently normalized. Its invalidity never propagates to later valid records.
  - Every label whose (`t_e`, `t_x`] contains an invalid record is invalid (S-5.5). For U3 and the §9 features, its r₈ is treated as missing.
  - The existing invalid-input thresholds (S-5.5, K8) decide whether this escalates into a larger failure.

---

## 3. Data schema (raw → derived)

All raw files are immutable once written. Each table has a manifest with per-file sha256. Every row carries `known_at` (UTC, ns): the earliest time a live process could have had the value. Time units are normalized to ns at ingest.

| # | Table | Key | Columns (beyond key) | `known_at` |
|---|---|---|---|---|
| R1 | `instruments` | venue, symbol, instance | base, contract_type, underlying_type, multiplier (immutable identity) · first_bar_ts, last_trade_ts (activity-based instance boundaries, S-2.2), instance_end_known_at · tick, step, min_notional (time-varying filters) · source, retrieved_at | **Immutable identity fields** verified invariant for the instance (at least one timestamped source dated within the instance states the field and no source conflicts; otherwise the instance is excluded): the instance's `first_bar_ts`, via the explicit L1 identity whitelist (DEC-31). **Time-varying fields:** their actual effective / known timestamp. **Instance end (`last_trade_ts` as an end):** `instance_end_known_at` (S-2.2, DEC-44) |
| R2 | `funding` | venue, symbol, funding_ts | rate, interval_h, reconciled / invalid flag, `interval_transition` provenance flag (S-2.6), mark_at_settle (nullable), source (archive / REST) | funding_ts + 1 min (S-2.6); forward: max(`recv_ts`, funding_ts + 1 min) |
| R3 | `bars_last_15m` | venue, symbol, open_ts | o, h, l, c, vol_base, vol_quote, n_trades, taker_buy_quote | open_ts + 15 min; monthly ∪ daily files (DEC-33); forward: max(`recv_ts`, open_ts + 15 min) |
| R4 | `bars_mark_15m`, `bars_index_15m`, `bars_premium_15m` | venue, symbol, open_ts | o, h, l, c | open_ts + 15 min; forward: max(`recv_ts`, open_ts + 15 min) |
| R5 | `bars_last_1m` | venue, symbol, open_ts | as R3 | open_ts + 1 min; for the reference prices of S-5.2 and the halt rule of S-5.4; monthly ∪ daily files (DEC-33); forward: max(`recv_ts`, open_ts + 1 min) |
| R6 | `book_top` (if available) | venue, symbol, ts | bid, bid_qty, ask, ask_qty | ts; **diagnostic only** (S-4.5): never alters any V7 verdict or frozen proxy parameter |
| R7 | `book_depth` (if available) | venue, symbol, ts, pct_level | cum_notional | ts |
| R8 | `open_interest` (optional) | venue, symbol, ts | oi, oi_value | ts |
| R9 | `fees` | venue, product, tier, effective_from | maker, taker, effective_to, source_url, evidence_record_sha256 (DEC-39 provenance record) | constants |
| R10 | `aggtrades_cache` | venue, symbol, trade_id | price, qty, ts, is_buyer_maker | ts; fetched only for (symbol, UTC day) pairs containing a simulated `E_M` order |
| R12 | `terminal_settlements` | venue, contract_instance_id | settlement_ts, settlement_price, source, source_timestamp / `known_at`, retrieval timestamp, sha256 | source_timestamp. **Optional table (EC-7), valid with zero rows.** Populated only for an A — exact settlement reconstructible event (S-5.4): only exact official values or exact reproductions of the official formula. The `W_DEV` audit found A = 0, so it is empty by design. No placeholder, approximate or synthetic settlement row is ever permitted |
| R11 | `notices` | venue, notice_id, version | kind (delisting / postponement / cancellation), matched instances (S-2.5), `releaseDate`, archive URL and capture timestamp (forward: `recv_ts`), content sha256, `scheduled_delist_ts` per matched instance (nullable) | max(`releaseDate`, capture timestamp); forward: `recv_ts` (S-2.5, DEC-36) |

| # | Derived table | Columns |
|---|---|---|
| DT1 | `candidates` | decision_ts, venue, symbol, side, U1–U8 flags, features (§9), `max_known_at` (asserted ≤ decision_ts − 1 min) |
| DT2 | `labels` | decision_ts, venue, symbol, side, h, every S-5.3 component, `net`, `y_pos`, `label_known_at`, `invalid_reason` |
| DT3 | `folds` | fold_id, block boundaries (S-10.4), test_lo, test_hi |
| DT4 | `runs` | code commit, config sha256, data manifest sha256s, seeds, library versions |

Kraken raw data come only from the frozen V5 copy (`var/history_v5/frozen`, verified manifest) and, for funding before 2025-10-06, the frozen V4 history. Nothing else from Kraken is read (S-1.3).

---

## 4. Decision clock, execution timing, costs

**S-4.1 Cadence: every 8 h; decisions at 00:05, 08:05, 16:05 UTC.** The rationale was fixed before any V7 data were inspected:
1. **Settlement alignment.** These are 5 minutes after Binance's standard 8-hour settlements, so every decision sees the freshly settled rate (`known_at` = settle + 1 min).
2. **No sub-minute funding capture.** Entry fills at `t_e` = `t` + 15 min = hh:20, and the exit at `t_e` + h is also hh:20. Settlements are always on the hour, so none lies on a holding-window boundary.
3. **Comparable to the horizons.** 24 / 72 / 168 h are 3 / 9 / 21 decision steps.
4. **Turnover.** An hourly clock multiplies turnover and costs without a pre-registered reason. A daily clock ignores the intermediate settlements.

Contracts with 4 h or 1 h funding intervals still settle inside holding windows; their features are normalized per 8 h (§9).

**S-4.2 Execution delay.** Fills happen at `t_e` = `t` + 15 min. Forced risk exits fill on the first 1-minute bar after the trigger + 1 min.

**S-4.3 Fees (DEC-18, DEC-23, DEC-39).**
- **Tier:** Binance USD-M Regular User / VIP 0, USDT-margined. No alternate-tier shopping.
- **Frozen values:** **maker 0.0200 %, taker 0.0500 %**; BNB fee discount OFF.
- **Official fee evidence (DEC-39, replaced 2026-10-14):**
  - Source: the official Binance Support article "Binance Futures Fee Structure & Fee Calculations" (360033544231), in two official locales: `https://www.binance.com/en-AU/support/faq/detail/360033544231` and `https://www.binance.com/en-PH/support/faq/detail/360033544231`.
  - Both state under "How to calculate trading fee of USDS-Margined contracts?" that a Regular User's maker fee is 0.02 % and taker fee is 0.05 %. Recorded 2026-10-14 UTC.
  - **Provenance caveat:** the V7 research environment could not independently render or read these pages (regional redirect / AWS WAF). This is not independently verified raw-page evidence; the closure relies on the pre-freeze governance decision that accepts qualified official-source provenance.
  - Provenance record: `docs/results/v7-stage0/binance-fee-evidence.md`, sha256 `63a9fa95ca32ea7d0015fd19dd7cce2d3549cec6b3feb740761794f5e135efb7`, recorded in R9.
- **Evidence standard (DEC-39):** qualified official-source retrieval evidence is sufficient; a locally preserved raw HTML/PDF artefact is not required. Qualified means all of:
  - binance.com;
  - an official Support or fee page (no Binance Square, community, blog, Reddit or third-party content);
  - Regular User USDⓈ-M maker / taker stated explicitly;
  - two official locale URLs of the same article, or one support article plus one fee page;
  - URLs, UTC date, retrieval method, excerpt and the AWS WAF / regional-redirect limitation recorded.
- The failed capture files in `var/v7/stage0/fees/` (zero-byte files, the UK spot-fee PDF, Wayback captures) are never cited as evidence.
- **BNB discount (DEC-13):** off in the baseline; "on" is reported as a sensitivity only.
- **Kraken:** the V6 frozen fee snapshot.

**S-4.4 Spread and slippage.**
- **Decision-time estimate `ĥ(t)`:** the S-4.5 proxy value at `t`.
- **Fill-time half-spread `h_fill(τ)`** (used for `h_a`, `h_B` in labels and for every executed-book fill; DEC-40): the S-4.5 proxy value recomputed point-in-time at the fill timestamp τ with the same frozen 24 h rule, using only bars fully known by τ − 1 min. No R6.
- **Missing proxy at a fill (DEC-40):**
  - an entry, increase or resize that needs the proxy: no trade. A label whose entry proxy is missing is invalid (S-5.5);
  - an exit (forced, risk or terminal exit, a decision exit, which still runs under S-7.3 staleness, or a label exit) with a valid reference price: half-spread 25 bps and slippage 3 × slip, reported as a fallback;
  - if the required reference price itself is missing, the existing invalid / gap policy applies (S-5.4, S-5.5). No price is invented.
- **Slippage** `slip` = 2 bps per side.
- **Liquidity cap:** a leg's notional ≤ 0.5 % of the symbol's trailing 24 h quote volume. Larger orders are reduced deterministically.

**S-4.5 Spread source and proxy (DEC-22 as amended by DEC-38; replaces DEC-11's data-chosen branch).**

**Source policy: proxy only, in every window.** F6/F14 features, the U5 gate, the A0 cost/hurdle, label costs and every executed-book spread cost use the proxy below. `W_DEV`, `W_HOLD` and `W_FWD7` therefore use the same spread source. R6 / book-top spreads are **diagnostic only**: they can never alter the V7 verdict or the frozen proxy parameters. The full 1.06 TB R6 dataset is not downloaded or processed for the primary research.

**λ = 2.0, fixed.** It is a pre-outcome research decision and is never reopened because results are weak. It is **not** described as conservative: synthetic measurement-error analysis (red team round 2) shows the proxy can understate the true spread. That risk is handled by the strengthened P6 cost stress (S-13.2, DEC-38), not by changing the proxy.

**Exact estimator (DEC-38).** The published Abdi & Ranaldo (2017) estimator ("A Simple Estimation of Bid-Ask Spreads from Daily Close, High, and Low Prices", RFS 30(12), 4437–4480), applied at the rolling-window level to consecutive 15-minute last-price bars (R3) of one instance:
- For bar k: log close c_k = ln C_k; log mid-range η_k = (ln H_k + ln L_k)/2.
- **Pair product** for consecutive bars (k, k+1): π_k = 4 · (c_k − η_k) · (c_k − η_{k+1}), with `known_at`(π_k) = close of bar k+1.
- **Window at τ:** 𝒫(τ) = the pairs whose two bars are both present and consecutive (no missing bar between them), whose bar k opens ≥ τ − 24 h − 15 min, and with `known_at` ≤ τ − 1 min (up to 95 pairs).
- **Full proportional spread:** Ŝ(τ) = sqrt(max(mean_{k ∈ 𝒫(τ)} π_k, 0)). The products are averaged first; truncation at 0 applies only to the window mean. Per-pair truncation and medians are not used, because they are not the published window-level estimator.
- **Proxy value:** ĥ(τ) = max(λ · Ŝ(τ) / 2, 1 bp). In order: full spread → ÷ 2 (half-spread) → × λ → the 1 bp half-spread floor.
- If 𝒫(τ) is empty, the value is missing: a staleness failure at decisions (S-7.3), and the S-4.4 missing-proxy rule at fills. No other sufficiency threshold applies; U4 already bounds the bar coverage of eligible candidates. Exchange-supplied flat zero-volume bars give π = 0 and pull Ŝ toward the floor; U8, U2 and the stressed halted exit limit their effect (acknowledged property).
- **Required tests (before the code freeze):**
  - hand-worked examples: a window whose mean product is negative (→ Ŝ = 0 → floor), a window where a missing bar breaks a pair, a single-pair window, and a floor case;
  - equality on fixed vectors with an independent reference implementation of the published equation: CRAN `bidask` 2.1.5, method `"AR"` with `sign = TRUE`, negative window values then set to 0;
  - synthetic tests on simulated OHLC paths (e.g. `bidask::sim`) that check only equation equivalence and invariants: scale invariance, constant prices → floor, the λ-then-floor order. No calibration on market outcomes.

No outcome and no R6 value may be used to alter this rule.

**S-4.6 Funding payment convention (Binance).**
- A position open at a settlement pays or receives `qty × mark(settle) × rate`; a long pays when rate > 0.
- Missing `mark_at_settle` → the mark close of the 15-minute bar ending at the settlement → else the last-price close.
- Missing funding records make a label invalid (S-5.5).

---

## 5. Candidates and labels (exact)

**S-5.1 Candidate.** `c = (venue v, symbol a, side s, decision t)` with s ∈ {+1 long, −1 short}. Both sides of every eligible symbol are candidates.

**S-5.2 Reference prices (PIT).**
- `P(τ)` = the open of the 1-minute last-price bar starting at τ (R5); fallback: the open of the 15-minute bar starting at τ.
- `M(τ)` = the mark price per S-4.6.
- BTC reference `B(τ)`, defined the same way.
- **Point-in-time beta β̂_a(t)** (DEC-16):
  - inputs: hourly log returns of a and BTC over the 30 d ending at the last full hour ≤ `t` − 1 min, requiring ≥ 500 joint observations;
  - formula: β̂ = clip(0.7·β_OLS + 0.3·1.0, 0, 3);
  - frozen at decision time.

**S-5.3 Leg label — a scoring measurement, not an executed hedge.** Per 1 USD of entry notional, horizon h, `t_e`, `t_x` = `t_e` + h:
- Leg quantity `q` = 1 / P(t_e).
- **Pro-forma** hedge quantity `q_B` = β̂_a(t) / B(t_e). The hedge side is −s. **This hedge is never executed.**
- Taker fee `f`; `slip`; realized half-spreads `h_a`, `h_B` at fill times.

Fill prices:
- Leg entry: `E = P(t_e)·(1 + s·(h_a(t_e) + slip))`
- Leg exit: `X = P(t_x)·(1 − s·(h_a(t_x) + slip))`
- Hedge entry: `E_B = B(t_e)·(1 − s·(h_B(t_e) + slip))`
- Hedge exit: `X_B = B(t_x)·(1 + s·(h_B(t_x) + slip))`

| Component | Formula |
|---|---|
| `funding_leg` | Σ over settlements τ_k ∈ (`t_e`, `t_x`]: −s · q · r_k · M(τ_k) |
| `price_resid` | s · [ (P(t_x)/P(t_e) − 1) − β̂·(B(t_x)/B(t_e) − 1) ] |
| `fee_in` / `fee_out` | f · q · E / f · q · X |
| `ss_in` / `ss_out` | q · P(t_e) · (h_a(t_e) + slip) / q · P(t_x) · (h_a(t_x) + slip) |
| `hedge_cost` | f·q_B·E_B + f·q_B·X_B + q_B·B(t_e)·(h_B(t_e)+slip) + q_B·B(t_x)·(h_B(t_x)+slip) |
| `hedge_funding` | Σ over BTC settlements τ_j ∈ (`t_e`, `t_x`]: +s · q_B · r^B_j · M^B(τ_j) |

**net(h) = funding_leg + price_resid − fee_in − fee_out − ss_in − ss_out − hedge_cost + hedge_funding**

`hedge_funding` is signed: a credit when the hedge side receives funding, a debit when it pays.

- `y_reg(h)` = net(h) in bps.
- `y_pos(h)` = 1[net(h) > 0].
- Labels use **taker** costs only. Maker execution exists only in the book backtest (§6).
- The labels are conservative: each leg is charged a full pro-forma hedge. **Only the executed book (§7, §8) determines acceptance.**

**S-5.4 Halts, exits and terminal unwind (exact; DEC-35).** The normal exit is at `t_x`, with taker costs as in S-5.3.
- **HALTED / UNTRADABLE:** an instrument is halted at τ when it has no 1-minute last-price bar (R5) with volume > 0 opening in [τ − 60 min, τ). The rule is notice-independent and point-in-time.
- **While halted:** no new orders and no resizes. A fill is never booked retroactively at the last traded bar.
- **Exit from a halted instrument.** An exit due at τ while the instrument is halted (a label exit at `t_x`, a decision, risk or forced exit, or the terminal unwind) waits for the first subsequent executable reference: the first 1-minute bar of the same instance with volume > 0 opening at ≥ τ. If the instance reaches official cessation first, the terminal-settlement rule below applies.
  - The fill is at that bar's close, at its close time (resumed trading becomes known only when the bar closes).
  - The exit is taker, with stressed costs: half-spread max(`h_fill`, 25 bps) and slippage 3 × slip. Funding counts up to the fill.
  - For a label, `t_x` in S-5.3 and S-5.6 becomes that fill time and P(`t_x`) that bar's close; the pro-forma BTC hedge exits at the same time.
  - **E10: instances are economically separate instruments.** A pending exit from one instance can never be filled, settled or priced with a later instance's bars, funding, index or order-book data.
  - If the instance hasn't reached cessation and no executable bar of the same instance appears before the window's terminal unwind (S-1.5, S-8.1), the label is invalid (S-5.5) and an executed-book run holding the position is `INVALID RUN` (S-13.4). No synthetic fill is ever created.
- **Terminal settlement of a contract that ceases trading (M3-1, DEC-43).** Binance's published delisting rule settles a position still held at cessation automatically: the settlement price is the average of the price index sampled every second over the final 30 minutes before delisting (1,800 observations), and the settlement fee is the taker fee. Every terminal event (an instance end reached by an open position or label, S-2.2) is classified:
  - **A — exact settlement reconstructible:** the official scheduled settlement time comes from an immutable timestamped notice (S-2.5), and the exact official settlement price comes from an official historical source or an exact reproduction of the official formula. The position closes by automatic settlement, not a simulated market fill: timestamp = the official scheduled settlement time; price = the exact reconstructed official settlement price; fee = the frozen taker fee; no spread or slippage on top, unless Binance's official rule explicitly requires it. A record exists only in R12.
  - **B — settlement time known, exact settlement price not reconstructible**, and **C — official termination notice or time not reconstructible:** no fill is invented. In particular: no later instance, no stale last trade, and no approximation of the official price (1-minute or 15-minute bars never stand in for the 1-second index average).
  - **Labels:** a label reaching a B / C event is invalid (S-5.5); a label reaching an A event uses the A settlement as its exit (`t_x`, P(`t_x`)).
  - **Books:** if an actually-held position in an evaluated book reaches a B / C event, that arm's run is `INVALID RUN — terminal settlement not reconstructible` (S-13.4). In the forward run, this applies to ML\* only; an A0 event makes A0 unavailable as a reference (§15, EC-11). The mere existence of a B / C event in the eligible universe invalidates nothing if that book never held the contract.
  - This rule is deliberately strict, so that missing historical execution evidence can't be converted into favourable synthetic P&L. It applies identically in `W_DEV`, `W_HOLD` and the forward run.
  - **E11 status (M3-1 settlement-reconstruction audit, `docs/results/v7-stage0/m31/M3-1-settlement-feasibility.md`):** the `W_DEV` eligible terminations are **0 A, 14 B, 12 C**. No official historical settlement-price source exists and the 1-second index observations are not archived (finest archived index granularity: 1 minute). R12 is therefore empty by design (an optional table valid with zero rows, EC-7), and no placeholder, approximate or synthetic row may ever be inserted into it.
- **Entry while halted:** no order. A label whose instrument, or whose BTC reference B(τ), is halted at `t_e` is invalid (S-5.5).
- **Data gap at τ (not halted):** use the first 1-minute bar within 60 min after τ. If there is none, the label is invalid.
- **`t_x` at or past the window end:** no label (S-1.5).
- The previous "exit at the last volume > 0 bar + 1 min" rule is removed.

**S-5.5 Invalid labels.**
- A label is invalid if a funding record in (`t_e`, `t_x`] is missing or invalid (S-2.6), a required reference price is missing, its instrument is halted at `t_e`, it reaches a B / C terminal event (S-5.4), or its entry proxy is missing (S-4.4).
- Invalid labels are counted per fold and per symbol, reported, and excluded from fitting.
- **Pre-freeze input-availability audit (DEC-27, outcome-free, `W_DEV` only).** Before freeze, it is verified that the required 1-minute reference bars, funding event records and mark-price fallbacks exist for the `W_DEV` label inputs, without computing any label value, return or P&L. If the structural invalid-input rate exceeds 0.5 %, the data decisions are reopened before freeze. The audit was re-run after DEC-35 (U8 and activity-based instances). No post-freeze trigger is attached to it; K8 alone governs the holdout run.

**S-5.6 `label_known_at`** = `t_x` + 1 min.

---

## 6. Execution models

**S-6.1 `E_T` — pure taker (hard baseline).** Every entry, exit, resize and hedge trade fills at taker prices (S-5.3 price rules). **Every acceptance criterion is evaluated under `E_T`.**

**S-6.2 `E_M` — conservative maker-first (secondary).** Normal entries, exits and resizes only. Risk exits, forced exits and hedge trades are always taker.

1. **Post time:** at `t_e`, a limit at the touch: best bid for a buy, best ask for a sell.
   - The touch is P(t_e) ∓ ĥ(t), using the S-4.5 proxy decision-time estimate, never a realized value and never R6 (DEC-22).
2. **Fill rule — trade-through only:**
   - a buy at L fills only from aggTrades priced **strictly below** L; a sell at L only from trades strictly above L;
   - trades exactly at L (touch) never fill;
   - the filled quantity = min(remaining, cumulative trade-through quantity), from trades with timestamp > `t_e`, within W = 30 min;
   - **no queue-position or touch-fill assumption** is made anywhere.
3. **Fallback:** at `t_e` + 30 min, any remainder fills taker.
4. **Fees:** maker fee on the maker-filled part, taker fee on the remainder.
5. **Reported:** the adverse mid move 5, 30 and 60 min after each maker fill.

**S-6.3 Maker cannot rescue a verdict.**
- PASS requires the criteria under `E_T`.
- If P1 holds only under `E_M`, the verdict is **NO-GO: maker-dependent** (S-13.4).
- `E_M` never uses L2 queue or depth data, whatever exists.

---

## 7. Arms, constructor, risk layer

**S-7.1 One constructor and one risk layer for all arms.** Only the scoring changes between arms. Equity is per §14; the primary layer is normalized (E = 1).
- **Book:** at most **K = 5** legs per side (DEC-15; frozen, no breadth fallback; insufficient breadth is a Stage 0 FAIL, S-10.2).
  - The long side is filled from long candidates, the short side from short candidates.
  - Legs are equal-weighted within a side; each side's gross is 0.5 · E.
  - The book is dollar-neutral: |Σ long − Σ short| ≤ 0.05 · gross.
- **Entry:** a candidate enters only if all of the following hold:
  - it passes U1–U8;
  - it is in the top K of its side by the arm's score;
  - the score is above the arm's hurdle (S-7.2);
  - no cooldown;
  - the asset isn't held on the other side (no same-decision flip).
- **Hold and exit:**
  - Minimum hold = h\* (S-10.6).
  - After the minimum hold, a leg exits at a decision if it fails a gate, ranks outside K + 2 on its side, or its score falls below the hurdle.
  - Before the minimum hold, a leg exits only through a risk exit (S-7.3).
- **Resizes:** to the equal-weight target only when a leg's notional drifts > 25 % from its target. Reduce-only between entries. No order of any kind while the instrument is halted (S-5.4).

**S-7.2 Arms.**

| Arm | Score and hurdle | Role |
|---|---|---|
| **A0 rule** | `s_rule(a,side,h*)` = c(a,side)·h\* − 1.5 · 2 · cost(a) − 5 bps, where `c` is the frozen V5 `carry_per_hour` on FL30 and `cost` is the V5 per-side cost (taker fee + ĥ(t) + slip). Hurdle: `s_rule` > 0 | Frozen rule baseline (same constructor) |
| **A1 rule + ML veto** | A0's entries, minus those with calibrated P̂(net(h\*) > 0) < **0.50** (DEC-8). P̂ comes from the fixed classifier of the same family as ML\* (logistic EN if ML\* = A2; LightGBM classifier if ML\* = A3). Ranking: A0 order | Secondary |
| **A2 ML ranker – linear** | Ranking: ŷ(h\*) from Elastic Net. Hurdle: calibrated ŷ_cal(h\*) > 0 bps | Candidate for ML\* |
| **A3 ML ranker – GBM** | As A2 with the LightGBM regressor | Candidate for ML\* |
| **A0-V5 reference** | The frozen V5 `strategy.py` function, unchanged, with its frozen config (k = 2, H = 168, mult 1.5, buffer 5 bps, min hold 24 h), on the V7 Binance universe | Reference only (no test) |
| **TS references** | A0's rule with the FL30 forecast replaced by EWMA(r₈, half-life 72 h) or by an AR(1) on r₈ fitted on the last 30 d and projected to h\* (DEC-20) | Reference only (no test) |

**ML\*** = A2 or A3, selected on `W_DEV` by S-10.6, together with h\*. A0 uses the same h\*.

**S-7.3 Deterministic risk layer** (identical for every arm; ML has no access):
- **Leg stop:** adverse move ≥ 15 % from the average entry price → taker exit, 24 h cooldown.
- **Daily loss:** equity ≤ 98 % of the 00:00 equity → no entries until the next 00:00 UTC.
- **Drawdown kill:** equity ≤ 85 % of the running peak of the 15-minute marked equity → exit everything, no re-entry for the rest of the run.
- **Gross** ≤ 1.0 · E. **Per leg** ≤ (0.5 · E) / K. The S-4.4 liquidity cap.
- **Ineligibility exit:** failing U4, U5 (exit threshold 20 bps half-spread) or U7 on two consecutive decisions → taker exit.
- **Delisting notice (U6, S-2.5):** an open leg exits at the first decision at or after the matched version's `known_at` + 1 min, filled at that decision's `t` + 15 min (S-4.2) as a taker exit with normal S-5.3 costs. If the instrument is halted then, S-5.4 applies.
- **U8 false:** no entry or increase; it doesn't trigger an exit. A halted instrument follows S-5.4.
- **Staleness:** a decision with any required input past its freshness rule is skipped for entries; exits still run.

**S-7.4 The single book-level residual BTC hedge:**
- At each decision, after the leg targets are set: β_book = Σ_i n_i · β̂_i(t).
- If |β_book| > 0.05 · E, the target hedge is H_B = −β_book; otherwise the target is 0.
- The hedge trades (taker) only if |H_B − current| > 0.02 · E, or when the target is 0 and a hedge is open. |H_B| ≤ 0.5 · E.
- Its funding, fees, spread and slippage enter book accounting (§8). **There are no per-leg executed hedges in any arm.**

---

## 8. Book accounting objects (the only canonical definitions)

Every evaluation window starts flat and ends flat. This section is the only place the following objects are defined; every other section refers to it.

**S-8.1 Fills, marks, terminal unwind.**
- Every fill is booked at its own timestamp.
- Equity is marked to the mark price at every 15-minute tick: E = cash + Σ q · mark.
- Funding is booked at settlement times.
- **Terminal unwind:** at the last reference minute before the window's end (S-1.5), every leg and the hedge exit taker with S-5.3 cost rules.
- **Daily table** (UTC days): net, funding, fees, spread+slip, price. The components sum exactly to the daily equity change.

**S-8.2 Funding, costs, price, net.**

| Symbol | Definition |
|---|---|
| F_leg | Σ of signed funding cash flows on all legs (credits > 0, debits < 0) |
| F_hedge | Σ of signed funding cash flows on the BTC residual hedge |
| **F_net** | F_leg + F_hedge |
| **C_exec** | Σ over every fill (legs, hedge, resizes, forced exits, terminal unwind) of fees + spread + slippage, all ≥ 0 |
| **PR** | Σ_j PR_j over every instrument j (legs and BTC hedge), with PR_j defined in S-8.3 |
| **NET** | F_net − C_exec + PR (equals the window's total equity change) |
| **CAC** | F_net − C_exec (carry after cost) |

**S-8.3 Executed price P&L and its exact attribution.**
- **Event times of j:** every fill of j, and every 15-minute tick while j is held. If a fill and a tick share a timestamp, the tick comes first; same-timestamp fills keep booking order.
- **p_j(e):** the reference mid at a fill (the mid against which spread and slippage are charged), the mark at a tick.
- **q_j(e⁺):** the position immediately after e.
- **PR_j = Σ_k q_j(e_k⁺)·(p_j(e_{k+1}) − p_j(e_k)) = −Σ_{fills f of j} dq_f · p_j(f).** Exact by summation by parts, since q is 0 at both ends. PR equals the daily table's price column.
- **Episode:** a maximal interval during which a leg asset's position is non-zero with constant sign.
- **Cluster c:** a maximal set of episodes of the same (asset, side) where each episode starts ≤ 72 h after the previous one ended. Every leg fill belongs to exactly one episode and cluster.
- **PR_c:** the event sum of S-8.3 restricted to c's episodes. Σ_c PR_c = Σ_{j ≠ BTC} PR_j exactly.
- **mkt_c** = Σ over c's events of q(e_k⁺)·p(e_k)·β̂_episode·(B(e_{k+1}) / B(e_k) − 1).
  - β̂_episode is the point-in-time β̂ of the decision that opened the episode, frozen for the episode.
  - B(e) is BTC's reference mid at fills and its mark at ticks.
- **ρ_c = PR_c − mkt_c** (the cluster's residual price contribution).
- **MKT = Σ_c mkt_c + PR_BTC:** one book-level market-beta unit. The hedge is never split across clusters, and nothing is divided by β_book.
- **Invariant (K10):** Σ_c ρ_c + MKT = PR, within 1e-9 × Σ|fill notional|.

**S-8.4 Per-asset net attribution** (for P5):
- N_a = Σ over a's clusters of (leg funding + PR_c − leg C_exec).
- Hedge unit H = F_hedge + PR_BTC − hedge C_exec.
- NET = Σ_a N_a + H exactly.

---

## 9. Features

All features are point-in-time (`known_at` ≤ `t` − 1 min) and computed per (symbol, decision).

**Side adjustment.** Carry-direction features are multiplied by −s, so that positive means favourable to the candidate's side. A side indicator is included.

**Funding normalization.** r₈ = r · 8 / interval_h, with interval_h per S-2.6.

**Instance boundary.** Every look-back uses only data of the current contract instance (S-2.2, DEC-24).

| ID | Feature(s) | Definition |
|---|---|---|
| F1 | Funding level | mean r₈ over the last 24 h, 72 h, 7 d, 30 d of settled records (FL1, FL3, FL7, FL30) |
| F2 | Persistence | share of the last 30 d of settlements with the same sign as FL1; lag-1 autocorrelation of r₈ over 30 d |
| F3 | Slope / acceleration | OLS slope of r₈ over 7 d; FL1 − FL7; (FL1 − FL3) − (FL3 − FL7) |
| F4 | Sign reversals | sign changes of r₈ over 30 d ÷ number of settlements |
| F5 | Basis / premium | mean premium index over 8 h and 24 h (R4; fallback (mark − index)/index); last premium close |
| F6 | Spread | ĥ(t) (S-4.5 proxy); its 7 d median |
| F7 | Depth | cumulative notional within ±1 % (R7), 24 h median. Kept only if Stage 0 coverage ≥ 95 % of `W_DEV` decisions, else dropped for every model |
| F8 | Liquidity | log median daily quote volume over 30 d; Amihud illiquidity over 7 d; log trades per day over 7 d |
| F9 | Volatility | realized vol of hourly log returns over 24 h and 7 d (annualized) |
| F10 | Momentum | returns over 24 h, 7 d, 30 d; residual return vs BTC over 7 d using β̂ |
| F11 | BTC / market regime | BTC 30 d return; BTC 7 d realized vol; cross-sectional median FL1; share of eligible symbols with FL1 > 0 |
| F12 | Cross-sectional rank | percentile ranks of FL1, FL7, FL30 and F5 within the eligible universe |
| F13 | Price/funding vol ratio | 7 d realized vol ÷ (\|FL7\| · 3 · 365); 30 d std of r₈ ÷ \|FL30\| (both capped at 10⁴) |
| F14 | Expected execution cost | 2·f + 2·ĥ(t) + 2·slip (bps); (\|FL7\| · 3 · h/24) ÷ that cost |
| F15 | Open interest (optional) | 7 d log change of OI value; the same ≥ 95 % coverage rule as F7 |

- Linear models: training-fold winsorization at 1 / 99 %, standardization, median imputation plus a missing-indicator column.
- LightGBM: raw values, native missing handling.
- The feature set is fixed. Stage 0 applied the coverage rule: **F7 and F15 are dropped for every model** (coverage below 95 %).

---

## 10. Validation design (walk-forward, purged, three-block calibration)

**S-10.1 No random splits.** Every split is temporal.

**S-10.2 Outer walk-forward on `W_DEV`.**
- Expanding training set from D0.
- Test folds are consecutive calendar quarters, starting with the first quarter that begins ≥ D0 + 24 months (S-10.4 needs ≥ 24 months), and ending with 2025Q2.
- For D0 = 2021-01-01: **10 folds, 2023Q1–2025Q2.**
- **STAGE-0 FAIL / insufficient historical breadth** (DEC-15) if either:
  - no month satisfies the frozen S-1.8 D0 rule; or
  - the resulting fold count is < 8 (i.e. D0 later than 2021-07-01).
  
  V7 then stops and returns to the user for an explicit new-spec decision. K is never reduced and D0 is never redefined after seeing the data.

**S-10.3 Purge.**
- A sample is admissible for any later block or fold starting at T only if `label_known_at` ≤ T − 24 h.
- Training is forward-only, so no post-test embargo is needed.

**S-10.4 Three-block protocol (DEC-3).** Every outer training set with cutoff C (the test start) is split chronologically:

| Block | Span | Role |
|---|---|---|
| `T_tr` | [D0, C − 12 mo), ≥ 12 months | fit candidate models for hyperparameter selection |
| `T_sel` | [C − 12 mo, C − 6 mo) | hyperparameter selection by date-level IC (regression); LightGBM early stopping on the `T_sel` loss (l2 / logloss); never P&L |
| `T_cal` | [C − 6 mo, C) | **calibration tail only.** Never used to fit base-model weights, select hyperparameters or early-stop |

The S-10.3 purge applies at each block boundary.

1. **Base model for the fold:** refit on `T_fit` = `T_tr` ∪ `T_sel` with the selected hyperparameters (LightGBM: the early-stopped tree count from `T_sel`). `T_cal` is never absorbed.
2. **Calibrators** are fitted on predictions of that exact refit model on `T_cal`. No observation trains the prediction used as its own calibration target.
3. **Regression recalibration:**
   - ŷ_cal = a + b·ŷ;
   - (a, b) by OLS of realized `y_reg` on ŷ over `T_cal`, every decision date weighted equally;
   - b is clipped to ≥ 0. If b = 0: entries iff a > 0, ranking by ŷ.
   - Ranking is unaffected (the map is monotone); the calibration sets the hurdle and P8.
4. **Probability calibration:** isotonic regression of `y_pos` on P̂ over `T_cal`, date-equal weights, outputs clipped to [0.001, 0.999].
5. **Outer-fold evaluation:** the refit model plus its calibrators predict the test quarter.
6. **Final model before `W_HOLD`:**
   - `T_tr` = [D0, 2024-07-01), `T_sel` = [2024-07-01, 2025-01-01), `T_cal` = [2025-01-01, 2025-07-01), each with the purge.
   - One frozen, hashed model and calibrator pair, applied unchanged to all of `W_HOLD`.
   - **No refit inside the holdout.**

**S-10.5 Search space (DEC-9): 14 configurations per horizon, 42 in total.** Linear and LightGBM regressors get equal budgets.

| Model | Task | Configurations | Count per horizon |
|---|---|---|---|
| Elastic Net | y_reg | α ∈ {1e-3, 1e-2, 1e-1} × l1_ratio ∈ {0.2, 0.8} | 6 |
| LightGBM regressor | y_reg | num_leaves ∈ {7, 15, 31} × min_data_in_leaf ∈ {500, 2000} | 6 |
| Logistic EN | y_pos | **fixed:** α = 1e-2, l1_ratio = 0.5 | 1 |
| LightGBM classifier | y_pos | **fixed:** num_leaves = 15, min_data_in_leaf = 2000 | 1 |

LightGBM fixed settings (both tasks):
- learning_rate 0.03; lambda_l2 5; feature_fraction 0.8; bagging_fraction 0.8 (freq 1);
- max 1500 trees; early stopping after 100 rounds on the `T_sel` loss.

Seeds are fixed.
- **Selection metric** (regressors only): the mean over decision dates of the Spearman correlation between ŷ and realized `y_reg` (date-level IC). It is never P&L.
- Classifiers aren't selected. They are used only by A1 and P8-classifier.

**S-10.6 Choosing ML\* and h\* — the only dev-level economic choice.**

**Validity precondition (M3-1, DEC-43).** All six pre-registered candidates (A2 / A3 × 24 / 72 / 168 h) and A0 at each of the three horizons must be valid runs. If any of these runs is `INVALID RUN` under S-5.4 (an actually-held position reached a B / C terminal event, or a halted held position had no executable exit before the window end), V7 stops before selection with **`INVALID RUN — historical terminal execution not reconstructible`**. No candidate is dropped, replaced or re-run, and the holdout stays sealed. **A0 must be valid at each of 24, 72 and 168 h before h\* is selected (EC-2)**, so the selected horizon never inherits a baseline-validity condition discovered after selection. If A0 is invalid at any of them because of an actually-held B / C event, the `W_DEV` selection run is INVALID before h\* selection. Reference arms (A0-V5, TS) that are invalid are reported as invalid and have no test.

1. For each (A2, A3) × h ∈ {24, 72, 168} (6 candidates), run one continuous book backtest (§7, `E_T`, L-norm) over the concatenated outer test quarters. Each quarter uses its own fold model; positions carry across quarter boundaries; terminal unwind before 2025-07-01.
2. LB = r̄ − q₀.₉₅(r̄\* − r̄) from the S-13.1 circular block bootstrap with ℓ = 14 days and B = 10,000.
3. Select the highest LB.
4. **Simplicity rule:** A3 is chosen only if its LB exceeds the best A2 LB by ≥ 1 bp/day.
5. **Tie-break** (|ΔLB| ≤ 0.1 bp/day): the longer horizon.

**S-10.7 Development kill (before the holdout; only after the S-10.6 validity precondition holds).** The holdout is **not** opened, and V7 ends **NO-GO: no viable edge**, if any of these hold for ML\* out-of-fold on `W_DEV` (`E_T`, L-norm, §8 objects):
- mean daily net ≤ 0;
- F_net ≤ 0;
- C_exec / F_net > 1.0;
- **CAC ≤ 0;**
- fewer than 30 % of outer folds have positive net.

P11-R2′ is reported on `W_DEV`, not a kill.

---

## 11. Leakage controls

| ID | Control | Verification |
|---|---|---|
| L1 | Every feature column carries `known_at`; the candidate builder asserts `max_known_at` ≤ `t` − 1 min. **Identity whitelist (DEC-31):** only immutable contract-identity fields verified invariant for the instance (base, contract type, underlying type, multiplier) take `known_at` = the instance's `first_bar_ts`; every time-varying field keeps its actual timestamp | per-feature tests with synthetic future spikes 1 min after `t`; whitelist tests |
| L2 | Point-in-time universe (S-2.2/2.3): no present-day list; delisted symbols retained; activity-based instances, no bridging of dormant intervals; U8; notice versions only from their `known_at` (S-2.5) | live-today-listed-later absent at `t`; delisted present before delisting; dormant contracts never pass U8; no rolling feature or funding window crosses an instance boundary; an instance end is never used before `instance_end_known_at`; no cross-instance fill or settlement; current CMS text never backdated |
| L3 | Labels use only (`t_e`, `t_x`]; training uses only labels with `label_known_at` ≤ cutoff; S-1.5 window ends | synthetic spike at `t_x` + 1 min and at window end |
| L4 | Purge (S-10.3) at every block and fold boundary | fold-builder tests |
| L5 | Winsorization, scaling and imputation fitted on `T_fit`; calibrators on `T_cal` only (S-10.4) | transformers refuse test or calibration indices |
| L6 | Hyperparameters and early stopping only on `T_sel` | tests |
| L7 | No feature selection on pooled data | review |
| L8 | Fee constants frozen; spread proxy and λ = 2.0 frozen; R6 never feeds any decision, cost, feature or verdict (S-4.5) | review; tests that R6 isn't read by the pipeline |
| L9 | **Canaries**, both before the holdout opening: (a) labels permuted within decision dates → out-of-fold IC within ±2 SE of 0; (b) a feature equal to the label shifted 1 decision earlier is rejected by L1, and in a sandbox without L1 the out-of-fold IC jumps | `W_DEV` only |
| L10 | Holdout and M6 guards refuse overlap (S-1.3, S-1.7); the pre-opening endpoint allowlist refuses every non-allowed endpoint before network I/O (S-1.7, DEC-41); V6 boundaries read from the canonical artefacts (S-1.1) | guard tests, one refusal test per refused endpoint class (S-1.7 list, incl. redirect / timestamp substitution and current-state `fapi` paths), the forward-store read refusal before L7, hash checks |
| L11 | No V5/V6 outcome data as input; the Kraken outcome guard | audit plus tests |
| L12 | ns time units everywhere | ingest tests |
| L13 | Inference uses date-level aggregation and block bootstrap (never i.i.d. per row) | review |
| L14 | One holdout opening, exclusive markers, pinned manifest | V6-style guard tests |

L1–L14 apply to **every** model, classifiers included.

---

## 12. Metrics (every arm, execution model and equity layer)

**Economics** (§8 objects):
- NET, F_leg (credits / debits / net), F_hedge, F_net, C_exec (fees / spread / slippage; legs vs hedge), CAC, PR, MKT, Σ ρ_c;
- mean daily net return and its CBB interval;
- net expectancy per closed leg;
- trade count;
- turnover (Σ|traded notional| / E per year);
- C_exec / F_net;
- Sharpe (daily mean / std × √365, plus the Lo / Newey-West adjusted value);
- maximum drawdown on the 15-minute marked equity;
- hit rate (closed legs with net > 0);
- the daily decomposition.

**Price-residual reports:** the top-10 ρ_c; the per-asset ρ; NET_trim for m ∈ {1, 3, 5, 10}; R3 concentration (top-1 asset and top-5 cluster shares of the positive Σ ρ_c).

**Model quality:**
- date-level IC;
- the P8-primary regression calibration slope;
- P8-classifier: ECE, reliability curve and Brier, for A1 only;
- the out-of-fold vs `T_sel` gap.

**Stability:** per fold, per calendar year, per regime (BTC 30 d return terciles; market median-FL1 terciles; tercile edges from `W_DEV` only); concentration; capacity (leg notional ÷ 24 h volume).

---

## 13. Statistics and acceptance

**S-13.1 Bootstrap test (DEC-6).**
- **Daily returns:** r_t = (E_t − E_{t−1}) / E_{t−1} at 00:00 UTC, t = 1..T. The last day includes the terminal unwind.
- **Paired difference:** x_t = r_t^{ML\*} − r_t^{A0} over the same days.
- **Statistic:** the sample mean.
- **Circular block bootstrap:**
  - block length ℓ ∈ {14, 28} days;
  - k = ⌈T/ℓ⌉ uniform start indices; blocks wrap mod T; concatenated and truncated to T;
  - the **same draws are used for r and x** (paired);
  - B = 10,000; seed fixed and recorded.
- **Centered null:**
  - **p_abs(ℓ) = (1 + #{b : r̄\*_b − r̄ ≥ r̄}) / (B + 1)**, and likewise **p_diff(ℓ)** with x;
  - p_abs = max_ℓ p_abs(ℓ), p_diff = max_ℓ p_diff(ℓ).
- **Intersection-union:** the primary claim H_P = {μ_ML\* > 0} ∧ {μ_ML\* − μ_A0 > 0} is accepted iff **p_P = max(p_abs, p_diff) < 0.05**. The IUT has level ≤ α when each component has level α (Berger 1982); no adjustment is needed inside it.
- Non-stationarity within one year is a reported caveat.
- **One-sided lower bounds** used elsewhere (S-10.6, P7): LB = θ̂ − q₀.₉₅(θ̂\* − θ̂), the minimum over the stated block lengths.

**S-13.2 Acceptance on `W_HOLD`** (Binance, `E_T`, L-norm, ML\* at h\*). `V7 HOLDOUT ADVANCE` requires **all of P1–P11**:

| ID | Criterion |
|---|---|
| P1 | H_P holds: p_P < 0.05 (S-13.1) |
| P2 | F_net > 0 and C_exec / F_net ≤ 0.60 (§8 objects) |
| P3 | Maximum drawdown of the 15-minute marked equity ≤ 15 %, and no drawdown kill |
| P4 | Evidence: ≥ 60 active days (gross ≥ 10 % of E for ≥ 48 of 96 quarter-hours), ≥ 100 closed legs, ≥ 20 clusters (S-8.3) |
| P5 | Stability: net > 0 in ≥ 2 of the 4 holdout quarters; no quarter > 60 % of NET; using S-8.4, top-1 N_a ≤ 0.30 · NET and top-3 ≤ 0.60 · NET |
| P6 | Cost stress (DEC-38): the same executed book (decisions, quantities, fill times and reference prices unchanged) with every fill's fee × 1.5, slippage × 1.5 and spread charge × 3.0 (fallback and stressed-exit spreads included) → stressed mean daily net ≥ 0 |
| P7 | Date-level IC of the ML\* regression model: mean > 0 and LB > 0 (S-13.1, ℓ ∈ {14, 28}) |
| P8 | **P8-primary:** OLS slope (decision dates weighted equally) of realized `y_reg(h*)` on the ML\* regression model's calibrated ŷ_cal over every eligible `W_HOLD` candidate ∈ [0.5, 1.5] |
| P9 | Net Sharpe (daily, annualized √365) ≥ 1.0 |
| P10 | ML\* out-of-fold mean daily net on `W_DEV` > 0 |
| P11 | **R1 ∧ R2′:** CAC > 0, and NET_trim = NET − Σ_{c ∈ Top_m} max(ρ_c, 0) − max(MKT, 0) > 0, where Top_m are the m clusters with the largest positive ρ_c and m = max(3, ⌈0.05 · n_clusters⌉) (n_clusters = clusters with ≥ 1 closed episode) |

**S-13.3 Secondary comparisons** (reported; they never change S-13.2 or S-13.4). The CBB diff test of S-13.1 with Holm–Bonferroni across exactly this family of 6:
1. A1 vs A0.
2. The non-selected ML family vs ML\* at h\*.
3. ML\* at each non-selected horizon vs A0 (2 tests).
4. ML\* under `E_M` vs `E_T`.
5. ML\* Binance vs the same arm on Kraken (exploratory, S-1.6).

**P8-classifier:** ECE ≤ 0.05 over 10 equal-frequency bins (date-equal weights) of the A1 classifier's calibrated P̂ on `W_HOLD`.
- It is required only for claims about A1: A1 counts as better than A0 only if its Holm-adjusted test rejects **and** P8-classifier holds.
- **P8-classifier never affects S-13.2 or S-13.4.**

The A0-V5 and TS reference arms have no tests.

**S-13.4 Verdict classes**, most severe first:

| Class | Meaning |
|---|---|
| `INVALID RUN` | integrity, guard or K10 failure; or a required arm (ML\*, A0) whose actually-held position reached a B / C terminal event (`INVALID RUN — terminal settlement not reconstructible`, S-5.4). An invalid secondary arm voids only its own comparison |
| `NO-GO: leakage` | any L1–L14 failure in any model (classifiers included) |
| `NO-GO: no viable edge` | S-10.7 development kill, or P1 fails under `E_T` while it also fails under `E_M` |
| `NO-GO: maker-dependent` | P1 fails under `E_T` but holds under `E_M` |
| `INCONCLUSIVE` | P4 not met |
| `NO-GO` | any of P2, P3, P5, P6, P7, P8 (P8-primary), P9, P10, P11 fails |
| `V7 HOLDOUT ADVANCE` | all of P1–P11 hold |

The final class is the most severe one that applies. A positive cumulative P&L alone never yields ADVANCE.

---

## 14. Equity layers and deployment gate (DEC-7)

| Layer | Definition | Role |
|---|---|---|
| **L-norm** | Normalized book (E = 1): fractional quantities, no rounding, no minimum notional; liquidity cap evaluated at E_ref = 10,000 USD | **Primary scientific layer:** all of P1–P11, S-10.6, S-10.7, the forward test |
| **L-265** | The same frozen strategy at E = 265 USD: exchange step rounding, per-symbol minimum quantity / notional, K = 5 legs per side, the S-7.4 hedge rule including BTC's filters. Infeasible legs or hedges are not traded (fail closed); if a required hedge is infeasible, the beta rule forbids the entries. Exchange filters follow S-14.1 | **Mandatory deployment-feasibility layer;** never part of the scientific verdict |
| **L-10k** | E = 10,000 USD with rounding and minimum notional (filters per S-14.1) | Secondary capacity diagnostic only; it can never establish deployability for a 265 USD account |

**S-14.1 Exchange filters for L-265 and L-10k (DEC-21).** Today's `exchangeInfo` filters are never presented as historical truth. Stage 0 first searches for timestamped historical stepSize, minQty, `MIN_NOTIONAL` / `NOTIONAL` (or equivalent contract filters). Then:
1. **Reliable point-in-time historical filters exist** → they are used for the `W_DEV` / `W_HOLD` L-265 and L-10k runs.
2. **They don't exist** → current filters may be applied to `W_DEV` / `W_HOLD` **only as an explicitly labelled approximation / sensitivity**.
3. **In case 2**, the historical `W_HOLD` D1–D3 are reported as "approximate (current filters)". They are **never** described as exact historical deployability evidence.
4. **From `T_F7` onward**, the forward recorder snapshots the applicable exchange filters with timestamps and hashes (§15), so the forward D1–D3 use actual point-in-time filters.
5. **Any real-money deployment proposal** requires D1–D3 computed on the forward point-in-time filters. A historical approximation is never sufficient.
6. **Symbols missing from current `exchangeInfo` (DEC-34):** use a timestamped historical (e.g. Wayback) filter capture at or before the relevant period if one exists. Otherwise the symbol is excluded from exact L-265 feasibility, and the historical deployment result is flagged approximate/incomplete. Captures for the `W_HOLD` period are retrieved only at the legitimate opening (S-1.7, DEC-41).

**Granularity accepted (DEC-34).** E = 265 USD, no special L-265 rounding, the S-7.4 beta-hedge rule and D1 ≥ 90 % all stay as frozen. If BTC order granularity makes a scientifically positive strategy infeasible at 265 USD, the verdict is **GO — not deployable at 265 USD**.

**Separation of the two uses of current filters (EC-6).** There is no conflict between E8 (S-1.7) and DEC-21:
- **Point-in-time historical metadata (E8):** current `exchangeInfo` filters are never backdated and never represented as historical truth. They never enter R1 as historical filter values, any eligibility gate, any feature, any label or any L-norm book.
- **DEC-21 approximation layer (step 2):** current filters may be applied to historical `W_DEV` / `W_HOLD` runs only inside the explicitly labelled L-265 / L-10k approximation / sensitivity layer. They are held there as an "approximation (current filters)" input and never become point-in-time historical metadata.
- That approximation never affects P1–P11 or L-norm.
- Exact deployment evidence requires point-in-time forward filters (steps 4–5).

S-14.1 doesn't affect P1–P11 or the L-norm scientific verdict.

**Deployment gate at 265 USD** (all required for any deployment proposal; evaluated on `W_HOLD`, labelled per S-14.1, and on the forward run with point-in-time filters, which is decisive):

| ID | Gate |
|---|---|
| D1 | ≥ 90 % of the decisions at which L-norm holds a non-empty book are constructible at 265 USD, legs and any required hedge included |
| D2 | L-265 mean daily net > 0, with rounding and minimum-notional effects |
| D3 | **End-to-end deployment economics:** annualized L-265 net USD − annual fixed infrastructure cost > 0 (currently VPS 12 USD/month = 144 USD/year) |

Fixed infrastructure cost never enters labels or L-norm results; it appears only in D3's separate deployment-economics report.

---

## 15. Forward protocol (frozen; DEC-10)

**Precondition.** The `W_HOLD` verdict is `V7 HOLDOUT ADVANCE`. Otherwise there is no forward test.

**Forward model.** A mechanical refit with **no re-selection**:
- the frozen ML\* hyperparameters, and for LightGBM the frozen tree count;
- `T_fit` = [D0, 2026-01-01); `T_cal` = [2026-01-01, 2026-07-01) (the last 6 months of `W_HOLD`); S-10.3 purge;
- the same calibrators as S-10.4;
- hashed and frozen before `T_F7`.

A0 and A1 run alongside as references.

**Start-time rule.** `T_F7` = the first 00:00 UTC ≥ 24 h after the independent review PASS of the forward recorder and replay code. The recorder is a separate Binance public-data service with its own directory; it never touches the Kraken collector.

**Mode: delayed-reveal replay** (strict default of S-1.3).
- From `T_F7` the recorder stores raw data with `recv_ts`: tickers / book top, a depth snapshot every 5 minutes, funding records, **last-price 1-minute and 15-minute bars** (required by S-5.2 and the S-4.5 proxy), mark / index / premium bars, the exchange filters (`exchangeInfo` stepSize / minQty / notional filters), snapshotted at least daily and on change, hashed (S-14.1 step 4), and raw Binance announcement snapshots under the S-1.7 write-only exception (catalog responses and the article bodies fetched through transport-level ID extraction only, EC-8; `recv_ts`, source URL, SHA-256; sealed, semantically unparsed and unreadable by research tooling until L7; `known_at` = `recv_ts` at the replay).
- Every recorded row has `known_at` = max(`recv_ts`, its natural `known_at` in §3). Nothing is backfilled, except funding under "Missing data".
- The recorder uses public endpoints only. No private trading account is needed (DEC-14).
- **No outcome is computed before L7.**
- At L7 the frozen code replays the recording deterministically.

**Duration.** 180 calendar days: `W_FWD7` = [`T_F7`, `T_F7` + 180 d). Terminal unwind before the end (S-1.5).

**Single look.** **L7 = max(`T_F7` + 180 d, R_V6) + 1 d.** Nothing about forward outcomes is computed or shown before L7. With `T_F7` inside M6, **L7 ≥ 2027-10-14** under the strict default.

**Evidence (DEC-42).**
- **Evaluable quarter:** a 15-minute quarter is evaluable when every primary replay input that quarter requires is present and fresh under its frozen rule: last-price 1-minute and 15-minute bars (R3, R5); mark / index / premium bars where required (R4, S-4.6, F5); settled funding records (R2, U7); the required symbol / universe metadata (R1); the announcement recorder's successful poll log for that quarter (R11 is "present" when the poll succeeded, whether or not a new announcement appeared); and any other required reference-price data. The exchange-filter snapshot is required only for the L-265 / L-10k deployment evaluation (D1–D3), not for L-norm evaluability.
- **Book-top, depth and ticker feeds** aren't used by the frozen primary strategy. They are diagnostic only and never make a quarter or day non-evaluable.
- **Evaluable day:** ≥ 90 % of its quarter-hours are evaluable.
- **INCONCLUSIVE** if evaluable days < 144 (80 % of 180).
- P4 scaled to 180 / 365: ≥ 30 active days, ≥ 50 closed legs, ≥ 10 clusters.

**Statistical rule.**
- H_F: μ_ML\* > 0 on the forward daily returns (L-norm, `E_T`).
- p_abs from S-13.1 (ℓ ∈ {14, 28}, max p, B = 10,000), α = 0.05.
- With a single look, no alpha spending is needed.
- ML\* − A0 is reported, not required.

**Forward terminal events (EC-11; forward only).** ML\* is the primary scientific book.
- If ML\* holds a position that reaches a B / C terminal event (S-5.4), the forward run is `INVALID RUN`.
- If only A0 reaches such an unresolved event, A0 becomes unavailable as a forward reference from that point, and the ML\* forward evaluation continues. The ML\* vs A0 forward comparison is then reported as unavailable / incomplete.
- This doesn't block a forward GO, because the GO criteria require absolute ML\* evidence; ML\* − A0 is report-only.
- This relaxation never applies to `W_HOLD`, where P1 requires the frozen ML\* vs A0 comparison and an actually-held B / C event in ML\* or A0 is `INVALID RUN` (S-13.4).

**GO criteria** (all required):
- H_F p < 0.05;
- P2;
- P3;
- P6;
- **P11 = R1 ∧ R2′ on the forward executed book;**
- the evidence rules.

A **real-money deployment proposal** additionally requires:
- D1–D3 on the forward run with point-in-time filters (S-14.1);
- the DEC-14 venue/account-eligibility and execution-host verification.

A scientific GO without these reads "GO — not deployable at 265 USD".
- P7 and P8 (P8-primary) are reported, not gates.

**Verdicts:**

| Verdict | When |
|---|---|
| `INVALID RUN` | integrity failure, K10, any outcome computed before L7, or an actually-held B / C terminal event in ML\* (S-5.4) |
| `NO-GO` | any GO criterion fails, or the replay hits the drawdown kill |
| `INCONCLUSIVE` | evidence not met |
| `GO` | all GO criteria hold |

**Missing data.**
- A decision at an 8 h grid point uses the recorded inputs known by `t` − 1 min. If its quarter isn't evaluable, the decision is made at the first evaluable quarter within 2 h after `t`; otherwise it is skipped: no entries, and exits and risk exits run at the first evaluable quarter.
- **Funding** missing in the recorder: fetched from the official Binance REST funding history after the period, hashed, never interpolated. If unavailable → `INVALID RUN`.
- **Prices** are never backfilled or interpolated. A stale quarter is a no-op for risk and decisions.

**Restarts and gaps.**
- Recorder restarts don't move the window.
- A recorder gap > 24 h in a required input makes those days non-evaluable.
- **One** 90-day extension is allowed only if the look is `INCONCLUSIVE` because of coverage alone (same rules, single look at the extended end). There is no other extension.

---

## 16. Kill criteria

| ID | Trigger | Consequence |
|---|---|---|
| K1 | Any L1–L14 or known-at failure (any model) | stop; fix; restart `W_DEV` selection from scratch; the holdout stays sealed |
| K2 | Stage 0: funding history, the point-in-time universe, or ≥ 95 % bar coverage not reconstructible; or **STAGE-0 FAIL / insufficient historical breadth** (S-10.2) | stop; explicit new-spec decision by the user (no silent K / D0 change) |
| K3 | S-10.7 development kill | NO-GO: no viable edge; the holdout stays sealed |
| K4 | LightGBM not chosen under S-10.6 | the linear model is used (not a kill) |
| K5 | Holdout verdict not ADVANCE | NO-GO; no re-tuning, no re-run, family closed for this spec |
| K6 | P1 holds only under `E_M` | NO-GO: maker-dependent |
| K7 | One feature carries > 50 % of total LightGBM gain, or > 50 % of the absolute standardized EN weight | mandatory leakage audit before the holdout; if unexplained → K1 |
| K8 | During the holdout run: invalid labels or missing funding > 0.5 %, or a Stage 0-type integrity failure | INVALID RUN |
| K9 | Any attempt to read or derive outcomes inside M6 before R_V6, to read `W_HOLD` before the opening, or to reach an endpoint the S-1.7 allowlist refuses | guard refusal; logged; incident report |
| K10 | \|Σ_c ρ_c + MKT − PR\| > 1e-9 × Σ\|fill notional\|, or PR ≠ the daily-table price column | INVALID RUN |
| K11 | An actually-held position in a required run reaches a B / C terminal event (S-5.4). Required runs: `W_DEV`, the six candidates and A0 at 24 / 72 / 168 h; `W_HOLD`, ML\* and A0; forward, ML\* only (EC-11) | `W_DEV`: stop before selection, `INVALID RUN — historical terminal execution not reconstructible`, holdout sealed (S-10.6); `W_HOLD` / forward: INVALID RUN. Forward A0 alone: A0 unavailable from that point, the ML\* − A0 report incomplete, no INVALID RUN (§15) |

---

## 17. Guards and reproducibility

- **Spec tag** `v7-ml-spec-frozen`.
- **Code tag** `v7-code-frozen`, with T_F7 per §15.
- **V6 boundaries** read from the canonical artefacts with hash checks (S-1.1).
- **Holdout opening (DEC-19):** token from an explicit approval phrase, exclusive markers, pinned manifest, read-only loaders.
- **Reproducibility:** fixed seeds; `uv.lock`; every run recorded in DT4; deterministic data ordering.

---

## 18. Stage 0: data-availability audit (outcome-free; before freeze)

**Allowed:** downloading and reading `W_DEV` raw data, plus `W_HOLD` file listings only.

**Forbidden:** any label, P&L, IC or model; anything inside M6; any Kraken data beyond the frozen copy.

| Item | Question |
|---|---|
| S0-1 | Which Binance USD-M public datasets exist, per symbol and date range. The official repository documents aggTrades / klines / trades; to verify: funding, mark / index / premium klines, bookTicker, bookDepth, metrics |
| S0-2 | REST `/fapi/v1/fundingRate` completeness for every reconstructed symbol including delisted ones: intervals, timestamp units, mark-at-settle |
| S0-3 | Symbol-history reconstruction: delisted count; listing / last-bar dates; `underlyingType` for delisted symbols |
| S0-4 | Delisting announcement source with verifiable timestamps (DEC-12); under DEC-36, notice content counts only from timestamped archived snapshots |
| S0-5 | Spread (R6) coverage in `W_DEV` and `W_HOLD` (file listing only); diagnostic only, the λ test is superseded by DEC-22 |
| S0-6 | F7 / F15 coverage |
| S0-7 | D0 per S-1.8 |
| S0-8 | Official fee evidence: Binance USD-M Regular User / VIP 0 per the DEC-39 evidence standard (S-4.3); Kraken from the V6 snapshot |
| S0-9 | Bar coverage, duplicates and gaps, time units |
| S0-10 | aggTrades availability and size for `E_M` (sampled days only) |
| S0-11 | `W_HOLD` listing completeness (names / sizes only) |
| S0-12 | Search for timestamped historical stepSize / minQty / `MIN_NOTIONAL` / `NOTIONAL` (including BTCUSDT) → S-14.1 branch 1 or 2; record current values (DEC-21) |
| S0-13 | Read-only verification of the V6 artefact hashes used by S-1.1 |

Output: `docs/results/v7-stage0-audit.md` plus manifests. Results 2026-10-14: D0 = 2021-01-01 (10 folds), F7/F15 dropped, U6 source active where matched, S-14.1 branch 2. S0-5's λ test is superseded by DEC-22. S0-8: maker 0.0200 % / taker 0.0500 % confirmed by official Binance Support evidence (DEC-39, `binance-fee-evidence.md`).

**S0-14 Pre-freeze input-availability audit (DEC-27):** `W_DEV` only, existence of the label inputs only, no values; re-run after DEC-35. Output: `docs/results/v7-stage0/rt5-input-availability.md`.

---

## 19. Out of scope until GO

- Real-money trading.
- **Venue / account eligibility and the live execution host (DEC-14):**
  - verified only after a scientific GO and before any real-money deployment proposal;
  - not required for the V7 public-data forward recorder / replay, which is paper research;
  - no VPN, residency assumption or KYC workaround is part of V7.
- The London VPS is for public data only.
- Changes to the frozen V5 bot or to V6.

---

## 20. Decision register

**Closed:**

| ID | Decision | Approved value |
|---|---|---|
| DEC-1 | V6 embargo | Strict: M6 = [V6 holdout start, R_V6), outcome-free recording only, no Binance exception; V7 forward verdict at L7 ≥ R_V6 (S-1.2/1.3, §15) |
| DEC-2 | D0 | G_D0 = U1–U4, ≥ 30 at every decision of the month (S-1.8) |
| DEC-3 | Calibration | `T_tr` / `T_sel` / `T_cal` three-block, no absorption (S-10.4) |
| DEC-4 | P2 | F_net > 0 and C_exec / F_net ≤ 0.60, hedge funding included (§8, P2) |
| DEC-5 | P11 | R1 (CAC > 0) ∧ R2′ executed-book attribution with the MKT unit (S-8.3, P11) |
| DEC-6 | Bootstrap | Centered circular block bootstrap, max over ℓ ∈ {14, 28}, IUT (S-13.1) |
| DEC-7 | Equity | L-norm primary; L-265 deployment gate D1–D3; L-10k diagnostic (§14) |
| DEC-8 | A1 veto | Calibrated P̂ < 0.50 (S-7.2) |
| DEC-9 | Search budget | 42 configurations (6 + 6 regression per horizon; classifiers fixed), no new families (S-10.5) |
| DEC-10 | Forward protocol | Frozen 180-day delayed-reveal, single look L7 (§15) |
| O-12 | Model families | EN + Logistic + LightGBM only (S-0.4) |
| (REV 2.1) | P8 split | P8-primary gate (regression ML\*); P8-classifier secondary, A1 claims only (S-13.2/13.3) |
| (Draft 2) | Merge clarifications | S-1.5 (no outcome reads beyond a window); S-8.4 (per-asset net attribution); the P7 one-sided lower bound (S-13.1); the frozen forward-model blocks (§15) |

**Closed by the PRE-STAGE-0 DECISION PATCH (2026-10-14):**

| ID | Was | Decision | Approved value |
|---|---|---|---|
| DEC-11 | O-1 | Spread source | **superseded by DEC-22** (was: real R6 where available, else the proxy with λ chosen by an understatement test) |
| DEC-12 | O-2 | Delisting announcements | used only if a verifiable timestamped source exists, matched point-in-time per S-2.5 (DEC-26); otherwise U6 inactive and the S-5.4 stressed terminal unwind |
| DEC-13 | O-4 | BNB discount | off in the baseline; sensitivity only (S-4.3) |
| DEC-14 | O-5 | Venue / account eligibility and execution host | not required for the public-data forward recorder / replay; verified after a scientific GO and before any real-money deployment proposal; no VPN, residency assumption or KYC workaround (§15, §19) |
| DEC-15 | O-7 | K per side | **K = 5, no breadth fallback**; no valid D0 or < 8 folds → STAGE-0 FAIL / insufficient historical breadth, then an explicit new-spec decision (S-7.1, S-10.2, K2) |
| DEC-16 | O-8 | β̂ | clip(0.7·β_OLS + 0.3·1.0, 0, 3), 30 d hourly returns, ≥ 500 joint observations (S-5.2) |
| DEC-17 | O-10 | Kraken comparison | 2025-10-06 → 2026-06-30, exploratory, computed at the legitimate `W_HOLD` opening; never changes the primary verdict (S-1.6) |
| DEC-18 | O-11 | Fee tier | Binance USD-M VIP 0; no tier shopping; values fixed by DEC-23 (S-4.3) |
| DEC-19 | O-13 | Holdout opening | only after code freeze + all leakage / canary checks passed + the S-10.7 development gate passed + pinned manifest / guards ready + the explicit user approval phrase; one opening only (S-1.7, §17) |
| DEC-20 | O-14 | TS reference baselines | EWMA funding forecast (half-life 72 h); AR(1) on 30 d of r₈; reference only, no tuning, no confirmatory test (S-7.2) |
| DEC-21 | O-16 | Exchange filters | the S-14.1 hierarchy: point-in-time historical filters if reliable; otherwise current filters only as a labelled approximation; historical D1–D3 then never "exact"; forward recorder snapshots filters; a deployment proposal needs forward point-in-time D1–D3 |
| (Pre-Stage-0) | — | Clarifications confirmed by the user | required historical breadth = at least 8 outer folds, else STAGE-0 FAIL / insufficient historical breadth with no change to K, D0 or any parameter (S-10.2); forward exchange-filter snapshots at least daily and on detected change, an operational recording cadence only (§15) |

**Closed by the PRE-FREEZE BLOCKER RESOLUTION PATCH (2026-10-14, after Stage 0 and the red team):**

| ID | Was | Decision | Approved value |
|---|---|---|---|
| DEC-22 | B-1 / RT-1 (supersedes DEC-11's branch) | Spread | proxy only in every window for features, U5, A0, labels and the executed book; R6 diagnostic only; **λ = 2.0 fixed**, never reopened. Estimator **amended by DEC-38** (was: per-pair truncation, then the median) (S-4.4, S-4.5) |
| DEC-23 | B-2 | Fees | Regular / VIP 0 maker 0.0200 %, taker 0.0500 %, BNB off. **Confirmed by DEC-39** (official Binance Support evidence) (S-4.3) |
| DEC-24 | B-3 | Contract instances | new instance after dormancy; U1/U3/features reset; nothing bridges dormancy; unreconstructible instances not synthesized. **Dormancy redefined by DEC-35** (was: ≥ 7 UTC days without archived bars) (S-2.2) |
| DEC-25 | RT-4 | Seal | PROTOCOL INCIDENT — NON-OUTCOME METADATA EXPOSURE recorded permanently; S-1.7 kept strict (no later-dated CMS metadata before opening); one pinned, hashed capture at the opening; collector-integrity monitoring separated from research outcomes (S-1.3, S-1.7) |
| DEC-26 | RT-2 / R-5 | Delisting notices | point-in-time S-2.5; no last-trade conditions. **Amended by DEC-35** (notice-independent halt), **DEC-36** (archived-snapshot `known_at`), **DEC-37** (scheduled time, 72 h clear only with a unique time) and **DEC-40** (confident match) |
| DEC-27 | RT-5 | Invalid-input check | option (a) only: outcome-free `W_DEV` input-availability audit before freeze, re-run after DEC-35; no post-freeze trigger (R2-5) (S-5.5, S0-14) |
| DEC-28 | R-1 (rejected change) | U4 | zero-volume exchange bars count as present (unchanged by DEC-35); trading activity via U8 and the S-5.4 halt |
| DEC-29 | R-2 | Funding intervals | gap test: spacing > max(previous, current interval) + 60 s; explicit interval field for r₈; mechanical inference if missing. Off-schedule records: **amended by DEC-40** (unreconciled → invalid) (S-2.3, S-2.6) |
| DEC-30 | R-3 | Unknown types | the 5 unknown-type symbols are excluded; never guessed (S-2.1) |
| DEC-31 | RT-6 | Identity `known_at` | L1 whitelist for verified-invariant identity fields at the instance's `first_bar_ts` (verified invariant = at least one timestamped source dated within the instance, no conflicting source); time-varying fields keep their timestamps; unknown identity → exclude (R1, L1) |
| DEC-32 | R-4 / M-3 | Funding source; limitation | archive primary, REST fills gaps only; conflicts → data-integrity blocker; instance-aware; unreconstructible old instances are a known limitation (S-2.2, S-2.6) |
| DEC-33 | M-7 | Klines | monthly ∪ daily by primary key; conflicting duplicates reported, never chosen silently (S-2.2, R3, R5) |
| DEC-34 | RT-3 | L-265 granularity | accepted as is (E, rounding, hedge rule, D1 unchanged) → "GO — not deployable at 265 USD" if infeasible; missing-filter fallback: timestamped historical capture or exclude and flag (S-14.1) |

**Closed by the FINAL PRE-FREEZE PATCH (R2-1 … R2-9, 2026-10-14, after red team round 2):**

| ID | Was | Decision | Approved value |
|---|---|---|---|
| DEC-35 | R2-1 (C2-1) | Dead / dormant contracts | U4 stays a data-coverage gate (DEC-28). New gate **U8**: a volume > 0 15-minute bar closing in [`t` − 60 min, `t` − 1 min], else no entry or increase. **Instances by activity:** ≥ 7 consecutive UTC calendar days with no volume > 0 15-minute bar end the instance; the first later volume > 0 bar starts a new one; `first_bar_ts`, U1, U3 and all rolling funding / features reset; dormant-interval bars and funding never bridge. **Halt:** 60 min without a volume > 0 1-minute bar → HALTED; no orders or resizes; no retroactive fill; an exit waits for the first executable volume > 0 bar of the same instance (taker, stressed costs) or an official settlement price recorded under the frozen data rules; none by the terminal unwind → label invalid / `INVALID RUN` (S-2.2, S-2.3, S-5.4, S-7.3) |
| DEC-36 | R2-2 (M2-1) | Mutable notice bodies | historical content known only from the earliest timestamped archived snapshot containing it: `known_at` = max(`releaseDate`, capture timestamp); no snapshot → no historical U6, current CMS text never backdated; later revisions are new versions; forward snapshots `known_at` = `recv_ts` with hash, retrieval time and source (S-2.5, R11) |
| DEC-37 | R2-3 (M2-2) | Scheduled delisting times | `scheduled_delist_ts` only when one exact UTC timestamp maps deterministically to the exact instance, else null; matched notice → no new entries from `known_at`, open positions exit at the next normal decision; a later postponement / cancellation lifts U6 from its `known_at`; the 72 h auto-clear only with a unique `scheduled_delist_ts`; the `releaseDate` ≤ last-trade logic permanently removed (S-2.5, S-7.3) |
| DEC-38 | R2-4 (M2-3) | Spread estimator; P6 | the published Abdi–Ranaldo window-level estimator (mean of pair products, then truncation at 0 and the square root), ÷ 2, × λ = 2.0, then the 1 bp floor; same proxy in every window; R6 diagnostic only; λ not called conservative; hand-worked, reference-implementation and synthetic tests. **P6 revised:** fees × 1.5, slippage × 1.5, spread × 3.0, stressed mean daily net ≥ 0. A pre-outcome robustness decision based only on synthetic measurement-error analysis; never relaxed after results (S-4.5, P6) |
| DEC-39 | R2-8 (M2-5) | Official fee evidence | **Replaced by explicit user decision (2026-10-14); CLOSED.** Frozen: maker 0.0200 %, taker 0.0500 %, BNB discount OFF, Regular User / VIP 0, no tier shopping. Qualified official-source retrieval evidence is sufficient; a locally preserved raw HTML/PDF artefact is not required (the raw-artefact requirement is withdrawn because AWS WAF / regional redirects block raw preservation). Qualified = binance.com; an official Support / fee page (no Square, community, blog, Reddit or third-party content); Regular User USDⓈ-M maker / taker stated explicitly; two official locale URLs of the same article (or one article plus one fee page); URLs, UTC date, method, excerpt and limitation recorded. Evidence: support article 360033544231 (en-AU, en-PH), "How to calculate trading fee of USDS-Margined contracts?": maker 0.02 %, taker 0.05 %; not independently rendered in the V7 environment (regional redirect / AWS WAF), closure by this governance decision; provenance record `binance-fee-evidence.md` sha256 `63a9fa95…`. Evidence format only: constants, execution and economics unchanged (S-4.3, R9, S0-8) |
| DEC-40 | R2-9 | Ratified implementation semantics | fill-time proxy recomputed point-in-time at the fill with the 24 h rule (bars known by fill − 1 min); confident match = exact contract-symbol token in the archived title / body and an unambiguous USDⓈ-M futures context, no fuzzy inference; unreconciled funding interval (±60 s) → record invalid and reported, never normalized; missing proxy: no entry / increase / resize, exits use 25 bps half-spread + 3 × slip (reported), missing reference price → existing invalid / gap policy (S-2.5, S-2.6, S-4.4) |
| DEC-41 | R2-7 (M2-6) | Seal / network guards | pre-opening endpoint allowlist enforced in code: mutable CMS blocked, no download-then-filter workaround; Wayback only < 2025-07-01 (server-side bound); current `exchangeInfo` for schema / filters (and current identity) only, never its status / listing fields for history; `W_HOLD` filter captures deferred to the opening; forward filters from `T_F7` with `recv_ts` + hash; refusal tests per endpoint class; incident file immutable, included unchanged in the freeze record (S-1.7, L10, S-14.1) |
| DEC-42 | R2-6 (M2-7) | Forward evaluability | evaluable quarter = every primary replay input present / fresh under its frozen rule; book-top / depth / ticker diagnostic only; ≥ 90 % quarters per evaluable day; 144 / 180 days (§15) |
| (R2-5) | M2-4 | S-5.5 post-freeze sentence | removed; RT-5 option (a) only; the completed `W_DEV` audit is the pre-freeze check |

**Closed by the ROUND-3 PATCH (M3-1 … M3-5, E6 / E8 / E10 / E11, 2026-10-14, after red team round 3):**

| ID | Was | Decision | Approved value |
|---|---|---|---|
| DEC-43 | M3-1, E11 | Terminal settlement | Binance automatic settlement (1-second index average over the final 30 min; taker fee). A events: settle at the official time and the exact official price, taker fee, no spread / slippage. B / C events: no invented fill, no later instance, no stale trade, no approximation; label invalid; an actually-held B / C event → that run `INVALID RUN — terminal settlement not reconstructible`; `W_DEV` selection needs all six candidates and A0 valid, else stop before selection (`INVALID RUN — historical terminal execution not reconstructible`, holdout sealed); same rule in `W_HOLD` / forward; R12 only for exact official sources. `W_DEV` audit: 0 A / 14 B / 12 C (S-5.4, S-10.6, S-13.4, K11, R12) |
| DEC-44 | M3-2 | Instance-end `known_at` | `instance_end_known_at` = close_time(last volume > 0 15-minute bar) + 7 × 24 h, canonical (EC-1; no later midnight boundary); not used before then; U8 stays point-in-time and never asserts a permanent end (S-2.2, R1, L2) |
| DEC-45 | M3-3 | Forward CMS recorder during M6 | write-only raw recording from `T_F7` (`recv_ts`, URL, SHA-256, sealed store); only transport-level article ID / code / URL extraction to fetch the raw body (EC-8); no semantic parsing (classification, symbols, delisting times) / derived table / display / search / metric before L7; research tooling can't read it; not a permission for research code (S-1.7, §15) |
| DEC-46 | M3-4, E6, E8 | Exact endpoint guards | exact endpoint + parameter allowlist N1–N6; only `fundingRate` among REST market data; no endpoint assumed bounded by `endTime`; Wayback only exact bounded CDX pairs, `id_`, redirects off, `Memento-Datetime` check; refusal tests. E6: research CMS blocked while M6 is sealed, even after the `W_HOLD` opening; `W_HOLD` notices only from captures < 2026-07-01. E8: current `exchangeInfo` only for whitelisted, verified-invariant identity / schema fields; never status, listing / delisting, filters or limits as history (S-1.7, L10) |
| DEC-47 | M3-5 | Funding intervals | DEC-29 restored exactly: explicit field controls; missing interval inferred mechanically from adjacent spacing / local regime; adjacent-interval gap test with ±60 s; one anomaly never poisons later records; regime-transition records reconciled by DEC-29 stay valid with `interval_transition = true` (EC-9); genuinely unreconciled / off-schedule records individually invalid and flagged (S-2.6, R2) |
| (E10) | E10 | Cross-instance fills | a pending exit is never filled, settled or priced from a later instance; instances are economically separate instruments (S-5.4) |

**Closed by the FINAL EDITORIAL CLARIFICATIONS (2026-10-14, after red team round 4: 0 Critical / 0 Major).** These are editorial; they introduce no new economic or model choice.

| ID | Subject | Approved clarification |
|---|---|---|
| EC-1 | Instance-end `known_at` | `instance_end_known_at` = close_time(last volume > 0 15-minute bar) + 7 × 24 h, canonical; no later midnight boundary; U8 independent (S-2.2, DEC-44) |
| EC-2 | A0 validity on `W_DEV` | A0 valid at 24, 72 and 168 h before h\* selection; else the selection run is INVALID before selection (S-10.6) |
| EC-6 | E8 vs DEC-21 | current filters never backdated or historical truth; DEC-21 current filters only in the labelled L-265 / L-10k approximation layer, never PIT metadata, never P1–P11 or L-norm; exact deployment evidence needs forward PIT filters (S-1.7, S-14.1) |
| EC-7 | R12 | optional, valid with zero rows; populated only for A events; empty by design for `W_DEV`; no placeholder / approximate / synthetic rows (R12, S-5.4) |
| EC-8 | Forward recorder | minimal transport-level article ID / code / URL extraction to fetch the raw body; both raw responses stored with `recv_ts`, URL, SHA-256; no semantic parsing, classification, symbol / time extraction, display or derived table before the reveal (S-1.7, §15, DEC-45) |
| EC-9 | Funding flags | DEC-29-reconciled regime transitions valid with `interval_transition = true`; genuinely unreconciled / off-schedule records individually invalid; no propagation (S-2.6, R2, DEC-47) |
| EC-11 | Forward A0 | forward only: an ML\* B / C event → INVALID RUN; an A0-only event → A0 unavailable from then, ML\* − A0 reported unavailable / incomplete, forward GO not blocked; not applicable to `W_HOLD` (§15, S-5.4, K11) |

**P6 patch history** (only the revised definition is normative):

| Version | Definition |
|---|---|
| Original (Draft 2 through the PRE-FREEZE BLOCKER RESOLUTION PATCH) | fees, spread and slippage all × 1.5 → mean daily net ≥ 0 |
| **Revised (DEC-38, normative)** | fees × 1.5, slippage × 1.5, spread × 3.0 on the same executed book → stressed mean daily net ≥ 0 |

**Open decisions: none.** No economic, model or statistical parameter remains free.

**Open operational items: none.** The DEC-39 fee evidence was closed on 2026-10-14 (S-4.3).

**Stage 0 branches: resolved (2026-10-14)** (`docs/results/v7-stage0-audit.md`):

| Branch | Stage 0 item | Result |
|---|---|---|
| Spread multiplier λ | S0-5 | superseded: λ = 2.0 by decision (DEC-22) |
| U6 delisting gate | S0-4 | active where a matched notice version has a timestamped archived snapshot (S-2.5, DEC-36); inactive otherwise |
| Fee constants | S0-8 | 0.0200 % / 0.0500 %, BNB off; official evidence recorded (DEC-39, `binance-fee-evidence.md`) |
| Exchange filters | S0-12 | S-14.1 branch 2 (labelled approximation) |
| D0 / folds | S0-7 | D0 = 2021-01-01, 10 folds (2023Q1–2025Q2) |
| F7 / F15 | S0-6 | both dropped for every model |

The numeric constants embedded in §2, §4, §6, §7, §10, §13, §14 and §15 are fixed by this draft and freeze with the spec.

---

## Appendix A — Notation

| Symbol | Meaning |
|---|---|
| s | side (+1 long, −1 short) |
| h | horizon |
| `t` | decision time |
| `t_e` | entry fill time (`t` + 15 min) |
| `t_x` | exit fill time (`t_e` + h) |
| P, M, B | last-price reference, mark, BTC reference |
| r₈ | funding rate per 8 h |
| f | fee rate |
| ĥ | decision-time half-spread estimate |
| `h_a`, `h_B` | realized half-spreads at fill |
| slip | slippage per side |
| β̂ | point-in-time BTC beta |
| E | equity |
| K | legs per side |
| ML\* | the selected ML arm |
| h\* | the selected horizon |
| F_net, C_exec, PR, NET, CAC, ρ_c, MKT | §8 |
| M6, R_V6 | S-1.2 (V6-masked span and its release time; `R6` names only the `book_top` table) |
| U8, HALTED | S-2.3, S-5.4 (trading activity; notice-independent halt) |
| Ŝ(τ), ĥ(τ) | S-4.5 (window full spread; half-spread proxy) |
| L7 | §15 |
