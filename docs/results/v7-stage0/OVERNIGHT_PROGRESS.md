# Overnight progress (2026-10-14, until 08:00 UK)
Cron f0ef9479 (17,47 3-7 * * * local). Scripts: C:\Users\umutzbk\.claude\jobs\34c13fa2\tmp\s0\ ; data: LunaTradingBot-v7\var\v7\stage0\ (gitignored)
Collector comparison: background subagent -> LunaTradingBot\docs\results\pc-vps-collector-continuity-2026-10-14.md

## Done
- §20 clarifications row added (8 folds; filter snapshot cadence)
- S0-13 V6 hashes PASS (constants 152af4cb…, freeze record 2f844995…) at ea78102
- S0-1 listings: monthly klines 15m/1d/1m?, funding, mark/index/premium 15m, bookTicker (2023-05..2024-04 only, 1.06 TB W_DEV, 0 W_HOLD)
- S0-8 BLOCKED: official fee page geo-restricted from UK (binance.com redirects en-GB, futures hidden). No workaround. -> BLOCKERS
- S0-12: Wayback has 22 archived fapi exchangeInfo (20 W_DEV-dated downloaded; 2 W_HOLD-dated index only). BTC MIN_NOTIONAL 5 (2021-22) -> 100 (2023-11) -> 50 (now). Sparse (none 2023-11..2025-06) -> branch 2
- S0-4: catalog 161 (Delisting) 440 articles from 2022-02-17; 77 futures-titled; 47 W_DEV bodies fetched
- current exchangeInfo sha 8a7ce454… (927 symbols; TRADIFI_PERPETUAL present; 131 SETTLING)

## Running
- funding REST (511 syms, W_DEV only) -> funding.log
- klines 15m W_DEV (511 syms, 12312 files) -> k15.log
- daily listings (bookDepth, metrics, bookTicker daily, klines daily) -> list.log

## Next
- D0 eval (needs klines+funding), bar audit summary, funding audit (REST vs archive, intervals), delisting coverage, F7/F15 coverage, aggTrades sizes (S0-10), W_HOLD listing completeness (S0-11)
- Do NOT compute λ understatement on any ad-hoc sample (forking path) -> blocker (matching unit undefined + 1 TB)

## Update 04:35 local
- A Stage 0: DONE except B-1 (λ) and B-2 (fees) blocked. Report docs/results/v7-stage0-audit.md; BLOCKERS_FOR_USER.md; resource plan written (D done).
- D0=2021-01 (min 31), 10 folds; F7/F15 dropped; U6 active where sourced; S-14.1 branch 2.
- C collector: subagent report done (2.5 h window, PASS WITH MINOR GAPS / NOT READY). TODO near 07:00: rerun comparison scripts in tmp/collector for an extended window and append addendum.
- B red team: independent subagent running -> v7-prefreeze-redteam.md
- delist bodies fetch running (bodies.log); then recompute delisting coverage
- E morning report: write at >= 07:40
- 04:25 local tick: red team + delist body fetch still running; both collectors healthy (0 errors today, status fresh). Next: delisting coverage recompute after bodies; collector rerun ~07:00; morning report >=07:40 local.
- 04:39 red team done (C1/M7/m17, NOT READY); M-1 purge done; report errata added; scripts archived; manifest refreshed. Remaining: collector rerun ~07:00, morning report >=07:40.
- 05:09 tick: C: free fell 6.8 -> 4.7 GB between 04:20 and 05:00 (cause not identified; none of my jobs were running; Docker docker_data.vhdx 41 GB was written at 05:03); drain is flat now (0 MB in 45 s). Above the 3 GB floor. Recheck each tick. Remaining: collector rerun ~07:00, morning report >=07:40.
- 10:19 USER-APPROVED: free disk by moving var/raw/Kraken_OHLCVT_Full_2026Q2.zip.part00-04 (8.6 GB) to VPS /home/luna/archive/kraken_ohlcvt_2026Q2/ (scp running; log $CLAUDE_JOB_DIR/tmp/kraken_move.log). NEXT TICK: when ALLDONE, sha256sum on VPS vs PINNED_SHA256 in luna/data/kraken_spot_archive.py; delete PC copies ONLY if all 5 match. Gapfill/backfill tasks have no triggers (ran once 10-09).
- 10:35 BST: PC had rebooted 05:16 (Windows); Docker restarted 09:22Z by Claude; collector addendum + incident written; Kraken parts moved to VPS (5/5 sha OK) and deleted on PC, C: 24 GB free; MORNING_REPORT written; cron deleted. OVERNIGHT DONE.
