# Incident 2026-10-10/11: two host restarts, forward collector gaps

Operational record only. It was investigated from Windows event logs and the collector's own hour
files. No forward data was backfilled, interpolated or fabricated. No research outcome was looked
at.

## Timeline (UTC)

| Time | Event |
|---|---|
| 2026-10-10 23:55:00 | Last collector row (fut_tickers) before the gap |
| 2026-10-10 23:55:11 | User-initiated restart from the Start menu (System 1074, StartMenuExperienceHost) |
| 2026-10-10 23:55:30 / 23:55:46 | OS shut down and started again |
| 2026-10-10 23:56:05 | User logon (Winlogon 7001) |
| 23:57–00:03 | Kernel live dumps reported by Windows Error Reporting (LiveKernelEvent a1000001, 193, 19c, 9f) |
| 2026-10-11 04:18:00 | First collector row after the gap (fut_tickers hour 04) |
| 2026-10-11 20:44:36 | Second user-initiated restart from the Start menu (System 1074) |
| 2026-10-11 20:44:00 → 20:47:00 | Collector gap of ~3 minutes; container started 20:46:49 |

## Root cause

Both gaps start at a user-initiated Windows restart. The collector cannot fetch public snapshots
retroactively.
- **Second restart:** Docker Desktop and the containers came back within ~2.5 minutes.
- **First restart:** The containers did not write data for ~4 h 22 min after logon. The exact
  reason for the delay is **not confirmed**. Docker Desktop logs for that time have rotated. The
  kernel live dumps just after boot (0x9f is a driver power-state failure) point to the host
  coming back degraded, with Docker/WSL starting late.

The earlier gap (2026-10-10 ~02:36–10:39) is a separate incident: host sleep, see
`2026-10-10-host-sleep.md` on the main branch.

## Data impact

- **Lost, unrecoverable:** fut_tickers, fut_book, spot_ticker and spot_depth for
  2026-10-10 23:55 → 2026-10-11 04:18 and 2026-10-11 20:44 → 20:47.
- **Funding is recovered:** the hourly rounds carry 48 h of history.
- **No effect on research evidence:** these gaps lie before the V6 freeze, so they are outside V6
  forward evidence. They also lie before the V4 evidence start (2026-11-10). The sealed holdout
  was not involved.

## Follow-ups

- **Avoid manual restarts while the collector runs.** If a restart is needed, check afterwards
  that `docker ps` shows the collector healthy and that fresh hour files are being written.
- **Coverage rule:** The V6 exact spec has a fixed forward coverage rule. A missing snapshot is
  never filled.
