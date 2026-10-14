# V7 Stage 0: data-availability audit (outcome-free)

- **Date:** 2026-10-14, 02:31–04:30 UTC.
- **Spec:** `docs/superpowers/specs/2026-10-14-luna-v7-ml-research-design.md` (Draft 2 plus the PRE-STAGE-0 DECISION PATCH; uncommitted, unfrozen).
- **Executed by:** Claude, overnight, user-authorized.

**Overall: STAGE 0 NOT COMPLETE. Two factual branches are blocked; no STAGE-0 FAIL condition was hit.**
- The frozen breadth rule passes: D0 = 2021-01, 10 outer folds.
- Funding, symbol history and bars are reconstructible.
- **λ (DEC-11)** can't be resolved as specified. See B-1 in `v7-stage0/BLOCKERS_FOR_USER.md`.
- **The official VIP 0 fee snapshot (DEC-18)** can't be retrieved from a UK IP without a workaround. See B-2.

## Errata after the independent red team (2026-10-14, same night)

See `v7-stage0/v7-prefreeze-redteam.md`. These corrections supersede the text below where they differ.
1. **M-1, seal:** see the M6 row in "Boundaries kept". The claim "nothing in M6 was read" was false for notice titles; those entries are now purged.
2. **M-7, truncated monthly kline files:** several "gaps" (2022-02-26..28 and 2022-04-01..02, about 49 symbols, including the common 288-bar gap) are monthly files cut short where the **daily** files exist. They are not exchange gaps. A merge rule (daily fills a truncated monthly) is needed: red-team RT item.
3. **M-3, relists:** BTCST is not a relist (dead since 2021-03; funding still published on zero volume). The old instances of BNT, BTT, OG and RIF have REST funding but **no archived bars**, so bar-based reconstruction misses them.
4. **Mislabelled fields in `bar_audit.json`:** `delisted_in_wdev` = 27 counts symbols whose *last archived bar* (with zero-volume tails) ends early, not delistings. The spec-rule count (last bar with volume > 0) is **70**, in `symbol_trading_history.csv`.
5. **`d0_eval.py` U3:** it passes when no funding record falls in the last 7 days, and uses REST only. The red team re-derived D0 under stricter readings with REST ∪ archive and got the same result (D0 = 2021-01, decision counts identical). The script must not be reused as an implementation.
6. **Delisting "32 matched"** includes one mismatch (BLZ) and postponement cases (OMG, MAVIA). U6 as specified can't express postponements (red team M-2).
7. **Reproducibility:** the F7/F15, R6 and delisting-coverage numbers came from inline commands. They have since been saved as `tmp/s0/coverage_scripts.py`.

## Boundaries kept

| Rule | How it was kept |
|---|---|
| No labels, P&L, IC, models, backtests, expectancy or Sharpe | Only availability, timestamps, counts, sizes and checksums were computed. Funding rates were compared only for equality between sources. Quote volume was used only for the U2 breadth gate. |
| `W_HOLD` (2025-07-01 → 2026-07-01): listings only | Archive listings (name, date, size) only. No `W_HOLD` file was downloaded. No REST call had a timestamp ≥ 2025-07-01 (the funding fetch used `endTime` = 2025-06-30T23:59:59.999Z, plus a filter on returned rows). The two Wayback exchangeInfo captures dated in `W_HOLD` (2026-02-17, 2026-05-10) were taken from the index only. Delisting notices dated in `W_HOLD` or later were used by title only. |
| M6 (≥ 2026-07-01) | Archive listing entries dated ≥ 2026-07-01 were dropped at parse time; only a boolean "has later files" was kept. **Correction (red team M-1):** the delisting-catalog index as first saved held the titles and timestamps of 142 notices dated in `W_HOLD` and 32 in M6 (latest 2026-10-09), and stored bodies held 1,330 related-article links. No market data, label or outcome was involved, but this broke the seal as written. At about 04:37 UK both were purged: the index now holds only the 266 `W_DEV`-dated entries, and the related-article lists were stripped. During the audit I saw counts by year and, in the first API probe, the titles of the 5 newest notices (2026-10-02..09, including "Binance Futures Will Delist Multiple USDⓈ-M Perpetual Contracts (2026-10-05)"). No V7 or V6 computation used them. |
| No V6 or Kraken reads beyond S0-13 | V6 artefacts were hashed via `git show ea78102:<file>`. No Kraken data was read. |
| λ | **No understatement rate was computed on any sample**, to avoid choosing a sample design after seeing a result (B-1). |

