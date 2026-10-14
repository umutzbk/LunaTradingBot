# Morning report: 2026-10-14 (overnight A–E)

Written at 10:30 UK, late. **The PC rebooted itself at 05:16 BST.** The cron ticks and the PC collector were down until about 10:20 BST; see "Collector".

No commit, tag or spec freeze was made. No model, label, P&L, IC or backtest was produced. No V5/V6 change, no cutover, no PC bot-carry, no live trading.

## V7 Stage 0

**Status: NOT COMPLETE (2 factual branches blocked; no STAGE-0 FAIL condition hit).**
- **Report:** `docs/results/v7-stage0-audit.md`, including errata added after the red team.
- **Window and sources:** run 2026-10-14 02:31–04:40 UTC.
  - Binance public archive: listings for all 16 datasets; `W_DEV` files downloaded and checksum-verified.
  - `fapi` REST funding with `endTime` < 2025-07-01, and current `exchangeInfo`.
  - 20 `W_DEV`-dated Wayback captures of `exchangeInfo`.
  - The Binance CMS Delisting catalog, `W_DEV`-dated entries only after the purge.
  - `W_HOLD`: listings only.

| Branch | Result |
|---|---|
| D0 | **2021-01-01.** Tightest decision 31 vs ≥ 30; the same under 4 interpretation variants, confirmed independently by the red team |
| Outer folds | **10 (2023Q1–2025Q2).** Still 9 even if D0 were 2021-02 |
| F7 depth | **DROPPED** (bookDepth from 2023-01; coverage 60 % of candidates, 44 % of decisions) |
| F15 OI | **DROPPED** (81.8 % / 62.8 %, an upper bound) |
| Spread λ | **UNRESOLVED (B-1 / RT-1).** R6 exists only 2023-05 → 2024-04 (1.06 TB, 23 % of candidates); none in `W_HOLD`; matching undefined. No rate was computed |
| Delisting source / U6 | **ACTIVE where sourced.** Official catalog from 2022-02-17; 32 of 63 later delistings matched naively; U6 has no rule for postponements (RT-2) |
| Fee constants | **UNVERIFIED (B-2).** The official page is geo-blocked from the UK; provisional 0.020 % / 0.050 % retained |
| Historical exchange filters | **S-14.1 branch 2** (current filters as a labelled approximation). Wayback shows BTC `MIN_NOTIONAL` went 5 → 100 → 50 USD |
| S0-13 V6 hashes | PASS |

**Data integrity:**
- **Klines:** 12,312 files, 0 checksum failures, 0 duplicates, all ms. Headers are mixed. Some monthly files are truncated where the daily files exist (M-7).
- **Funding:** REST ∪ archive, 0 mismatches over 1.36 M records. REST is empty for LITUSDT. 40 apparent gaps are interval-switch artefacts.
- **Symbol history:** 70 delistings; zero-volume tails continue for years after delisting.
- **Survivorship:** 12 symbol names reused after dormancy (B-3, M-3). 5 symbols have unknown `underlyingType`.
- **Seal:** a seal incident (M-1) was found by the red team and contained. Titles dated in `W_HOLD` and M6 were purged and the report corrected.

## Red team

`docs/results/v7-stage0/v7-prefreeze-redteam.md`. Recommendation: **NOT READY FOR SPEC FREEZE — BLOCKERS.**

- **Critical (1):**
  - **C-1:** the spread policy can't be implemented as written: three different spread sources, an unclear half-spread definition, floor/λ order and point-in-time window.
- **Major (7):**
  - **M-1:** seal incident.
  - **M-2:** U6 has no rule for postponed or cancelled delistings.
  - **M-3:** survivorship / name reuse.
  - **M-4:** metadata `known_at` vs L1.
  - **M-5:** L-265 BTC hedge granularity makes D1 near-certain to fail.
  - **M-6:** S-5.5's "reopen before freeze" can't be evaluated.
  - **M-7:** truncated monthly files.
- **Minor (17):** listed in the report.
- **Errors found in my Stage 0 audit:** corrected in the report's errata.

**Blockers needing your decision** (`docs/results/v7-stage0/BLOCKERS_FOR_USER.md`):
- **B-1 / RT-1:** spread rule and λ.
- **B-2:** save the official fee page from Turkey.
- **B-3:** relist rule.
- **RT-2 to RT-6:** postponements, L-265 hedge, seal policy, S-5.5, metadata `known_at`.
- **R-1 to R-5:** small data rules to confirm.

## Collector

`LunaTradingBot/docs/results/pc-vps-collector-continuity-2026-10-14.md`, with a 10:30 addendum.

- **Window:** 2026-10-14T00:45Z → 09:15Z (8.5 h).
- **fut_tickers coverage:** VPS **100 %** (510/510); PC **41 %** (210/510).
- **PC-only gaps:** 1 (VPS restart at 01:07Z). **VPS-only gaps:** the PC outage, 04:14 → 09:23Z, which is the longest gap (PC, 5 h 10 m).
- **Duplicates:** 0.
- **Funding:** 1,071 common events, **0 mismatches**, 105 VPS-only (the PC outage).
- **Clock:** PC about 58–68 ms ahead of the VPS; the VPS is NTP-synced.
- **Errors and restarts:**
  - PC: 2 system reboots (05:16 / 05:18 BST, not user-initiated). Docker Desktop didn't auto-start.
  - The PC collector, bot-perp, bot-spot and dashboard were restored at 09:22Z by starting Docker Desktop. **bot-carry stayed off on the PC.**
  - VPS: 0 restarts.
- **Resources:** VPS 1 GB RAM available, 40 GB disk. PC C: **24 GB free** after the archive move (below).
- **Incident:** `LunaTradingBot/docs/ops/incidents/2026-10-14-pc-reboot-collector-gap.md`.

**VPS SHADOW VALIDATION: PASS WITH MINOR GAPS**
**COLLECTOR CUTOVER READINESS: NOT READY** (needs ≥ 7 days, day-close manifests and a working VPS backup).

## Disk (approved by you this morning)

**Done.** `var/raw/Kraken_OHLCVT_Full_2026Q2.zip.part00–04` (8.6 GB) was moved to the VPS at `/home/luna/archive/kraken_ohlcvt_2026Q2/` (plus the remote sums file).
- All 5 SHA-256 values on the VPS were checked against `PINNED_SHA256` in `luna/data/kraken_spot_archive.py`: **5/5 match**. Only then were the PC copies deleted.
- A note (`var/raw/KRAKEN_ARCHIVE_MOVED_TO_VPS.txt`) says where they are; copy them back before any spot backfill rerun. The archive isn't re-downloadable: Kraken replaced it.
- **C: free: 24 GB** (was 4.7 GB).
- Operations unaffected: the bots read `var/bars`; the gap-fill/backfill tasks have no triggers.

## Resource plan

`docs/results/v7-stage0/v7-resource-plan.md`.
- **Build and training:** on the PC, but only with about 30 GB free disk and about 4 GB free RAM.
- **Forward recorder:** on the VPS.
- **The 2 GB VPS is unsuitable for training.**

## Next actions for you

1. Decide B-1/RT-1, B-3 and RT-2 to RT-6; confirm R-1 to R-5.
2. Save the official Binance futures fee page from Turkey (B-2).
3. Set Docker Desktop to start at sign-in, and limit Windows auto-restart.

## V7 status

**NOT READY FOR SPEC FREEZE — BLOCKERS**
