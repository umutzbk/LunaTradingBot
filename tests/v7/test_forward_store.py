"""Task 2 forward-store tests (spec S-1.7 M3-3, EC-8, DEC-45, §15, L7)."""

from __future__ import annotations

import hashlib
import json

import pytest

from luna.v7.forward_store import ForwardStore, SealedBeforeL7, extract_transport_id
from luna.v7.timeutil import D, to_ns

T0 = to_ns("2026-10-15")
L7 = T0 + 180 * D


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


def test_forward_store_unreadable_before_l7(tmp_path):
    s = ForwardStore(tmp_path / "fs")
    rec = s.write_raw("https://example/a", b"abc", T0)
    for now, l7 in ((L7 - 1, L7), (L7 + 5 * D, None)):  # before L7; L7 unknown (R_V6 undetermined)
        with pytest.raises(SealedBeforeL7):
            s.read(rec.sha256, now_ns=now, l7_ns=l7)
        with pytest.raises(SealedBeforeL7):
            list(s.iter_records(now_ns=now, l7_ns=l7))
    assert s.read(rec.sha256, now_ns=L7, l7_ns=L7) == b"abc"
    assert [r.sha256 for r in s.iter_records(now_ns=L7, l7_ns=L7)] == [rec.sha256]
    assert not hasattr(s, "read_unchecked")


def test_read_detects_tampering(tmp_path):
    s = ForwardStore(tmp_path / "fs")
    rec = s.write_raw("u", b"abc", T0)
    (tmp_path / "fs" / "blobs" / rec.sha256).write_bytes(b"zzz")
    with pytest.raises(ValueError, match="sha256"):
        s.read(rec.sha256, now_ns=L7, l7_ns=L7)


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
