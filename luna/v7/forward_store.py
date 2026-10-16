"""Sealed, write-only forward store (spec S-1.7 M3-3, EC-8, DEC-45, section 15, L7, L10).

Raw responses go directly into a content-addressed store with ``recv_ts``, source URL and SHA-256. There is
no read path before L7: ``read`` / ``iter_records`` take no time or L7 argument and refuse unless the
canonical L7 exists (``release.l7``: the hash-checked R_V6 release and T_F7 review-pass records, SC-06 /
AMB-34) and the guards' trusted clock (``release.trusted_now_ns``, the wall clock) has reached it. No caller
value can unseal the store (red-team M5), and no caller repository either (red-team r2 MJ5: a forged scratch
repository with valid-looking records unsealed it): the canonical repository is resolved only through the
guards' single location seam ``guard._roots`` (tests monkeypatch it).

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

from luna.v7 import release
from luna.v7.constants import FROZEN
from luna.v7.timeutil import D, to_ns


class SealedBeforeL7(RuntimeError):
    pass


@dataclass(frozen=True)
class Record:
    source_url: str
    recv_ts: int
    sha256: str
    size: int


def _canonical_repo() -> Path:
    from luna.v7 import guard  # local: guard imports this module

    return Path(guard._roots()[0])


def _gate() -> int | None:
    """Refuse before the canonical L7; return the ``recv_ts`` bound of the readable records: T_F7 + 180 d
    until the extended L7 (records of the extension window [T_F7 + 180 d, ...) stay sealed until the extended
    look, review m2), None (no bound) from the extended L7 on. The records come from the canonical repository
    only."""
    repo = _canonical_repo()
    l7 = release.l7(repo)
    now = release.trusted_now_ns()
    if l7 is None or now < l7:
        raise SealedBeforeL7("the forward store is sealed until the canonical L7 (and while L7 / R_V6 / T_F7 "
                             "is undetermined)")
    ext = release.l7(repo, extended=True)
    if ext is not None and now >= ext:
        return None
    return int(release.t_f7(repo)) + FROZEN.forward.days * D  # type: ignore[arg-type]


class ForwardStore:
    """Append-only: blobs/<sha256> (content addressed) + index.jsonl (one line per snapshot). The read gate
    uses the canonical records of the code repository (``guard._roots``); no repository is taken."""

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

    def _records(self) -> list[Record]:
        if not self._index.exists():
            return []
        return [Record(**json.loads(line)) for line in self._index.read_text(encoding="utf-8").splitlines()]

    def iter_records(self) -> Iterator[Record]:
        bound = _gate()
        for r in self._records():
            if bound is None or r.recv_ts < bound:
                yield r

    def read(self, sha256: str) -> bytes:
        bound = _gate()
        if not sha256.isalnum():
            raise ValueError("invalid sha256")
        if bound is not None and not any(r.sha256 == sha256 and r.recv_ts < bound for r in self._records()):
            raise SealedBeforeL7("this blob was received only in the extension window: sealed until the "
                                 "extended L7")
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
