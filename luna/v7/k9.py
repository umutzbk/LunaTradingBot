"""K9 refusal log (spec §16 K9 "guard refusal; logged; incident report"; S-1.3, S-1.7, L10; SC-03, AMB-35).

Every guard refusal of the seal (an S-1.7 endpoint refused before network I/O, a W_HOLD read before the
opening, an M6 read before R_V6, a W_FWD7 read before L7, an N6 / N2 / E6 guard failure) is appended, as one
JSON line (UTC time, request, rule, reason, exception, sha256 of the record), to the append-only
``K9_REFUSALS.jsonl`` of a ``K9Log`` and gets an incident-report stub (exclusive create, never overwritten)
in its incident directory, to be completed by the operator.

Location: a run configures its log under its own output directory (``configure(K9Log.under(out_dir))`` or
``using(...)``); the fetch layer (``luna.v7.fetch.Fetcher``) always has one (its run output directory, or an
injected ``K9Log``). Logging never replaces the refusal: the refusal is raised in every case; a log that is
not configured or cannot be written is reported as a note on the raised exception (never swallowed).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pandas as pd

REFUSALS = "K9_REFUSALS.jsonl"
INCIDENTS = "incidents"

__all__ = ["INCIDENTS", "REFUSALS", "K9Log", "configure", "current", "log_refusal", "using"]


def _json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


class K9Log:
    """An append-only refusal log (``log_dir / K9_REFUSALS.jsonl``) and its incident stubs (``incident_dir``,
    default ``log_dir / incidents``). Both directories are injectable; ``clock`` (ns UTC) for tests."""

    def __init__(self, log_dir: str | Path, incident_dir: str | Path | None = None, *,
                 clock: Callable[[], int] = time.time_ns) -> None:
        self.log_dir = Path(log_dir)
        self.incident_dir = Path(incident_dir) if incident_dir is not None else self.log_dir / INCIDENTS
        self._clock = clock

    @classmethod
    def under(cls, out_dir: str | Path) -> K9Log:
        """The default location: ``<run output directory>/k9``."""
        return cls(Path(out_dir) / "k9")

    def record(self, *, request: Mapping, rule: str, reason: str, exception: str = "",
               extra: Mapping | None = None) -> dict:
        """Append one refusal record (fsync) and write its incident stub; returns the record."""
        utc = pd.Timestamp(int(self._clock()), unit="ns", tz="UTC")
        rec = {"utc": utc.isoformat(), "request": json.loads(_json(dict(request))), "rule": str(rule),
               "reason": str(reason), "exception": str(exception), **json.loads(_json(dict(extra or {})))}
        rec["sha256"] = hashlib.sha256(_json(rec).encode("utf-8")).hexdigest()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with open(self.log_dir / REFUSALS, "a", encoding="utf-8") as f:
            f.write(_json(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._incident(rec, utc)
        return rec

    def _incident(self, rec: dict, utc: pd.Timestamp) -> Path:
        self.incident_dir.mkdir(parents=True, exist_ok=True)
        stem = f"K9-{utc.strftime('%Y%m%dT%H%M%S%f')}Z-{rec['sha256'][:12]}"
        text = "\n".join([
            f"# K9 incident report (stub): {rec['rule']}", "",
            "Spec §16 K9: guard refusal; logged; incident report. Status: OPEN - to be completed by the "
            "operator (cause, whether any content was read, follow-up). The refusal itself was raised.", "",
            f"- UTC: {rec['utc']}", f"- Rule: {rec['rule']}", f"- Exception: {rec['exception']}",
            f"- Reason: {rec['reason']}", f"- Request: `{_json(rec['request'])}`",
            f"- Record sha256 (`{REFUSALS}`): {rec['sha256']}", ""])
        for i in range(1000):
            p = self.incident_dir / (f"{stem}.md" if i == 0 else f"{stem}-{i}.md")
            try:
                fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            return p
        raise OSError(f"no free incident file name for {stem}")

    def records(self) -> list[dict]:
        p = self.log_dir / REFUSALS
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]

    def incidents(self) -> list[Path]:
        return sorted(self.incident_dir.glob("K9-*.md")) if self.incident_dir.exists() else []


_CURRENT: K9Log | None = None


def configure(log: K9Log | None) -> K9Log | None:
    """Set the process's K9 log (the run's; ``K9Log.under(out_dir)``); returns the previous one."""
    global _CURRENT
    prev, _CURRENT = _CURRENT, log
    return prev


def current() -> K9Log | None:
    return _CURRENT


@contextlib.contextmanager
def using(log: K9Log | None) -> Iterator[K9Log | None]:
    """``configure(log)`` for the duration of the block (the previous log is restored)."""
    prev = configure(log)
    try:
        yield log
    finally:
        configure(prev)


def log_refusal(exc: BaseException, *, request: Mapping, rule: str, log: K9Log | None = None,
                extra: Mapping | None = None) -> dict | None:
    """Record a guard refusal in ``log`` (default: the configured one). Never raises and never replaces the
    refusal: an absent or failing log is noted on ``exc`` (the caller re-raises it)."""
    lg = log if log is not None else _CURRENT
    if lg is None:
        exc.add_note("K9 refusal log not configured: this refusal was NOT logged (configure luna.v7.k9 with "
                     "the run's output directory)")
        return None
    try:
        return lg.record(request=request, rule=rule, reason=str(exc), exception=type(exc).__name__,
                         extra=extra)
    except (OSError, TypeError, ValueError) as e:
        exc.add_note(f"K9 refusal log not written ({type(e).__name__}: {e}); the refusal stands")
        return None
