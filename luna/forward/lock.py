"""Single-instance lock ``var/forward/collector.lock`` (pid + host + heartbeat; spec §9).

A fresh lock (heartbeat <= 5 min) held by another process refuses a second collector. A stale lock
is taken over. Same host *and* pid means a restart of the same container (pid 1, same hostname):
the previous holder cannot be alive, so the lock is taken over at once. Same host with another pid
is taken over only if that pid is not alive (compose gives the collector the fixed hostname
``luna-collector``, so a recreated container is "the same host"). Another host with a fresh
heartbeat is always refused. ``heartbeat()`` raises if the lock is no longer ours.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path

from luna.forward.envelope import NS, iso_ns
from luna.forward.writer import atomic_write_text

STALE_AFTER_S = 300


class CollectorLocked(RuntimeError):
    """Another collector holds a fresh lock (or took ours over)."""


def pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` runs on this machine/namespace (never signals it)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: exists
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    if Path("/proc").is_dir():
        return Path(f"/proc/{pid}").exists()
    try:
        os.kill(pid, 0)  # POSIX signal 0: existence check only
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class CollectorLock:
    def __init__(
        self,
        path: Path,
        clock_ns: Callable[[], int] = time.time_ns,
        pid: int | None = None,
        host: str | None = None,
        pid_alive: Callable[[int], bool] = pid_alive,
    ) -> None:
        self.path = Path(path)
        self.clock_ns = clock_ns
        self.pid = os.getpid() if pid is None else pid
        self.host = socket.gethostname() if host is None else host
        self.pid_alive = pid_alive

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
            same_host_dead = data.get("host") == self.host and not self.pid_alive(int(data.get("pid", -1)))
            if age_s <= STALE_AFTER_S and not same_host_dead:
                raise CollectorLocked(
                    f"collector lock {self.path} held by pid {data.get('pid')} on {data.get('host')} "
                    f"(heartbeat {age_s:.0f}s ago)"
                )
        self._write()

    def heartbeat(self) -> None:
        data = self._read()
        if data is not None and not self._mine(data):
            raise CollectorLocked(f"collector lock {self.path} was taken over by pid {data.get('pid')} "
                                  f"on {data.get('host')}")
        self._write()

    def release(self) -> None:
        if self._mine(self._read()):
            self.path.unlink(missing_ok=True)
