"""``var/forward/status.json`` (spec §8): per-source health counters written atomically every loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from luna.forward.config import SOURCES
from luna.forward.envelope import iso_ns
from luna.forward.writer import atomic_write_text, day_of

STATUS_FILE = "status.json"


def _source_entry(day: str) -> dict[str, Any]:
    return {"last_ok_recv_ts": None, "last_error_ts": None, "last_error": None,
            "today": {"date": day, "ok": 0, "error": 0}}


def read_status(root: Path) -> dict[str, Any] | None:
    try:
        return json.loads((Path(root) / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class StatusTracker:
    def __init__(self, root: Path, now_ns: int) -> None:
        self.root = Path(root)
        self.started_ts = iso_ns(now_ns)
        today = day_of(now_ns)
        prev = read_status(root) or {}
        self.sources: dict[str, dict[str, Any]] = {}
        for s in SOURCES:
            entry = _source_entry(today)
            old = (prev.get("sources") or {}).get(s)
            if old:  # resume last-ok/last-error and, on the same UTC day, today's counters
                for k in ("last_ok_recv_ts", "last_error_ts", "last_error"):
                    entry[k] = old.get(k)
                if (old.get("today") or {}).get("date") == today:
                    entry["today"] = dict(old["today"])
            self.sources[s] = entry
        self.backup: dict[str, Any] = {"backup_ok": False, "backup_reason": "backup not run yet",
                                       "backup_warnings": [], "backup_last_run_ts": None}

    def record(self, env: dict[str, Any]) -> None:
        entry = self.sources.setdefault(env["source"], _source_entry(env["recv_ts"][:10]))
        day = env["recv_ts"][:10]
        if entry["today"]["date"] != day:
            entry["today"] = {"date": day, "ok": 0, "error": 0}
        if env["ok"]:
            entry["last_ok_recv_ts"] = env["recv_ts"]
            entry["today"]["ok"] += 1
        else:
            entry["last_error_ts"] = env["recv_ts"]
            entry["last_error"] = env["error"]
            entry["today"]["error"] += 1

    def record_error(self, source: str, ts: str, error: str) -> None:
        """A failure outside an envelope (e.g. the row could not be written)."""
        entry = self.sources.setdefault(source, _source_entry(ts[:10]))
        entry["last_error_ts"], entry["last_error"] = ts, error

    def set_backup(self, ok: bool, reason: str | None, warnings: list[str], now_ns: int) -> None:
        self.backup = {"backup_ok": ok, "backup_reason": reason, "backup_warnings": list(warnings),
                       "backup_last_run_ts": iso_ns(now_ns)}

    def write(self, now_ns: int, collector_start_ts: str | None, open_hours: int,
              closed_days: list[str]) -> None:
        today = day_of(now_ns)
        for entry in self.sources.values():
            if entry["today"]["date"] != today:
                entry["today"] = {"date": today, "ok": 0, "error": 0}
        body = {
            "updated_ts": iso_ns(now_ns),
            "started_ts": self.started_ts,
            "collector_start_ts": collector_start_ts,
            "sources": self.sources,
            "open_hours": open_hours,
            "closed_days": closed_days[-7:],
            **self.backup,
        }
        atomic_write_text(self.root / STATUS_FILE, json.dumps(body, indent=1))
