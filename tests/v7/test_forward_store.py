"""Task 2 forward-store tests (spec S-1.7 M3-3, EC-8, DEC-45, §15, L7). The read gate is the canonical L7
(SC-06): a canonical release / review-pass fixture repository and the pinned trusted clock
(``auth_fixtures``); no time or L7 argument exists."""

from __future__ import annotations

import hashlib
import json

import pytest

from luna.v7.forward_store import ForwardStore, SealedBeforeL7, extract_transport_id
from luna.v7.timeutil import D, to_ns
from tests.v7 import auth_fixtures as af

T0 = to_ns("2026-10-15")
L7 = to_ns("2027-10-15")  # canonical: T_F7 = 2026-10-16, R_V6 = 2027-10-14 -> max(T_F7 + 180 d, R_V6) + 1 d


def test_write_records_sha_and_is_append_only(tmp_path):
    s = ForwardStore(tmp_path / "fs")
    body = b'{"x":1}'
    rec = s.write_raw("https://example/a", body, T0)
    assert rec.sha256 == hashlib.sha256(body).hexdigest()
    assert rec.source_url == "https://example/a" and rec.recv_ts == T0 and rec.size == len(body)
    rec2 = s.write_raw("https://example/a", body, T0 + 1)  # same bytes, new snapshot: both kept
    assert rec2.recv_ts == T0 + 1
    lines = (tmp_path / "fs" / "index.jsonl").read_text().splitlines()
    assert [json.loads(x)["recv_ts"] for x in lines] == [T0, T0 + 1]
    with pytest.raises(ValueError):
        s.write_raw("https://example/a", "str", T0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        s.write_raw("https://example/a", body, 123)  # small int: ambiguous unit


def test_forward_store_unreadable_before_l7(tmp_path, monkeypatch):
    canon = af.canonical_repo(tmp_path / "canon")
    none = af.canonical_repo(tmp_path / "none", r_v6=None)  # R_V6 undetermined: L7 unknown
    rec = ForwardStore(tmp_path / "fs").write_raw("https://example/a", b"abc", T0)
    for repo, now in ((canon, L7 - 1), (none, L7 + 5 * D), (None, L7 + 5 * D)):
        s = ForwardStore(tmp_path / "fs", repo=repo)
        af.clock(monkeypatch, now)
        with pytest.raises(SealedBeforeL7):
            s.read(rec.sha256)
        with pytest.raises(SealedBeforeL7):
            list(s.iter_records())
    s = ForwardStore(tmp_path / "fs", repo=canon)
    af.clock(monkeypatch, L7)
    assert s.read(rec.sha256) == b"abc"
    assert [r.sha256 for r in s.iter_records()] == [rec.sha256]
    assert not hasattr(s, "read_unchecked")


def test_read_detects_tampering(tmp_path, monkeypatch):
    s = ForwardStore(tmp_path / "fs", repo=af.canonical_repo(tmp_path / "canon"))
    rec = s.write_raw("u", b"abc", T0)
    (tmp_path / "fs" / "blobs" / rec.sha256).write_bytes(b"zzz")
    af.clock(monkeypatch, L7)
    with pytest.raises(ValueError, match="sha256"):
        s.read(rec.sha256)


def test_transport_extraction_no_semantics():
    cat = {"code": "000000", "message": None, "data": {"catalogs": [{"catalogId": 161, "articles": [
        {"id": 111, "code": "abc123", "title": "Binance Will Delist FOO", "releaseDate": 1,
         "body": "FOO delisted at 2026-10-20"},
        {"id": 222, "title": "Notice of New Listing", "url": "https://www.binance.com/en/support/announcement/x"},
    ]}]}}
    out = extract_transport_id(json.dumps(cat).encode())
    assert out == ["abc123", "222", "https://www.binance.com/en/support/announcement/x"]
    assert extract_transport_id(cat) == out
    blob = " ".join(out)
    for forbidden in ("Delist", "FOO", "Listing", "delisted"):
        assert forbidden not in blob
    assert all(isinstance(x, str) for x in out)
    assert extract_transport_id(b'{"data": {}}') == []
    with pytest.raises(ValueError):
        extract_transport_id(b"not json")
