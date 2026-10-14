"""Sealed, write-only forward store (spec S-1.7 M3-3, EC-8, DEC-45, section 15, L7, L10).

Raw responses go directly into a content-addressed store with ``recv_ts``, source URL and SHA-256. There is
no read path before L7: ``read`` / ``iter_records`` refuse unless L7 is known and ``now_ns >= L7``. L7
itself needs R_V6, which is undetermined, so L7 is an injected parameter (ns or None; None = sealed).

``extract_transport_id`` is the only parsing allowed before L7: the article id / code / URL needed to fetch
the raw body. It never returns titles, bodies, dates or anything semantic (EC-8).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from luna.v7.timeutil import to_ns


class SealedBeforeL7(RuntimeError):
    pass


@dataclass(frozen=True)
class Record:
    source_url: str
    recv_ts: int
    sha256: str
    size: int


def _gate(now_ns: int, l7_ns: int | None) -> None:
    if l7_ns is None or now_ns < l7_ns:
        raise SealedBeforeL7("the forward store is sealed until L7 (and while L7 / R_V6 is undetermined)")


class ForwardStore:
    """Append-only: blobs/<sha256> (content addressed) + index.jsonl (one line per snapshot)."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._blobs = self.root / "blobs"
        self._index = self.root / "index.jsonl"

    def write_raw(self, source_url: str, body: bytes, recv_ts: int) -> Record:
        if not isinstance(body, (bytes, bytearray)):
            raise ValueError("body must be raw bytes")
        recv_ts = to_ns(recv_ts)  # rejects small ints of ambiguous unit
        sha = hashlib.sha256(body).hexdigest()
        self._blobs.mkdir(parents=True, exist_ok=True)
        blob = self._blobs / sha
        if not blob.exists():
            tmp = blob.with_suffix(".tmp")
            tmp.write_bytes(bytes(body))
            os.replace(tmp, blob)
        rec = Record(source_url, recv_ts, sha, len(body))
        with open(self._index, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec), sort_keys=True) + "\n")
        return rec

    def iter_records(self, *, now_ns: int, l7_ns: int | None) -> Iterator[Record]:
        _gate(now_ns, l7_ns)
        if not self._index.exists():
            return
        for line in self._index.read_text(encoding="utf-8").splitlines():
            yield Record(**json.loads(line))

    def read(self, sha256: str, *, now_ns: int, l7_ns: int | None) -> bytes:
        _gate(now_ns, l7_ns)
        if not sha256.isalnum():
            raise ValueError("invalid sha256")
        data = (self._blobs / sha256).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError(f"stored blob does not match its sha256 {sha256}")
        return data


def extract_transport_id(catalog_json: bytes | str | dict | list) -> list[str]:
    """EC-8 transport orchestration: article code (else id) and url of each entry of an ``articles`` list in
    the raw catalog response. Assumed minimal shape (AMB-01): ``{... "articles": [{"id", "code", "url",
    ...}]}`` at any depth. Nothing else (titles, bodies, dates) is read or returned."""
    obj = json.loads(catalog_json) if isinstance(catalog_json, (bytes, bytearray, str)) else catalog_json
    out: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "articles" and isinstance(v, list):
                    for a in v:
                        if isinstance(a, dict):
                            ident = a.get("code") if a.get("code") not in (None, "") else a.get("id")
                            for x in (ident, a.get("url")):
                                if isinstance(x, (str, int)) and not isinstance(x, bool) and str(x) != "":
                                    out.append(str(x))
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return list(dict.fromkeys(out))
