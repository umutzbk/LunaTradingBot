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
import os
import stat
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from luna.forward.config import SOURCES, expected_rows
from luna.forward.envelope import NS, iso_ns, ns_from_iso

REPLACE_BACKOFF_S = (0.2, 0.4, 0.8, 1.6, 3.2)
HOUR_CLOSE_DELAY_S = 120
DAY_CLOSE_DELAY_S = 3 * 3600


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
    return {"expected": 0, "received": 0, "lines": 0, "errors": 0}


class RawWriter:
    def __init__(self, root: Path, sleep: Callable[[float], None] = time.sleep) -> None:
        self.root = Path(root)
        self.sleep = sleep

    # ---- paths
    def manifest_path(self, day: str) -> Path:
        return self.root / "manifest" / f"date={day}.json"

    def path_for(self, source: str, recv_ns: int) -> Path:
        hour = datetime.fromtimestamp(recv_ns // NS, UTC).hour
        return self.root / "raw" / source / f"date={day_of(recv_ns)}" / f"{hour:02d}.jsonl"

    def _open_files(self, day: str = "*") -> list[Path]:
        return sorted((self.root / "raw").glob(f"*/date={day}/*.jsonl"))

    def open_hours(self) -> int:
        return len(self._open_files())

    def closed_days(self) -> list[str]:
        return sorted(p.stem.removeprefix("date=") for p in (self.root / "manifest").glob("date=*.json"))

    # ---- writing
    def append(self, env: dict[str, Any]) -> Path:
        """Append one envelope line to its ``recv_ts`` hour file (flush + fsync)."""
        recv_ns = ns_from_iso(env["recv_ts"])
        day = day_of(recv_ns)
        if self.manifest_path(day).exists():
            raise ClosedDayError(f"{day} is closed (manifest written); refusing a {env['source']} row")
        path = self.path_for(env["source"], recv_ns)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(env, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(path, "ab") as f:
            f.write(line.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        return path

    # ---- hour close
    def close_hours(self, now_ns: int) -> list[Path]:
        """Gzip every open hour that ended at least 2 minutes before ``now_ns``."""
        closed = []
        for path in self._open_files():
            day = path.parent.name.removeprefix("date=")
            hour_end_s = day_start_s(day) + (int(path.stem) + 1) * 3600
            if now_ns >= (hour_end_s + HOUR_CLOSE_DELAY_S) * NS:
                closed.append(self._close_hour(path))
        return closed

    def _close_hour(self, path: Path) -> Path:
        gz = path.with_name(path.name + ".gz")
        prior = gzip.decompress(gz.read_bytes()) if gz.exists() else b""
        content = prior + path.read_bytes()
        atomic_write_bytes(gz, gzip.compress(content, mtime=0), self.sleep)
        if gzip.decompress(gz.read_bytes()) != content:  # pragma: no cover - disk corruption
            raise OSError(f"verification of {gz} failed; keeping {path}")
        path.unlink()
        return gz

    # ---- day close
    def close_days(self, now_ns: int, start_ns: int | None = None) -> list[str]:
        """Write the manifest of each finished day (>= 3h after its end, no open hour left).
        ``start_ns`` (collector_start_ts) trims the expected-row window of the first day."""
        days = sorted({p.name.removeprefix("date=") for p in (self.root / "raw").glob("*/date=*")})
        already = set(self.closed_days())
        done = []
        for day in days:
            end_s = day_start_s(day) + 86400
            if day in already or now_ns < (end_s + DAY_CLOSE_DELAY_S) * NS or self._open_files(day):
                continue
            self._close_day(day, now_ns, start_ns)
            done.append(day)
        return done

    def _close_day(self, day: str, now_ns: int, start_ns: int | None) -> None:
        start_s = day_start_s(day)
        window = max(start_s, start_ns / NS) if start_ns is not None else start_s
        sources = {s: _empty_counts() for s in SOURCES}
        for s in SOURCES:
            sources[s]["expected"] = expected_rows(s, window, start_s + 86400)
        files = []
        for gz in sorted((self.root / "raw").glob(f"*/date={day}/*.jsonl.gz")):
            source = gz.parent.parent.name
            data = gz.read_bytes()
            rows = [json.loads(x) for x in gzip.decompress(data).splitlines() if x.strip()]
            ok = sum(1 for r in rows if r.get("ok"))
            files.append({
                "path": gz.relative_to(self.root).as_posix(),
                "source": source,
                "hour": int(gz.name[:2]),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "lines": len(rows),
                "ok": ok,
                "errors": len(rows) - ok,
            })
            agg = sources.setdefault(source, _empty_counts())
            agg["received"] += ok
            agg["lines"] += len(rows)
            agg["errors"] += len(rows) - ok
        manifest = {"date": day, "closed_ts": iso_ns(now_ns), "files": files, "sources": sources}
        path = self.manifest_path(day)
        atomic_write_text(path, json.dumps(manifest, indent=1), self.sleep)
        for f in files:
            _read_only(self.root / f["path"])
        _read_only(path)