## Observation window and sources

| Source | Endpoint / location | Retrieved (UTC) | Scope |
|---|---|---|---|
| Binance public archive listing | `s3-ap-northeast-1.amazonaws.com/data.binance.vision` (S3 ListObjects) | 02:33–03:00 | 16 dataset/interval listings, names and sizes, truncated at 2026-07-01 |
| Archive files | `data.binance.vision/data/futures/um/monthly/{klines/15m, fundingRate}` | 02:39–03:10 | `W_DEV` only: 12,312 kline files and 12,312 funding files, each verified against its published `.CHECKSUM` |
| REST funding | `fapi.binance.com/fapi/v1/fundingRate` | 02:38–03:03 | 511 symbols, `endTime` < 2025-07-01 |
| Current exchangeInfo | `fapi.binance.com/fapi/v1/exchangeInfo` | 02:36:59 | sha256 `8a7ce454…6202602` |
| Historical exchangeInfo | Wayback CDX index + 20 `W_DEV`-dated captures | 02:45 | `var/v7/stage0/meta/wayback/index.json` |
| Delisting notices | `binance.com/bapi/composite/v1/public/cms/article/{list,detail}` (catalog 161 "Delisting") | 02:50–ongoing | index of 440 articles; bodies only for `W_DEV`-dated ones |
| Fee page | `binance.com/en/fee/futureFee` | 02:40 | **blocked:** HTTP 202 challenge to scripts; in a real browser it redirects to en-GB with the futures tab hidden for UK users |
| Sample files (schema/size only) | 8 fixed `W_DEV` daily files (aggTrades, bookDepth, metrics, bookTicker) | 03:00 | `samples/sample_schema.json` |

- **Data root:** `var/v7/stage0/` (gitignored), 1.23 GB.
- **Manifest:** `docs/results/v7-stage0/stage0-data-manifest.json`, 2,619 files with sha256.
- **Per-item JSON:** `docs/results/v7-stage0/`.

## Branches: SPEC RULE → OBSERVATION → MECHANICAL BRANCH → RESULT

### S0-7 D0 and outer folds
- **Spec rule:** S-1.8 picks the first month M ≥ 2021-01 where G_D0 = U1 ∧ U2 ∧ U3 ∧ U4 holds for at least 30 symbols at every 00:05/08:05/16:05 decision. S-10.2 then requires ≥ 8 folds, or STAGE-0 FAIL.
- **Observation:**
  - 2021-01: min 31, median 42, max 52 eligible symbols.
  - 2021-02: min 52.
  - 2021-03: min 73.
  - The tightest decisions are 2021-01-01 00:05 and 2021-01-06/07, all at 31.
  - The result is the same under 4 interpretation variants:
    - U2 with missing days counted as 0 (primary) or skipped;
    - U3 spacing ≤ interval + 60 s (primary) or ≤ 2 × interval;
    - the 5 symbols with unknown `underlyingType` included or excluded.
  - Universe: 511 symbols with `W_DEV` data. 7 are excluded by S-2.1: BTCUSDT, stablecoin bases, and the index contracts BTCDOMUSDT, DEFIUSDT, FOOTBALLUSDT and BLUEBIRDUSDT.
- **Branch:** a valid D0 exists, so fold count = quarters 2023Q1–2025Q2.
- **Result:** **D0 = 2021-01-01; 10 outer folds (2023Q1–2025Q2). PASS.**
  - The margin is 1 symbol at the tightest decision. If 2021-01 had failed, D0 = 2021-02 would still give 9 folds (≥ 8).

### S0-5 Spread (R6) availability and the DEC-11 λ branch
- **Spec rule:** S-4.5. λ = 1.5 if the understatement_rate over matched (venue, symbol, timestamp) `W_DEV` observations is ≤ 25 %, else λ = 2.0.
- **Observation:**
  - **R6 (USD-M bookTicker) exists only for 2023-05 → 2024-04** (monthly; daily 2023-05-16 → 2024-03-30). That is 270 symbols, 2,704 monthly files, **1.06 TB zipped**: a symbol-day median of 8 MB, and BTC about 139 MB per day.
  - Coverage: **23.3 %** of G_D0-eligible `W_DEV` symbol-decisions; 22.3 % of decisions have any R6.
  - **`W_HOLD`: 0 bookTicker files.**
  - File schema: `update_id, best_bid_price, best_bid_qty, best_ask_price, best_ask_qty, transaction_time, event_time` (ms).
