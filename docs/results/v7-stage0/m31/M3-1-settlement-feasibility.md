# M3-1 settlement-reconstruction feasibility audit (`W_DEV`, outcome-free)

**Date:** 2026-10-14. The audit is scoped by the user's M3-1 decision.
- No settlement P&L, strategy return or label value was computed.
- No price value was read.
- 1-second settlement prices were not approximated with 1-minute or 15-minute bars.
- Nothing dated ≥ 2025-07-01 was used.

**Network access** followed the DEC-46 guard:
- Wayback CDX queries were server-side bounded (`to` ≤ 20250630235959).
- Captures were fetched only at exact (timestamp, URL) pairs returned by those queries, in `id_` form, with redirects disabled and a `Memento-Datetime` check. Any non-200 response, redirect or timestamp substitution was refused unparsed.
- The archive bucket listing was read only for dataset and interval folder names.

## Scripts and outputs

| File | Role |
|---|---|
| `scripts/m31_events.py` → `m31/events.json` (sha256 `8070b202…`) | The eligible terminations: activity-instance ends before 2025-07-01 that the RT-5 population (G_D0 ∧ instance U1 ∧ U8) could reach within 168 h |
| `scripts/m31_notices.py` → `m31/notices.json` (sha256 `c10d8752…`) | Notice discovery and verification. Raw captures are in `var/v7/stage0/m31/captures/<ts>_<sha256[:16]>.bin` |
| `scripts/m31_classify.py` → `m31/classification.json` (sha256 `fcfe4e81…`) | A / B / C classification; the scheduled time read from each capture is recorded with its evidence sentence |
| `m31/archive_dataset_and_index_intervals.txt` (sha256 `7b5ff7a1…`) | Bucket listing: Binance USD-M public datasets and index-kline intervals |

**Population: 26 eligible terminations.** These are a superset of the round-3 red team's 16 out-of-fold deaths. The two extra events, LUNA 2022-05-12 and BNX 2023-02-01, come from the DEC-44 point-in-time instance membership.

## Answers to the seven questions (all events)

- **(5) Exact official settlement price from an official historical source: none for any event.** The public archive (`data.binance.vision`, USD-M) has no settlement or delivery dataset. The datasets are aggTrades, bookDepth, bookTicker, fundingRate, index / mark / premium klines, klines, metrics and trades.
- **(6) Index observations at the official formula's granularity: not available for any event.** The formula needs the price index sampled every second over the final 30 minutes (1,800 observations). The finest archived index kline is **1 minute** (listing: 1m, 3m, 5m, 15m, 30m, 1h … 1mo; no 1s), and the official REST kline interval enum also starts at 1m.
- **Consequence:** no event can be class A.
- **(7) Provenance:** every capture used has its timestamp and SHA-256 in the table.

**A / B / C counts: A = 0, B = 14, C = 12.**

**Classification rules:**
- **B** requires an immutable archived notice that deterministically maps one scheduled settlement time to the exact contract, and that time must agree with the observed cessation: the last traded 15-minute bar closes at the scheduled time or within 15 minutes after it.
- **C** means no such notice was found under the guard, or the archived time conflicts with the observed cessation.

