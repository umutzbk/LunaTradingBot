"""``var/forward/status.json`` (spec §8): per-source health counters written atomically every loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from luna.forward.config import SOURCES
from luna.forward.envelope import NS, iso_ns, ns_from_iso
from luna.forward.writer import atomic_write_text, day_of

STATUS_FILE = "status.json"


def _source_entry(day: str) -> dict[str, Any]:
    return {"last_ok_recv_ts": None, "last_error_ts": None, "last_error": None,
            "today": {"date": day, "ok": 0, "error": 0}, "skipped_calls": 0, "skipped_slots": 0,
            "last_skipped_ts": None}


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
                for k in ("last_ok_recv_ts", "last_error_ts", "last_error", "skipped_calls", "skipped_slots",
                          "last_skipped_ts"):
                    entry[k] = old.get(k, entry[k])
                if (old.get("today") or {}).get("date") == today:
                    entry["today"] = dict(old["today"])
            self.sources[s] = entry
        self.unclosed_days: list[str] = []
        self.backup_running = False
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

    def record_skipped(self, source: str, n: int, ts: str) -> None:
        """Calls of an aborted round (its next slot was due): gap info only, never fake rows."""
        entry = self.sources.setdefault(source, _source_entry(ts[:10]))
        entry["skipped_calls"] = entry.get("skipped_calls", 0) + n
        entry["last_skipped_ts"] = ts

    def record_skipped_slots(self, source: str, n: int, ts: str) -> None:
        """Whole scheduled slots that were never started (gap info only)."""
        entry = self.sources.setdefault(source, _source_entry(ts[:10]))
        entry["skipped_slots"] = entry.get("skipped_slots", 0) + n
        entry["last_skipped_ts"] = ts

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
            "unclosed_days": list(self.unclosed_days),
            **self.backup,
            "backup_running": self.backup_running,
        }
        atomic_write_text(self.root / STATUS_FILE, json.dumps(body, indent=1))


HEALTH_MAX_AGE_S = {"fut_tickers": 180, "spot_ticker": 180, "fut_funding": 2 * 3600}
REFERENCE_MAX_AGE_S = {s: 26 * 3600 for s in ("fut_instruments", "spot_assetpairs", "fut_funding_full")}


def _age_s(ts: str | None, now_ns: int) -> float | None:
    return None if not ts else (now_ns - ns_from_iso(ts)) / NS


def check_health(status: dict[str, Any] | None, now_ns: int, strict: bool = False) -> tuple[int, list[str]]:
    """(exit code, lines) for ``luna collector-health``: 0 healthy, 1 unhealthy, 2 backup warning
    under ``--strict``. Healthy: fut_tickers and spot_ticker ok < 3 min ago, fut_funding < 2 h ago
    (a funding source that never succeeded is tolerated during the first 2 h of the process).
    Daily reference sources older than 26 h and backup problems only add WARNING lines."""
    if status is None:
        return 1, ["collector-health: status.json missing or unreadable"]
    sources = status.get("sources") or {}
    lines: list[str] = []
    healthy = True
    uptime = _age_s(status.get("started_ts"), now_ns)
    for src, max_age in HEALTH_MAX_AGE_S.items():
        age = _age_s((sources.get(src) or {}).get("last_ok_recv_ts"), now_ns)
        if age is None and src == "fut_funding" and uptime is not None and uptime < max_age:
            lines.append(f"{src}: no success yet (process up {uptime / 60:.0f} min, grace 2h)")
        elif age is None or age >= max_age:
            healthy = False
            lines.append(f"{src}: STALE, last ok " + ("never" if age is None else f"{age:.0f}s ago"))
        else:
            lines.append(f"{src}: ok, last ok {age:.0f}s ago")
    for src, max_age in REFERENCE_MAX_AGE_S.items():
        age = _age_s((sources.get(src) or {}).get("last_ok_recv_ts"), now_ns)
        if age is None or age > max_age:
            last = "never" if age is None else f"{age / 3600:.1f}h ago"
            lines.append(f"WARNING {src}: last ok {last} (> 26h)")
    backup_warnings = []
    if not status.get("backup_ok"):
        backup_warnings.append(status.get("backup_reason") or "backup not ok")
    backup_warnings += [w for w in status.get("backup_warnings") or [] if w not in backup_warnings]
    lines += [f"WARNING backup: {w}" for w in backup_warnings]
    unclosed = list(status.get("unclosed_days") or [])
    lines += [f"WARNING day {d} has raw data but no manifest > 4h after its end (close failing)"
              for d in unclosed]
    if not healthy:
        return 1, ["collector-health: unhealthy", *lines]
    if strict and (backup_warnings or unclosed):
        return 2, ["collector-health: ok, but backup/day-close warning (--strict)", *lines]
    return 0, ["collector-health: ok", *lines]


def forward_view(root: Path, now_ns: int) -> dict[str, Any]:
    """Health/coverage view for the dashboard "Forward data" page: status.json + day manifests
    only (no prices, no account database)."""
    root = Path(root)
    st = read_status(root)
    sources = []
    for name, e in ((st or {}).get("sources") or {}).items():
        today = e.get("today") or {}
        sources.append({
            "source": name,
            "last_ok_age_s": None if _age_s(e.get("last_ok_recv_ts"), now_ns) is None
            else round(_age_s(e.get("last_ok_recv_ts"), now_ns), 1),
            "ok_today": today.get("ok", 0),
            "errors_today": today.get("error", 0),
            "last_error": e.get("last_error"),
        })
    days = []
    for mpath in sorted((root / "manifest").glob("date=*.json"), reverse=True):
        try:
            m = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        backed = (root / "backup" / mpath.name).exists()
        for name, c in (m.get("sources") or {}).items():
            days.append({"date": m["date"], "source": name, "expected": c.get("expected", 0),
                         "received": c.get("received", 0),
                         "missing": max(0, c.get("expected", 0) - c.get("received", 0)),
                         "errors": c.get("errors", 0), "backed_up": backed})
    return {
        "status_found": st is not None,
        "updated_ts": (st or {}).get("updated_ts"),
        "collector_start_ts": (st or {}).get("collector_start_ts"),
        "open_hours": (st or {}).get("open_hours"),
        "backup_ok": bool((st or {}).get("backup_ok")),
        "backup_reason": (st or {}).get("backup_reason"),
        "backup_warnings": list((st or {}).get("backup_warnings") or []),
        "unclosed_days": list((st or {}).get("unclosed_days") or []),
        "sources": sources,
        "days": days,
    }