- **Branch:** not determinable.
  1. The spec doesn't define the matched observation. Which R6 statistic is paired with a 15-minute bar's proxy: a snapshot at the bar close, the time-weighted mean over the bar, or the median? And is the comparison per bar or on the 24 h ĥ?
  2. The full computation means downloading and parsing about 1.06 TB of tick data (roughly 7 TB of CSV). That is infeasible on this PC (6.8 GB free) and was not attempted. Any subsample would be a new design choice.
- **Result:** **BLOCKED → B-1.** λ is unresolved. Facts that hold regardless: `W_HOLD` uses the proxy for every symbol-decision; `W_DEV` before 2023-05 and after 2024-04 uses the proxy.

### S0-4 Delisting announcements and the U6 branch
- **Spec rule:** DEC-12. Use an announcement only if a verifiable timestamped source exists; otherwise U6 is inactive.
- **Observation:**
  - The official Binance CMS catalog 161 has 440 articles from **2022-02-17**, each with an ms `releaseDate`.
  - 77 have a futures-related title; 47 are dated in `W_DEV`. Bodies were fetched and hashed for `W_DEV`-dated ones only; the full `W_DEV`-dated body fetch was still running at report time.
  - 70 symbols stopped trading in `W_DEV` (last bar with volume > 0 before 2025-06-24):
    - **7 before 2022-02-17:** 0 covered;
    - **63 after:** 32 matched to a futures notice before their last trade. Median lead 7.2 days; the maximum (859 days) is a mismatch.
  - Many of the 31 unmatched are token migrations or swaps (MATIC, FTM, RNDR, AGIX, OCEAN, KLAY, TOMO, GAL, EOS…) or notices outside the futures-title filter.
- **Branch:** a verifiable timestamped source exists → U6 **active, per symbol, from the notice's `releaseDate`**. Symbols with no verifiable notice (including every delisting before 2022-02-17) have U6 inactive, and the S-5.4 stressed terminal unwind applies.
- **Result:** **U6 ACTIVE where sourced** (coverage as above). The symbol-matching rule needs pre-registration before implementation (red team M-4).

