"""The forward-collector loop (spec §3): schedule, envelopes, maintenance, dead-feed exit.

Rounds are UTC-aligned (``k * period + offset``). Sources sharing a cadence run as one sequential
round in ``config.SOURCES`` order: the minute round is ``fut_tickers`` then ``spot_ticker`` with a
shared ``pair_seq``; the :02:30 five-minute round is ``spot_time``, ``fut_book``, ``spot_depth``
(spaced); funding at :05, analytics at :07; the daily reference sources at 00:10/00:12 and once at
start. A due minute round is run between the requests of a longer round, so it stays on time.
After every minute round (also inside long rounds): dead-feed check, status.json, lock heartbeat
(a lost lock stops the loop with exit 1). After every round: hour/day close and the backup, which
runs in one background worker thread (hourly, and when a day closes). A long round whose next slot
is due is abandoned (skipped calls are counted in status). If neither ticker source succeeded for
30 minutes, ``exit_code`` becomes 3 and the loop stops (Docker restarts the process).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx

from luna.forward import sources
from luna.forward.backup import run_backup
from luna.forward.config import AT_START, SOURCES
from luna.forward.envelope import NS, Request, fetch, iso_ns, ns_from_iso
from luna.forward.lock import CollectorLock, CollectorLocked
from luna.forward.meta import Meta
from luna.forward.status import StatusTracker
from luna.forward.writer import ClosedDayError, RawWriter

log = logging.getLogger("luna.forward")

TICKER_SOURCES = ("fut_tickers", "spot_ticker")
FEED_DEAD_AFTER_S = 30 * 60
FEED_DEAD_EXIT_CODE = 3
LOCK_LOST_EXIT_CODE = 1
MAX_SLEEP_S = 1.0  # keep the loop responsive to stop()
_NEVER = math.inf


SLOT_ARG_SOURCES = ("fut_analytics", "fut_funding")


def build_requests(source: str, slot_s: int) -> list[Request]:
    fn = getattr(sources, source)
    return fn(slot_s) if source in SLOT_ARG_SOURCES else fn()


@dataclass
class _Round:
    name: str
    sources: tuple[str, ...]
    period_s: int | None  # None: one-shot (the start fetch)
    offset_s: int
    priority: int
    next_ns: float = field(default=_NEVER)

    def first_slot_at_or_after(self, t_ns: int) -> int:
        p, o = self.period_s * NS, self.offset_s * NS
        return math.ceil((t_ns - o) / p) * p + o


def _rounds() -> list[_Round]:
    groups: dict[tuple[int, int], list[str]] = {}
    for name, cad in SOURCES.items():
        groups.setdefault((cad.period_s, cad.offset_s), []).append(name)
    out = [_Round("+".join(srcs), tuple(srcs), p, o, i) for i, ((p, o), srcs) in enumerate(groups.items())]
    out.append(_Round("start", AT_START, None, 0, 1))  # right after the first minute round
    return out


class CollectorStartError(RuntimeError):
    """The collector refuses to start (e.g. raw data exists but meta.json is missing)."""


class Collector:
    def __init__(
        self,
        root: Path,
        client: httpx.Client,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        sleep: Callable[[float], None] = time.sleep,
        backup_dir: Path | None = None,
        retries: int = 2,
        backoff: float = 0.5,
        lock_pid: int | None = None,
        lock_host: str | None = None,
        backup_join_s: float = 30.0,
    ) -> None:
        self.root = Path(root)
        self.client = client
        self.clock_ns = clock_ns
        self.sleep = sleep
        self.backup_dir = backup_dir
        self.retries = retries
        self.backoff = backoff
        self.backup_join_s = backup_join_s
        self.writer = RawWriter(self.root, sleep)
        self.lock = CollectorLock(self.root / "collector.lock", clock_ns, pid=lock_pid, host=lock_host)
        self.meta: Meta | None = None
        self.status: StatusTracker | None = None
        self.rounds = _rounds()
        self.exit_code = 0
        self.exit_reason = ""
        self._stop = False
        self._started = False
        self._in_minute = False
        self._last_ticker_ok_ns = 0
        self._backup_hour = -1
        self._backup_thread: threading.Thread | None = None
        self._backup_done: tuple | None = None  # (result, error, now_ns), set by the worker
        self._backup_wanted = False

    # ---- lifecycle
    def start(self) -> None:
        """Lock (raises CollectorLocked), meta/start ts, close leftovers, first backup + status.
        Raises CollectorStartError when raw data exists without meta.json: starting would reset
        collector_start_ts (restore meta.json, e.g. from the backup target, first)."""
        self.root.mkdir(parents=True, exist_ok=True)
        raw = self.root / "raw"
        if not (self.root / "meta.json").exists() and raw.is_dir() and any(raw.rglob("*.jsonl*")):
            raise CollectorStartError(
                f"{raw} has data but {self.root / 'meta.json'} is missing; refusing to start because "
                f"collector_start_ts would be reset. Restore meta.json (backup: <target>/forward/meta.json)."
            )
        self.lock.acquire()
        now = self.clock_ns()
        try:
            self.meta = Meta.load(self.root)  # a corrupt meta.json fails loudly, never resets
        except BaseException:
            self.lock.release()
            raise
        self._started = True
        self.meta.start(now)
        self.meta.save()
        self.status = StatusTracker(self.root, now)
        self._last_ticker_ok_ns = now
        for r in self.rounds:
            r.next_ns = now if r.period_s is None else r.first_slot_at_or_after(now)
        self._maintenance(now, force_backup=True)
        log.info("forward collector started (collector_start_ts %s)", self.meta.collector_start_ts)

    def stop(self) -> None:
        self._stop = True

    def close(self) -> None:
        if not self._started:
            return
        try:
            if self._backup_thread is not None:
                self._backup_thread.join(self.backup_join_s)
            self._publish_backup()
            self.meta.save()
            self._write_status(self.clock_ns())
        finally:
            self.lock.release()  # only removes the lock if it is still ours
            self._started = False

    def run_forever(self, until_ns: int | None = None) -> None:
        while not self._stop:
            now = self.clock_ns()
            if until_ns is not None and now >= until_ns:
                return
            for r in self.rounds:  # the clock stepped back: do not wait for a far future slot
                if r.period_s is not None and r.next_ns - now > r.period_s * NS:
                    r.next_ns = r.first_slot_at_or_after(now)
            r = min(self.rounds, key=lambda x: (x.next_ns, x.priority))
            if r.next_ns > now:
                wait = r.next_ns - now if until_ns is None else min(r.next_ns, until_ns) - now
                self.sleep(min(wait / NS, MAX_SLEEP_S))
                continue
            self._run_round(r)
            if not self._stop:
                self._maintenance(self.clock_ns())
            else:
                self._write_status(self.clock_ns())

    # ---- rounds
    def _reschedule(self, r: _Round, slot_ns: float) -> None:
        if r.period_s is None:
            r.next_ns = _NEVER
            return
        nxt = int(slot_ns) + r.period_s * NS
        now = self.clock_ns()
        r.next_ns = nxt if nxt >= now else r.first_slot_at_or_after(now)  # fell behind: skip

    def _run_round(self, r: _Round) -> None:
        slot = r.next_ns
        if r.sources == TICKER_SOURCES:
            self._run_minute()
        else:
            self._run_heavy(r, slot)
        self._reschedule(r, slot)

    def _run_heavy(self, r: _Round, slot: float) -> None:
        """Sequential, spaced requests. Stops at stop(); when the round's next slot is due, the
        remaining calls are abandoned and counted as skipped (gap info, no fake rows)."""
        slot_s = int(slot // NS)
        deadline = slot + r.period_s * NS if r.period_s is not None else _NEVER
        for k, src in enumerate(r.sources):
            spacing = SOURCES[src].spacing_s
            reqs = build_requests(src, slot_s)
            if r.period_s is None:  # the start fetch: flagged, excluded from coverage ratios
                reqs = [replace(q, extra={**(q.extra or {}), "at_start": True}) for q in reqs]
            first = self.meta.reserve(src, len(reqs))
            self.meta.save()
            for i, req in enumerate(reqs):
                if i and spacing:
                    self._pause(spacing)
                if self._stop:
                    return
                if self.clock_ns() >= deadline:
                    ts = iso_ns(self.clock_ns())
                    self.status.record_skipped(src, len(reqs) - i, ts)
                    for later in r.sources[k + 1:]:
                        self.status.record_skipped(later, SOURCES[later].calls, ts)
                    log.warning("round %s overran its next slot: %d %s calls skipped", r.name,
                                len(reqs) - i, src)
                    return
                self._fetch_write(req, first + i)
                self._minute_if_due()

    def _run_minute(self) -> None:
        self._in_minute = True
        try:
            pair_seq = self.meta.next_pair_seq()
            reqs = [req for src in TICKER_SOURCES for req in build_requests(src, 0)]
            seqs = [self.meta.next_seq(req.source) for req in reqs]
            self.meta.save()
            for req, seq in zip(reqs, seqs, strict=True):
                if self._stop:
                    break
                self._fetch_write(req, seq, pair_seq)
        finally:
            self._in_minute = False
        self._light_maintenance(self.clock_ns())

    def _pause(self, seconds: float) -> None:
        """Sleep ``seconds``, running a minute round that falls due meanwhile on time."""
        end = self.clock_ns() + round(seconds * NS)
        while not self._stop and (now := self.clock_ns()) < end:
            due = self.rounds[0].next_ns
            if due <= now and not self._in_minute:
                self._minute_if_due()
            else:
                self.sleep((min(end, due) - now) / NS if due > now else (end - now) / NS)

    def _minute_if_due(self) -> None:
        """Run a due minute round between the requests of a longer round."""
        if self._in_minute or self._stop:
            return
        minute = self.rounds[0]
        if minute.next_ns <= self.clock_ns():
            slot = minute.next_ns
            self._run_minute()
            self._reschedule(minute, slot)

    def _fetch_write(self, req: Request, seq: int, pair_seq: int | None = None) -> None:
        env = fetch(self.client, req, seq, pair_seq, self.clock_ns, self.retries, self.backoff, self.sleep)
        try:
            self.writer.append(env)
        except (ClosedDayError, OSError) as e:
            log.error("could not write %s row: %s", req.source, e)
            self.status.record_error(req.source, env["recv_ts"], f"write failed: {e}"[:500])
        else:
            self.status.record(env)
            if env["ok"] and req.source in TICKER_SOURCES:
                self._last_ticker_ok_ns = ns_from_iso(env["recv_ts"])
            elif not env["ok"]:
                log.warning("%s failed: %s", req.source, env["error"])
        self._check_dead(self.clock_ns())

    # ---- maintenance
    def _check_dead(self, now: int) -> None:
        if now - self._last_ticker_ok_ns >= FEED_DEAD_AFTER_S * NS and not self.exit_code:
            self.exit_code = FEED_DEAD_EXIT_CODE
            self.exit_reason = (f"no successful fut_tickers/spot_ticker call since "
                                f"{iso_ns(self._last_ticker_ok_ns)} (>= 30 min)")
            log.error("feed dead: %s; exiting with code %d", self.exit_reason, self.exit_code)
            self._stop = True

    def _light_maintenance(self, now: int) -> None:
        """After every minute round (also inside long rounds): dead feed, status, lock heartbeat."""
        self._check_dead(now)
        self._publish_backup()
        self._write_status(now)
        try:
            self.lock.heartbeat()
        except CollectorLocked as e:
            if not self.exit_code:
                self.exit_code, self.exit_reason = LOCK_LOST_EXIT_CODE, str(e)
                log.error("lock lost: %s; stopping", e)
            self._stop = True
        except OSError as e:
            log.error("lock heartbeat failed: %s", e)

    def _maintenance(self, now: int, force_backup: bool = False) -> None:
        closed: list[str] = []
        try:
            self.writer.close_hours(now)
            closed = self.writer.close_days(now, self.meta.collector_start_ns)
        except (OSError, ValueError) as e:
            log.error("hour/day close failed (retried next round): %s", e)
        try:
            self.status.unclosed_days = self.writer.overdue_unclosed_days(now)
        except OSError as e:
            log.error("scanning for unclosed days failed: %s", e)
        hour = now // (3600 * NS)
        if force_backup or closed or hour != self._backup_hour:
            self._backup_hour = hour
            self._backup_wanted = True
        self._publish_backup()
        if self._backup_wanted:
            self._start_backup(now)
        self._light_maintenance(now)

    # ---- backup worker (never blocks collection)
    def _backup_running(self) -> bool:
        return self._backup_thread is not None and self._backup_thread.is_alive()

    def _start_backup(self, now: int) -> None:
        if self._backup_running():
            return  # one worker at most; the wanted run starts when it finishes
        self._backup_wanted = False
        root, target = self.root, self.backup_dir

        def work() -> None:
            try:
                self._backup_done = (run_backup(root, target, now), None, now)
            except Exception as e:  # noqa: BLE001 - reported in status, never crashes collection
                self._backup_done = (None, e, now)

        self._backup_thread = threading.Thread(target=work, name="luna-forward-backup", daemon=True)
        self._backup_thread.start()

    def _publish_backup(self) -> None:
        done, self._backup_done = self._backup_done, None
        if done is not None:
            res, err, ts = done
            if err is not None:
                self.status.set_backup(False, f"backup error: {type(err).__name__}: {err}", [], ts)
            else:
                self.status.set_backup(res.ok, res.reason, res.warnings, ts)
                for w in ([res.reason] if res.reason else []) + res.warnings:
                    log.warning("backup: %s", w)
        self.status.backup_running = self._backup_running()

    def _write_status(self, now: int) -> None:
        try:
            self.status.write(now, self.meta.collector_start_ts, self.writer.open_hours(),
                              self.writer.closed_days())
        except OSError as e:
            log.error("status.json write failed: %s", e)