| # | Contract instance (start → last traded bar close) | Class | Archived notice (capture UTC, sha256 prefix) | Scheduled settlement (UTC) | `known_at` ≥ | Consistent with observed cessation | Exact `XXXUSDT` token (DEC-40) | U6 exit before cessation if matched | Settlement price source / 1-s index |
|---|---|---|---|---|---|---|---|---|---|
| 1 | BZRXUSDT #0 (2020-09-05T03:00 → 2021-12-19T02:00) | **C** | none found under the guard | — | — | — | — | — | none / not archived |
| 2 | BTTUSDT #0 (2021-04-06T07:00 → 2022-01-14T02:15) | **C** | none found under the guard | — | — | — | — | — | none / not archived |
| 3 | KEEPUSDT #0 (2021-06-22T03:30 → 2022-02-15T02:15) | **C** | none found under the guard | — | — | — | — | — | none / not archived |
| 4 | LUNAUSDT #0 (2021-01-28T07:00 → 2022-05-12T15:45) | **B** | `ef3ce76d…` (20220512150146, `30455c1b9ac0…`) | 2022-05-12 15:30 | 2022-05-12 15:01 | yes | no | no | none / not archived |
| 5 | ANCUSDT #0 (2022-03-08T03:30 → 2022-05-13T04:15) | **B** | `0bfb6cfd…` (20220513023034, `2e71e914a448…`) | 2022-05-13 04:00 | 2022-05-13 02:30 | yes | no | no | none / not archived |
| 6 | AKROUSDT #0 (2021-01-19T07:00 → 2022-05-27T07:00) | **C** | `d1cb9596…` (20220518143101, `8185b61235d3…`) | 2022-05-26 09:00 | 2022-05-18 14:31 | **no** | no | yes | none / not archived |
| 7 | DODOUSDT #0 (2021-02-19T13:00 → 2022-05-27T09:00) | **B** | `d1cb9596…` (20220518143101, `8185b61235d3…`) | 2022-05-27 09:00 | 2022-05-18 14:31 | yes | no | yes | none / not archived |
| 8 | TLMUSDT #0 (2021-07-16T03:30 → 2022-06-09T09:00) | **B** | `af469aee…` (20220602113539, `7291c1503658…`) | 2022-06-09 09:00 | 2022-06-02 11:35 | yes | no | yes | none / not archived |
| 9 | ICPUSDT #0 (2021-05-11T03:00 → 2022-06-10T09:15) | **B** | `af469aee…` (20220602113539, `7291c1503658…`) | 2022-06-10 09:00 | 2022-06-02 11:35 | yes | no | yes | none / not archived |
| 10 | SRMUSDT #0 (2020-09-05T02:00 → 2022-11-15T04:45) | **B** | `9a667666…` (20221114091646, `2cb9cb51ca5f…`) | 2022-11-15 04:30 | 2022-11-14 09:16 | yes | yes | yes | none / not archived |
| 11 | BNXUSDT #0 (2022-04-01T03:30 → 2023-02-01T00:00) | **C** | `4d23ada5…` (20230210043740, `56c78a7f0a4a…`) | 2023-02-11 04:00 | 2023-02-10 04:37 | **no** | no | no | none / not archived |
| 12 | COCOSUSDT #0 (2023-02-21T14:45 → 2023-05-25T09:15) | **B** | `45852dc1…` (20230518081658, `884b279764a3…`) | 2023-05-25 09:00 | 2023-05-18 08:16 | yes | no | yes | none / not archived |
| 13 | TOMOUSDT #0 (2020-10-12T07:00 → 2023-11-14T09:00) | **C** | none found under the guard | — | — | — | — | — | none / not archived |
| 14 | STRAXUSDT #0 (2023-10-11T02:30 → 2024-03-15T09:00) | **B** | `2e6eb8e6…` (20240308095620, `27b037c3e46f…`) | 2024-03-15 09:00 | 2024-03-08 09:56 | yes | yes | yes | none / not archived |
| 15 | WAVESUSDT #0 (2020-08-12T07:00 → 2024-06-11T09:00) | **B** | `f65faeee…` (20240603030208, `d6ac886af187…`) | 2024-06-11 09:00 | 2024-06-03 03:02 | yes | yes | yes | none / not archived |
| 16 | AGIXUSDT #0 (2023-02-16T14:15 → 2024-06-25T09:15) | **C** | none found under the guard | — | — | — | — | — | none / not archived |
| 17 | RNDRUSDT #0 (2023-02-03T02:30 → 2024-07-16T09:00) | **B** | `d1f2ae8d…` (20240710075935, `2c9e441fb2db…`) | 2024-07-16 09:00 | 2024-07-10 07:59 | yes | yes | yes | none / not archived |
| 18 | MATICUSDT #0 (2020-10-22T07:00 → 2024-09-04T09:00) | **B** | `6a6de383…` (20240828094940, `24327685ed26…`) | 2024-09-04 09:00 | 2024-08-28 09:49 | yes | yes | yes | none / not archived |
| 19 | UNFIUSDT #0 (2021-02-19T07:00 → 2024-10-30T09:15) | **B** | `043c8407…` (20241101134821, `762ae9dbf40f…`) | 2024-10-30 09:00 | 2024-11-01 13:48 | yes | yes | no | none / not archived |
| 20 | XEMUSDT #0 (2021-03-03T07:00 → 2024-12-09T09:15) | **B** | `cfe97d52…` (20250429054216, `e2e0611dbec3…`) | 2024-12-09 09:00 | 2025-04-29 05:42 | yes | yes | no | none / not archived |
| 21 | MAVIAUSDT #0 (2024-02-21T10:00 → 2024-12-16T09:15) | **B** | `27eac260…` (20241206174323, `902fdf32cd12…`) | 2024-12-16 09:00 | 2024-12-06 17:43 | yes | yes | yes | none / not archived |
| 22 | DARUSDT #0 (2022-04-29T03:30 → 2024-12-26T09:15) | **C** | none found under the guard | — | — | — | — | — | none / not archived |
| 23 | FTMUSDT #0 (2020-09-24T07:00 → 2025-01-06T09:15) | **C** | none found under the guard | — | — | — | — | — | none / not archived |
| 24 | BNXUSDT #1 (2023-02-22T14:45 → 2025-03-17T09:15) | **C** | none found under the guard | — | — | — | — | — | none / not archived |
| 25 | VIDTUSDT #0 (2024-08-23T09:30 → 2025-04-14T09:15) | **C** | none found under the guard | — | — | — | — | — | none / not archived |
| 26 | EOSUSDT #0 (2020-01-08T08:00 → 2025-05-21T09:00) | **C** | none found under the guard | — | — | — | — | — | none / not archived |
## Notes per class