### S0-8 Official Binance USD-M VIP 0 fee snapshot
- **Spec rule:** DEC-18. Snapshot the official schedule; if it differs from the provisional 0.020 % / 0.050 %, correct the constants before freeze.
- **Observation:**
  - Scripted requests get an HTTP 202 empty challenge.
  - In the browser the page redirects to `en-GB` with the USDⓈ-M futures tab hidden (derivatives aren't offered to UK retail users). Support articles redirect to the UK landing page too.
  - No VPN or other workaround was used (DEC-14 spirit).
  - Third-party pages (finder.com, tradersunion.com) state maker 0.02 % / taker 0.05 % for the regular tier, but they are **not** the official source and are not used.
- **Branch:** official snapshot not obtained → constants stay **provisional / unverified**.
- **Result:** **BLOCKED → B-2.** The user, in Turkey, can save the official page; it will be hashed.

### S0-12 Historical exchange filters and the S-14.1 branch
- **Spec rule:** S-14.1. Point-in-time historical filters if reliable (branch 1); otherwise current filters only as a labelled approximation (branch 2).
- **Observation:**
  - No official historical `exchangeInfo` exists.
  - The Wayback CDX index has 22 captures of `fapi/v1/exchangeInfo` up to 2026-06-30:
    - 2021: 10; 2022: 9; 2023: 1;
    - **none from 2023-11-02 to 2026-02**; 2 dated in `W_HOLD` (index only).
  - The 20 `W_DEV` captures were downloaded and parsed. BTCUSDT `MIN_NOTIONAL`: absent in 2021-01, **5** (2021-04 → 2022-11), **100** (2023-11), **50** (current). stepSize 0.001 throughout.
  - Current filters: `MIN_NOTIONAL` is 5 USD for 652 USDT perps, 20 for 5, and 50 for BTC only.
- **Branch:** the history is sparse, third-party and has a 20-month hole, so it is not reliable point-in-time → **branch 2**.
- **Result:** **S-14.1 BRANCH 2.**
  - Historical L-265 / L-10k use current filters, labelled "approximate (current filters)".
  - Historical D1–D3 are never exact deployability evidence.
  - The forward recorder must snapshot filters (§15).
  - The Wayback captures are kept only as a diagnostic: BTC's minimum notional varied 5 → 100 → 50 USD.

### S0-6 F7 depth and F15 open interest
- **Spec rule:** keep a feature only if coverage is ≥ 95 % of `W_DEV` decisions.
- **Observation** (file-level presence; an upper bound on true coverage):

  | Feature | Data range | Candidate-level coverage | All eligible covered at the decision |
  |---|---|---|---|
  | F7 (bookDepth) | 2023-01-01 → | 60.0 % | 43.6 % |
  | F15 (metrics) | 2020-09-01 →, 10,863 missing symbol-days in `W_DEV` | 81.8 % | 62.8 % |

  - Candidates = 514,252 G_D0-eligible symbol-decisions over 4,926 decisions (2021-01-01 → 2025-06-30).
- **Branch:** coverage < 95 % under every reading.
- **Result:** **F7 DROPPED; F15 DROPPED** for every model.

### S0-13 V6 artefact hashes (S-1.1)
- At `ea78102` (= `v6-code-frozen^{commit}`):
  - constants `152af4cb19e688b3…946974`;
  - freeze record `2f84499556480b6a…42d413`;
  - `verdict.py` `10c15f4e…`;
  - `forward.py` `53f79e41…`.
- **PASS:** these match S-1.1.

## Data audits

### S0-1 Binance USD-M datasets (USDT perpetual symbols; dates < 2026-07-01)

| Dataset | First | Last (pre-M6) | `W_DEV` files / GB | `W_HOLD` files / GB (listing only) | Holes (`W_DEV` / `W_HOLD`) |
|---|---|---|---|---|---|
| klines 15m (monthly) | 2020-01 | 2026-06 | 12,312 / 1.38 | 7,437 / 0.77 | 0 / 0 |
| klines 1m (monthly) | 2020-01 | 2026-06 | 12,312 / 17.31 | 7,437 / 9.40 | 0 / 0 |
| klines 1d (monthly) | 2020-01 | 2026-06 | 12,312 / 0.02 | 7,437 / 0.01 | 0 / 0 |
| markPriceKlines 15m | 2020-01 | 2026-06 | 12,539 / 0.77 | 7,477 / 0.41 | 0 / 0 |
| indexPriceKlines 15m | 2020-01 | 2026-06 | 12,592 / 0.85 | 6,987 / 0.41 | 0 / 6 months |
| premiumIndexKlines 15m | 2020-01 | 2026-06 | 12,264 / 0.66 | 6,698 / 0.32 | 29 / 5 months |
| fundingRate (monthly) | 2020-01 | 2026-06 | 12,312 / 0.01 | 6,642 / 0.01 | 28 / 0 months |
| bookTicker | 2023-05 | 2024-04 | 2,704 / 1,064.6 | **0** | 7 / – |
| bookDepth (daily) | 2023-01-01 | 2026-06-30 | 239,717 / 97.8 | 194,834 / 87.1 | 1,134 / 482 days |
| metrics (daily, OI) | 2020-09-01 | 2026-06-30 | 302,085 / 3.3 | 202,094 / 2.1 | 10,863 / 2,552 days |
| aggTrades (monthly) | 2020-01 | 2026-06 | 11,751 / 705.7 | 6,587 / 298.0 | 52 / 0 months |

- Daily klines, bookTicker and trades also exist. 0 zero-size files in any listing.
- Holes = months/days missing between a symbol's first and last file in that window.

### S0-2 Funding (REST vs archive, `W_DEV`)
- **Volume:** 511 symbols. REST has 1,365,757 records; the archive has 1,365,252.
- **Matching:**
  - every archive record is in REST;
  - 5,252 REST records aren't in the archive (the 28 missing archive months);
  - **0 rate mismatches** over 1,360,505 matched records;
  - 0 duplicates in either source.
- **REST gap:** **LITUSDT returns 0 REST records**, although the archive has 4,747. The archive is required for completeness.
- **Timestamps:** ms throughout; every `fundingTime` is within 1 s of the hour (jitter up to a few ms).
- **Interval distribution** (archive `funding_interval_hours`): 8 h 839,751; 4 h 510,127; 1 h 12,094; 2 h 3,280.
- **Unknown intervals:** 5,233 REST-only records have no archive interval and need inference from spacing (red team m-2).
- **Mark at settle:** REST `markPrice` exists only from **2023-10-31** (median symbol 2023-12-17). Before that, the S-4.6 fallback (mark 15-minute close) applies; mark klines exist from 2020-01.
- **Gaps (spacing > interval + 60 s):** 45.
  - **40 are interval-switch artefacts:** the archive labels the first record after a switch with the new interval while the spacing is the old one.
  - **5 are real:** BNTUSDT 20,016 h, OGUSDT 18,052 h, BTTUSDT 3,656 h, RIFUSDT 3,608 h, BTCSTUSDT 1,632 h. These are **symbol names reused after long dormancy** (red team M-3).

### S0-3 Symbol history
- **Count:** 511 USDT-perpetual names with `W_DEV` data (900 including later listings).
- **Delisted in `W_DEV`:** 70 by the spec rule (last archived bar with volume > 0).
- **Zero-volume tails:** the archive **keeps publishing zero-volume bars after delisting**, e.g. BTCSTUSDT to 2025-06. "Last archived bar" without the volume > 0 condition finds only 27.
- **Relist / suspension:** ≥ 7-day no-trade spans followed by resumption in klines for 7 symbols (AERGO, BNX, CTK, CVC, ICP, MAVIA, TLM), plus the 5 funding-gap names above.
- **`underlyingType`:** known for 506 of 511 symbols (current `exchangeInfo` includes 131 SETTLING contracts, plus the Wayback captures). **Unknown: AERGOUSDT, BDXNUSDT, DOTECOUSDT, LENDUSDT, MBLUSDT**; DOTECO is plausibly an index. D0 is invariant to them.
- **Known non-COIN:** BTCDOMUSDT, DEFIUSDT, FOOTBALLUSDT, BLUEBIRDUSDT.
- **Current `exchangeInfo`:** also lists `TRADIFI_PERPETUAL` equity, commodity and pre-market contracts. S-2.1 excludes them.

### S0-9 Bars (last-price 15-minute klines, `W_DEV`)
- **Files:** 12,312, all checksums OK.
- **Timestamps:** all ms; no µs files in `W_DEV`.
- **Headers:** **mixed.** 10,336 files have a header row and 1,976 don't, so ingest must detect it.
- **Integrity:** 0 duplicate `open_time`, 0 misaligned (`open_time` mod 15 min), 0 non-monotonic.
- **Gaps:** 35,115,108 bars with 31,314 missing (0.09 %). Per-symbol first-to-last coverage: minimum 97.76 %, p1 99.60 %, median 100 %.
- **Largest gaps** are relists/suspensions: TLM 2,834 bars, ICP 2,506, BNX 2,075; then a common 288-bar (3-day) gap across many symbols.
- **Zero-volume bars:** 1,729,161, mostly post-delisting tails. U4 counts a bar as present even when its volume is 0 (red team m-3).

### S0-10 aggTrades (for `E_M`, sampled days only)
- **Availability:** 2020-01 → 2026-06 (`W_HOLD` 724 symbols by listing).
- **Size per symbol-day:** median 1.0 MB zipped, p90 4.2, p99 17.2. BTC: median 17 MB, max 46.
- **Samples:** BTC 2021-03-15 is 38 MB zipped / 2.86 M rows; 2024-01-15 is 13.5 MB / 1.04 M rows.
- **Schema:** `agg_trade_id, price, quantity, first_trade_id, last_trade_id, transact_time (ms), is_buyer_maker`. The header is present in 2024 files and absent in 2021–2022 files.
- **Result:** feasible for sampled (symbol, day) pairs.

### S0-11 `W_HOLD` listing completeness (names and sizes only)
- **No month-level holes:** klines 15m, 1m and 1d (763 symbols); mark 15m; funding.
- **Month-level holes:** index 15m (6 symbol-months) and premium 15m (5).
- **Day-level holes:** bookDepth 482 symbol-days, metrics 2,552 (both features dropped anyway).
- **bookTicker:** none in `W_HOLD`.

## Facts handed to the red team / freeze review
- D0 = 2021-01-01, 10 folds.
- F7 and F15 dropped.
- U6 active where sourced.
- S-14.1 branch 2.
- λ **unresolved (B-1)**; fees **unverified (B-2)**.
- New data semantics not covered by the spec:
  - symbol-name reuse / relists (B-3);
  - zero-volume post-delisting bars;
  - funding-interval labelling at switches;
  - 5 unknown `underlyingType` symbols;
  - mixed CSV headers;
  - REST funding missing for LITUSDT.
