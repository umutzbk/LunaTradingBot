"""``var/forward/meta.json``: collector_start_ts (written once, never overwritten) and the
per-source ``seq`` / shared ``pair_seq`` counters, resumed after a restart (spec §2, §4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from luna.forward.envelope import iso_ns, ns_from_iso
from luna.forward.writer import atomic_write_text

VERSION = 1


class Meta:
    def __init__(self, root: Path, data: dict[str, Any]) -> None:
        self.path = Path(root) / "meta.json"
        self.data = data
        self.data.setdefault("version", VERSION)
        self.data.setdefault("collector_start_ts", None)
        self.data.setdefault("seq", {})
        self.data.setdefault("pair_seq", 0)

    @classmethod
    def load(cls, root: Path) -> Meta:
        path = Path(root) / "meta.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return cls(root, data)

    @property
    def collector_start_ts(self) -> str | None:
        return self.data["collector_start_ts"]

    @property
    def collector_start_ns(self) -> int | None:
        ts = self.collector_start_ts
        return ns_from_iso(ts) if ts else None

    def start(self, now_ns: int) -> None:
        if not self.data["collector_start_ts"]:
            self.data["collector_start_ts"] = iso_ns(now_ns)

    def reserve(self, source: str, n: int) -> int:
        """Reserve ``n`` consecutive seqs for ``source``; returns the first. Saving before the
        calls means a crash can leave a gap in ``seq`` but never reuse one."""
        first = int(self.data["seq"].get(source, 0)) + 1
        self.data["seq"][source] = first + n - 1
        return first

    def next_seq(self, source: str) -> int:
        return self.reserve(source, 1)

    def next_pair_seq(self) -> int:
        self.data["pair_seq"] = int(self.data["pair_seq"]) + 1
        return self.data["pair_seq"]

    def save(self) -> None:
        atomic_write_text(self.path, json.dumps(self.data, indent=1))