**C because the time conflicts with observed trading (2 events):**
- **AKRO:** the archived notice (captured 2022-05-18 and again 2022-11-19, same text) says 2022-05-26 09:00. Yet 100 of the 104 15-minute bars up to 2022-05-27 07:45 have volume > 0.
- **BNX #0:** the swap notice says futures close all positions at 2023-02-11 04:00. But the archive's 15-minute bars stop at 2023-01-31 23:45. The termination observed in the data is an archive gap, not the official cessation.

**C because no notice was found (10 events): BZRX, BTT, KEEP, TOMO, AGIX, DAR, FTM, BNX #1, VIDT, EOS.**
- **2021 – early 2022:** announcement URLs had no slug. A notice outside the stored catalog-161 index can't be discovered without the mutable CMS, which is blocked.
- **2024 – 2025:** most Wayback captures of binance.com announcement pages are HTTP 202 bot-challenge pages (about 2 KB, no notice content). Slugless `/detail/<code>` URLs can't be searched by symbol.
- Only status-200 captures were used.

**B events are not protected by U6 under the strict DEC-40 token reading (8 of 14).**
- **Notice names only the base asset (6):** ANC, LUNA, DODO, TLM, ICP and COCOS name the contract as "`<BASE>` USDT-Margined Contract", with no `XXXUSDT` token. Under the current DEC-40 text ("exact contract symbol … as a whole token") they don't match.
- **Captured only after cessation (2):** UNFI and XEM.
- **Too late to exit even if matched:** LUNA (captured 29 min before settlement) and ANC (captured 1.5 h before) have no normal U6 exit in time.
- **Protected (6):** SRM, STRAX, WAVES, RNDR, MATIC and MAVIA. Each has a strict token match and a known_at early enough for the next-decision U6 exit to fill before cessation.

**Implication under the M3-1 rule.** An executed book that actually holds any of the **20 unprotected terminations** at cessation (12 C, plus 8 unprotected B) is `INVALID RUN — terminal settlement not reconstructible`. In `W_DEV` that stops V7 before selection (S-10.6), with the holdout sealed. Whether this happens depends on the books' holdings, which were not computed (outcome-free audit).

## Appendix: Wayback lag re-check (round-3 finding m3-8)

**The timezone bug is fixed** in `scripts/wayback_notice_lag.py`, which now uses `calendar.timegm` instead of `time.mktime − time.timezone`. The bug made every summer timestamp 1 h early.

**Re-run result** (`wayback_notice_lag.json`): 46 of 47 `W_DEV` futures notices have a status-200 capture before 2025-07-01. Median lag is 5.1 h; 38 are within 24 h, 40 within 72 h and 43 within 7 d. **No capture now precedes its notice's `releaseDate`**, so the 8 "pre-publication" captures were artefacts of the 1-hour error.

**Content check** of the 11 notices whose first capture is within 1 h of release, the set that had looked pre-publication (`scripts/wayback_prepub_check.py` → `wayback_prepub_check.json`; exact bounded CDX pair, `id_`, redirects off, `Memento-Datetime` check):
- **9 verified:** the captured page's title equals the stored `W_DEV` catalog title, and the article code is in the page.
- **1 same article, title since edited:** the captured title is "Binance Futures Will Delist SC USDT-Margined Contract", while the current CMS title is "Binance Futures Will Delist USDT-Margined SC Perpetual Contract". This is a concrete instance of the in-place edits DEC-36 guards against.
- **1 refused by the guard:** the exact capture request answered with a 302 redirect, so its content is unverified and unused. This is the TLMBUSD / AMBBUSD / LEVERBUSD notice, which concerns BUSD contracts that are out of the universe.
