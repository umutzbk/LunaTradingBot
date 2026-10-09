"""Append-only raw storage (spec §5).

``raw/<source>/date=YYYY-MM-DD/HH.jsonl`` holds the open hour (append + flush + fsync per line);
2 minutes after the UTC hour ends it becomes ``HH.jsonl.gz`` (tmp -> ``os.replace`` -> verify ->
delete ``.jsonl``). 3 hours after the UTC day ends (and once no hour of that day is open) the day
gets ``manifest/date=YYYY-MM-DD.json`` and its files become read-only; writing into it then fails.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import stat
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from luna.forward.config import SOURCES, expected_rows
from luna.forward.envelope import NS, iso_ns, ns_from_iso

log = logging.getLogger("luna.forward")

REPLACE_BACKOFF_S = (0.2, 0.4, 0.8, 1.6, 3.2)
HOUR_CLOSE_DELAY_S = 120
DAY_CLOSE_DELAY_S = 3 * 3600
DAY_OVERDUE_S = 4 * 3600  # raw data but no manifest this long after the day: warn


class ClosedDayError(RuntimeError):
    """A write targeted a day whose manifest is already written."""


def replace_with_retry(src: Path, dst: Path, sleep: Callable[[float], None] = time.sleep) -> None:
    """``os.replace`` retried on PermissionError (Windows AV/indexer locks) 5 times with backoff."""
    for delay in (*REPLACE_BACKOFF_S, None):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if delay is None:
                raise
            sleep(delay)


_os_write = os.write  # indirection for tests (partial writes, ENOSPC)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = _os_write(fd, view)
        if n <= 0:
            raise OSError("os.write wrote nothing")
        view = view[n:]


def unlink_with_retry(path: Path, sleep: Callable[[float], None] = time.sleep) -> None:
    """``os.unlink`` with the same PermissionError backoff as :func:`replace_with_retry`."""
    for delay in (*REPLACE_BACKOFF_S, None):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if delay is None:
                raise
            sleep(delay)


def hour_parts(path: Path) -> list[Path]:
    """The closed parts of ``path``'s hour: ``HH.jsonl.gz`` then ``HH.late.N.jsonl.gz`` by N."""
    hh = path.name[:2]
    late = sorted(path.parent.glob(f"{hh}.late.*.jsonl.gz"), key=lambda p: int(p.name.split(".")[2]))
    base = path.parent / f"{hh}.jsonl.gz"
    return ([base] if base.exists() else []) + late


def already_closed(path: Path) -> bool:
    """True if the open file's bytes are already the tail of a closed part of its hour (a crash
    between the verified gz replace and the unlink): the open file is then a duplicate."""
    data = path.read_bytes()
    if not data:
        return True
    return any(gzip.decompress(gz.read_bytes()).endswith(data) for gz in hour_parts(path))


def atomic_write_bytes(path: Path, data: bytes, sleep: Callable[[float], None] = time.sleep) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        replace_with_retry(tmp, path, sleep)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, sleep: Callable[[float], None] = time.sleep) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), sleep)


