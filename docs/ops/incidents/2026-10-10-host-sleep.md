# Incident 2026-10-10: host sleep, ~8 h outage of all Luna services

Operational record only. No V4 research artefact (spec, config, results, manifests, raw data) was
changed for or by this incident.

## Timeline (UTC)

| Time | Event |
|---|---|
| 02:36 | Laptop lid closed -> Windows Modern Standby. Last regular collector rows (fut_tickers, spot_ticker, fut_book) at 02:36. |
| 04:47 | Brief standby exit (one fut_tickers row at 04:47:14), then hibernate ("Standby Battery Budget Exceeded"). |
| 10:38 | Wake (lid opened). DNS not up yet: the collector's feed-dead rule (no ticker success >= 30 min) exits with code 3 at 10:38:13; Docker restarts it at 10:38:21; tickers ok from 10:39. Status: `skipped_slots = 480` for fut_tickers/spot_ticker. |
| 10:38-11:05 | Collector `unhealthy` only because fut_funding was stale (hourly round at :05). bot-carry evaluates every 60 s but freezes the book (`global_data_outage`: funding stale on >= half the universe); no orders. |
| 11:05 | Hourly fut_funding round (last 48 h per perp, 21/21 ok) covers every funding period of the outage. Collector healthy at 11:07:59. |
| 11:05-11:10 | bot-carry had read that round's file while it was still being written (first symbols only) and re-read it only at its next 5-minute refresh. |
| 11:10:05 | bot-carry back to normal: 17 eligible, coverage 719/720 on every asset, decision HOLD (flat), equity 265.00, 0 orders, 0 risk events. |

bot-perp, bot-spot and bot-carry made no evaluations between 02:36 and 10:38 (host asleep); none
restarted. No duplicate orders, fills, reconciliation or risk events.

## Data impact

- **Unrecoverable:** fut_tickers, spot_ticker, fut_book, spot_depth, spot_time between ~02:36 and
  ~10:39 (public snapshots cannot be fetched after the fact). Raw hour files for 03 and 05-09 do not
  exist; `luna forward-dq --since 2026-10-10T00:00 --until 2026-10-10T10:45` reports the gaps
  (fut_tickers 02:36-04:47 and 04:47-10:39; spot_ticker 02:36-10:39, 28 980 s). The 2026-10-10
  manifest (written when the day closes) lists only the files that exist.
- **Recovered:** funding. The 11:05 hourly round carries the last 48 h for every perp, so no funding
  period of the outage is missing.
- **V4:** the evidence period starts 2026-11-10T00:00Z (`V4_EVIDENCE_START_TS`); this gap lies
  before it, so it does not contaminate V4 evidence. The sealed holdout (2026-07-01 to 2026-10-09)
  is not involved and was not read.

## Follow-ups

- Fixed: bot-carry re-reads the hourly funding file as soon as it appears, grows or is replaced
  (stat-only check every evaluation) instead of waiting for the 5-minute refresh
  (`CollectorSource.funding_changed`).
- Host: keep the laptop on AC power with lid-close = "Do nothing" and sleep = "Never" while the
  collector runs; a gap after 2026-11-10 would fall inside the V4 evidence period.
