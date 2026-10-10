"""Read-only access to the forward collector's raw files (luna/forward/writer.py layout).

``var/forward/raw/<source>/date=YYYY-MM-DD/HH.jsonl`` is the open hour (one envelope per line,
appended + fsynced by the collector); two minutes after the hour it becomes ``HH.jsonl.gz`` and the
``.jsonl`` is deleted. The bot never writes there. To stay out of the collector's way on Windows
(an open handle blocks its replace/unlink) a ``.jsonl`` is opened only while its hour is current or
ended less than 60 s ago, each read opens/reads/closes at once, and closed hours are read from
their ``.gz``.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd

from luna.carry.funding import FundingStore
from luna.carry.market import Book, MarketSnapshot, parse_book, parse_tickers

log = logging.getLogger("luna.carry")

TAIL_BYTES = 256 * 1024
BOOK_TAIL_BYTES = 1024 * 1024  # one 5-minute round of 21 full books is ~250 KB
OPEN_GRACE = pd.Timedelta(seconds=60)


class CollectorSource:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)  # var/forward
        self._stat: tuple[str, int, int] | None = None  # (path, size, mtime_ns) of the last tickers read
        self._last_snap: MarketSnapshot | None = None

    # --- paths ---------------------------------------------------------------------------------------

    def _hour_file(self, source: str, hour: pd.Timestamp, gz: bool) -> Path:
        p = self.root / "raw" / source / f"date={hour:%Y-%m-%d}" / f"{hour:%H}.jsonl"
        return p.with_name(p.name + ".gz") if gz else p

    def _files(self, source: str, now: pd.Timestamp) -> list[Path]:
        """Newest first: the current hour's open file, then the previous hour (open file only in the
        first minute of the new hour, else its gz)."""
        cur = now.floor("h")
        prev = cur - pd.Timedelta(hours=1)
        out = [self._hour_file(source, cur, False)]
        if now - cur <= OPEN_GRACE:
            out.append(self._hour_file(source, prev, False))
        out.append(self._hour_file(source, prev, True))
        return out

    # --- reading -------------------------------------------------------------------------------------

    @staticmethod
    def _lines(path: Path, tail: int | None = None) -> list[bytes]:
        """Complete lines of ``path`` (a torn last line is dropped); gz files are read whole."""
        try:
            if path.suffix == ".gz":
                data = gzip.decompress(path.read_bytes())
            else:
                with open(path, "rb") as f:
                    if tail is not None:
                        size = f.seek(0, os.SEEK_END)
                        f.seek(max(0, size - tail))
                    data = f.read()
        except (OSError, EOFError, gzip.BadGzipFile) as e:
            if not isinstance(e, FileNotFoundError):
                log.warning("collector file %s unreadable: %s", path, e)
            return []
        lines = data.split(b"\n")
        return [x for x in lines[:-1] if x.strip()]  # last element: torn line or b""

    @staticmethod
    def _envs(lines: list[bytes]) -> list[dict[str, Any]]:
        out = []
        for x in lines:
            try:
                e = json.loads(x)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
        return out

    def latest_tickers(self, now: pd.Timestamp) -> MarketSnapshot | None:
        """The newest successful ``fut_tickers`` snapshot (None if none is readable)."""
        for path in self._files("fut_tickers", now):
            if not path.exists():
                continue
            for env in reversed(self._envs(self._lines(path, TAIL_BYTES if path.suffix != ".gz" else None))):
                snap = parse_tickers(env)
                if snap is not None:
                    self._last_snap = snap
                    return snap
        return None

    def has_new_tickers(self, now: pd.Timestamp, last_ts: pd.Timestamp | None) -> bool:
        """Cheap change detection (stat only); True when a snapshot newer than ``last_ts`` may exist."""
        if last_ts is None:
            return True
        cur = self._hour_file("fut_tickers", now.floor("h"), False)
        try:
            st = cur.stat()
        except OSError:
            return False
        key = (str(cur), st.st_size, st.st_mtime_ns)
        if key == self._stat:
            return False
        self._stat = key
        snap = self.latest_tickers(now)
        return snap is not None and snap.ts > last_ts

    def latest_books(self, now: pd.Timestamp) -> dict[str, tuple[pd.Timestamp, Book]]:
        """The newest ``fut_book`` (full order book, every 5 min) per asset from the newest file."""
        out: dict[str, tuple[pd.Timestamp, Book]] = {}
        for path in self._files("fut_book", now):
            if not path.exists():
                continue
            for env in self._envs(self._lines(path, BOOK_TAIL_BYTES if path.suffix != ".gz" else None)):
                b = parse_book(env)
                if b is not None and (b[0] not in out or b[1] >= out[b[0]][0]):
                    out[b[0]] = (b[1], b[2])
            if out:
                return out
        return out

    def refresh_funding(self, store: FundingStore, now: pd.Timestamp) -> int:
        """Merge the newest hourly ``fut_funding`` round (each envelope holds the last 48 h)."""
        for path in self._files("fut_funding", now):
            if path.exists():
                n = sum(store.merge_envelope(e) for e in self._envs(self._lines(path)))
                if n:
                    return n
        return 0

    def bootstrap_funding(self, store: FundingStore, now: pd.Timestamp, keep_hours: int) -> int:
        """Load the newest daily ``fut_funding_full`` round (full history per perp), then the newest
        hourly round, and keep only the last ``keep_hours``."""
        base = self.root / "raw" / "fut_funding_full"
        n = 0
        days = (
            sorted((d for d in base.iterdir() if d.name.startswith("date=")), reverse=True)
            if base.is_dir()
            else []
        )
        for d in days:
            files = sorted(d.glob("*.jsonl.gz")) + [
                p for p in sorted(d.glob("*.jsonl")) if self._open_ok(p, now)
            ]
            for path in files:
                n += sum(store.merge_envelope(e) for e in self._envs(self._lines(path)))
            if n:
                break
        n += self.refresh_funding(store, now)
        store.prune(now - pd.Timedelta(hours=keep_hours))
        return n

    def _open_ok(self, path: Path, now: pd.Timestamp) -> bool:
        """An open .jsonl may be read only while its hour is current or just ended."""
        try:
            day = pd.Timestamp(path.parent.name.removeprefix("date="), tz="UTC")
            hour = day + pd.Timedelta(hours=int(path.name[:2]))
        except ValueError:
            return False
        return now - (hour + pd.Timedelta(hours=1)) <= OPEN_GRACE