def day_of(ns: int) -> str:
    return datetime.fromtimestamp(ns // NS, UTC).strftime("%Y-%m-%d")


def day_start_s(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_only(path: Path) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


def _empty_counts() -> dict[str, int]:
    return {"expected": 0, "received": 0, "lines": 0, "errors": 0, "bad_lines": 0}


class RawWriter:
    def __init__(self, root: Path, sleep: Callable[[float], None] = time.sleep) -> None:
        self.root = Path(root)
        self.sleep = sleep
        self._closed: set[str] | None = None  # cache of days with a manifest

    # ---- paths
    def manifest_path(self, day: str) -> Path:
        return self.root / "manifest" / f"date={day}.json"

    def path_for(self, source: str, recv_ns: int) -> Path:
        hour = datetime.fromtimestamp(recv_ns // NS, UTC).hour
        return self.root / "raw" / source / f"date={day_of(recv_ns)}" / f"{hour:02d}.jsonl"

    def _date_dirs(self) -> list[tuple[str, Path]]:
        """(day, raw/<source>/date=<day>) by listing the source dirs only (never their contents)."""
        raw = self.root / "raw"
        if not raw.is_dir():
            return []
        out = []
        for src in raw.iterdir():
            if src.is_dir():
                out += [(d.name.removeprefix("date="), d) for d in src.iterdir()
                        if d.name.startswith("date=")]
        return sorted(out)

    def _open_files(self, day: str | None = None) -> list[Path]:
        """Open hour/late files; date dirs of closed days (manifest written) are never scanned."""
        closed = set(self.closed_days())
        out: list[Path] = []
        for d, path in self._date_dirs():
            if d not in closed and (day is None or d == day):
                out += path.glob("*.jsonl")
        return sorted(out)

    def open_hours(self) -> int:
        return len(self._open_files())

    def closed_days(self) -> list[str]:
        if self._closed is None:
            found = (self.root / "manifest").glob("date=*.json")
            self._closed = {p.stem.removeprefix("date=") for p in found}
        return sorted(self._closed)

    def _raw_days(self) -> list[str]:
        return sorted({d for d, _ in self._date_dirs()})

    def overdue_unclosed_days(self, now_ns: int) -> list[str]:
        """Days with raw data but no manifest more than 4 h after their end (a stuck close)."""
        closed = set(self.closed_days())
        return [d for d in self._raw_days()
                if d not in closed and now_ns > (day_start_s(d) + 86400 + DAY_OVERDUE_S) * NS]

    # ---- writing
    def append(self, env: dict[str, Any]) -> Path:
        """Append one envelope line to its ``recv_ts`` hour file (unbuffered write loop + fsync).
        Rows for an hour that is already gzipped go to an open ``HH.late.<recv_ns>.jsonl`` whose own
        gz does not exist yet (a closed late file is never appended to). Any failure truncates the
        file back to its previous size, so no partial line is ever left behind."""
        recv_ns = ns_from_iso(env["recv_ts"])
        day = day_of(recv_ns)
        if day in set(self.closed_days()) or self.manifest_path(day).exists():
            raise ClosedDayError(f"{day} is closed (manifest written); refusing a {env['source']} row")
        path = self.path_for(env["source"], recv_ns)
        if path.with_name(path.name + ".gz").exists():
            hh = path.name[:2]
            open_late = [p for p in sorted(path.parent.glob(f"{hh}.late.*.jsonl"))
                         if not p.with_name(p.name + ".gz").exists()]
            path = open_late[0] if open_late else path.with_name(f"{hh}.late.{recv_ns}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(env, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o644)
        try:
            size = os.lseek(fd, 0, os.SEEK_END)
            try:
                _write_all(fd, line)
                os.fsync(fd)
            except BaseException:
                os.ftruncate(fd, size)  # never leave a partial line behind
                raise
        finally:
            os.close(fd)
        return path

    # ---- hour close
    def close_hours(self, now_ns: int) -> list[Path]:
        """Gzip every open hour (and late part) that ended at least 2 minutes before ``now_ns``."""
        closed = []
        for path in self._open_files():
            day = path.parent.name.removeprefix("date=")
            hour_end_s = day_start_s(day) + (int(path.name[:2]) + 1) * 3600
            if now_ns >= (hour_end_s + HOUR_CLOSE_DELAY_S) * NS:
                gz = self._close_hour(path)
                if gz is not None:
                    closed.append(gz)
        return closed

    def _close_hour(self, path: Path) -> Path | None:
        """``X.jsonl`` -> ``X.jsonl.gz``. If the bytes already sit in a closed part (crash after the
        verified replace, before the unlink) the file is only unlinked: rows are never duplicated."""
        gz = path.with_name(path.name + ".gz")
        content = path.read_bytes()
        if already_closed(path):
            unlink_with_retry(path, self.sleep)
            return gz if gz.exists() else None
        if gz.exists():  # unexpected extra rows next to a closed part: keep them as a new late part
            prior = gzip.decompress(gz.read_bytes())
            content = content[len(prior):] if content.startswith(prior) else content
            ns = time.time_ns()
            try:
                ns = ns_from_iso(json.loads(content.splitlines()[0])["recv_ts"])
            except (ValueError, KeyError, IndexError, TypeError):
                pass
            gz = path.with_name(f"{path.name[:2]}.late.{ns}.jsonl.gz")
            while gz.exists():
                ns += 1
                gz = path.with_name(f"{path.name[:2]}.late.{ns}.jsonl.gz")
        atomic_write_bytes(gz, gzip.compress(content, mtime=0), self.sleep)
        if gzip.decompress(gz.read_bytes()) != content:  # pragma: no cover - disk corruption
            raise OSError(f"verification of {gz} failed; keeping {path}")
        unlink_with_retry(path, self.sleep)
        return gz

    # ---- day close
    def close_days(self, now_ns: int, start_ns: int | None = None) -> list[str]:
        """Write the manifest of each finished day (>= 3h after its end, no open hour left).
        ``start_ns`` (collector_start_ts) trims the expected-row window of the first day. A day
        that fails is logged and retried next time; it never blocks the other days."""
        already = set(self.closed_days())
        done = []
        for day in self._raw_days():
            if day in already or now_ns < (day_start_s(day) + 86400 + DAY_CLOSE_DELAY_S) * NS:
                continue
            if self._open_files(day):
                continue
            try:
                self._close_day(day, now_ns, start_ns)
            except Exception as e:  # noqa: BLE001 - one bad day must not block the rest
                log.error("closing day %s failed (retried later): %s: %s", day, type(e).__name__, e)
                continue
            done.append(day)
        return done

    def _close_day(self, day: str, now_ns: int, start_ns: int | None) -> None:
        start_s = day_start_s(day)
        window = max(start_s, start_ns / NS) if start_ns is not None else start_s
        sources = {s: _empty_counts() for s in SOURCES}
        for s in SOURCES:
            sources[s]["expected"] = expected_rows(s, window, start_s + 86400)
        files = []
        for gz in sorted(g for d, p in self._date_dirs() if d == day for g in p.glob("*.jsonl.gz")):
            source = gz.parent.parent.name
            data = gz.read_bytes()
            rows, bad = [], 0
            for x in gzip.decompress(data).splitlines():
                if not x.strip():
                    continue
                try:
                    rows.append(json.loads(x))
                except ValueError:
                    bad += 1  # kept verbatim in the gz, only counted here
            ok = sum(1 for r in rows if isinstance(r, dict) and r.get("ok"))
            files.append({
                "path": gz.relative_to(self.root).as_posix(),
                "source": source,
                "hour": int(gz.name[:2]),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "lines": len(rows),
                "ok": ok,
                "errors": len(rows) - ok,
                "bad_lines": bad,
            })
            agg = sources.setdefault(source, _empty_counts())
            agg["received"] += ok
            agg["lines"] += len(rows)
            agg["errors"] += len(rows) - ok
            agg["bad_lines"] += bad
        manifest = {"date": day, "closed_ts": iso_ns(now_ns),
                    "collector_start_ts": iso_ns(start_ns) if start_ns is not None else None,
                    "files": files, "sources": sources}
        path = self.manifest_path(day)
        atomic_write_text(path, json.dumps(manifest, indent=1), self.sleep)
        self.closed_days()
        self._closed.add(day)
        for f in files:
            _read_only(self.root / f["path"])
        _read_only(path)
