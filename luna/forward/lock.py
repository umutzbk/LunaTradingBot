"""Single-instance lock ``var/forward/collector.lock`` (pid + host + heartbeat; spec §9).

A fresh lock (heartbeat <= 5 min) held by another process refuses a second collector. A stale lock
is taken over. Same host *and* pid means a restart of the same container (pid 1, same hostname):
the previous holder cannot be alive, so the lock is taken over at once.
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Callable
from pathlib import Path

from luna.forward.envelope import NS, iso_ns
from luna.forward.writer import atomic_write_text

STALE_AFTER_S = 300


class CollectorLocked(RuntimeError):
    """Another collector holds a fresh lock."""


class CollectorLock:
    def __init__(
        self,
        path: Path,
        clock_ns: Callable[[], int] = time.time_ns,
        pid: int | None = None,
        host: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.clock_ns = clock_ns
        self.pid = os.getpid() if pid is None else pid
        self.host = socket.gethostname() if host is None else host

    def _read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _mine(self, data: dict | None) -> bool:
        return bool(data) and data.get("pid") == self.pid and data.get("host") == self.host

    def _write(self) -> None:
        now = self.clock_ns()
        body = {"pid": self.pid, "host": self.host, "heartbeat_ns": now, "heartbeat_ts": iso_ns(now)}
        atomic_write_text(self.path, json.dumps(body))

    def acquire(self) -> None:
        data = self._read()
        if data is not None and not self._mine(data):
            age_s = (self.clock_ns() - int(data.get("heartbeat_ns", 0))) / NS
            if age_s <= STALE_AFTER_S:
                raise CollectorLocked(
                    f"collector lock {self.path} held by pid {data.get('pid')} on {data.get('host')} "
                    f"(heartbeat {age_s:.0f}s ago)"
                )
        self._write()

    def heartbeat(self) -> None:
        self._write()

    def release(self) -> None:
        if self._mine(self._read()):
            self.path.unlink(missing_ok=True)
