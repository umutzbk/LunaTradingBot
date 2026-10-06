import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pandas as pd
import pytest

from luna.data import kraken_spot_archive as ka
from luna.http import make_client

XBT15 = "1438956000,3.0,3.5,2.5,3.2,81.5,2\n1438956900,3.2,3.3,3.1,3.1,10.0,1\n"
XBT60 = "1438956000,3.0,3.5,2.5,3.2,91.5,5\n"
XBTT15 = "1438956000,9.0,9.0,9.0,9.0,1.0,1\n"


def make_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("TIMEANDSALES/XBTUSD_15.csv", XBT15)
        z.writestr("XBTUSD_60.csv", XBT60)
        z.writestr("XBTUSDT_15.csv", XBTT15)
    return buf.getvalue()


def split_parts(tmp_path: Path, data: bytes, n: int = 3) -> list[Path]:
    size = -(-len(data) // n)
    paths = []
    for i in range(n):
        p = tmp_path / f"a.zip.part0{i}"
        p.write_bytes(data[i * size : (i + 1) * size])
        paths.append(p)
    return paths


def test_multipart_reads_like_single_file(tmp_path):
    data = make_zip_bytes()
    parts = split_parts(tmp_path, data)
    with ka.MultiPartFile(parts) as f:
        assert f.seekable() and f.readable()
        assert f.read() == data
        f.seek(5)
        assert f.read(len(data) // 2) == data[5 : 5 + len(data) // 2]
        f.seek(-10, io.SEEK_END)
        assert f.read() == data[-10:]
        assert f.tell() == len(data)
        f.seek(len(parts[0].read_bytes()) - 2)
        assert f.read(5) == data[len(parts[0].read_bytes()) - 2 :][:5]
        buf = bytearray(7)
        f.seek(3)
        assert f.readinto(buf) == 7 and bytes(buf) == data[3:10]


def test_extract_15m_only_requested_pairs(tmp_path):
    parts = split_parts(tmp_path, make_zip_bytes())
    out = ka.extract_15m(parts, ["XBTUSD", "ETHUSD"])
    assert set(out) == {"XBTUSD"}
    df = out["XBTUSD"]
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "trades", "filled"]
    assert df.index.tz is not None and str(df.index.tz) == "UTC"
    assert df.index[0] == pd.Timestamp("2015-08-07 14:00", tz="UTC")
    assert df.index[1] == pd.Timestamp("2015-08-07 14:15", tz="UTC")
    assert len(df) == 2  # exact name only: XBTUSDT_15 and XBTUSD_60 excluded
    assert df["close"].tolist() == [3.2, 3.1]
    assert df["trades"].dtype == "int64"
    assert not df["filled"].any()


def serve(files: dict[str, bytes], seen: list):
    def handler(req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1]
        seen.append((name, req.headers.get("Range")))
        body = files[name]
        rng = req.headers.get("Range")
        if rng:
            start = int(rng.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, content=body[start:])
        return httpx.Response(200, content=body)

    return make_client(httpx.MockTransport(handler))


def sums_for(files: dict[str, bytes]) -> bytes:
    return "".join(f"{hashlib.sha256(b).hexdigest()}  {n}\n" for n, b in files.items()).encode()


def test_download_verifies_and_skips_complete(tmp_path):
    parts = {"p0": b"a" * 100, "p1": b"b" * 50}
    files = {**parts, ka.SUMS: sums_for(parts)}
    seen: list = []
    client = serve(files, seen)
    paths = ka.download_archive(client, tmp_path, list(parts))
    assert [p.read_bytes() for p in paths] == list(parts.values())
    assert seen[0][0] == ka.SUMS
    seen.clear()
    ka.download_archive(client, tmp_path, list(parts))
    assert [n for n, _ in seen] == [ka.SUMS]  # parts not re-requested


def test_checksum_mismatch_raises(tmp_path):
    parts = {"p0": b"a" * 100}
    files = {"p0": b"x" * 100, ka.SUMS: sums_for(parts)}
    with pytest.raises(ka.ChecksumError):
        ka.download_archive(serve(files, []), tmp_path, ["p0"])


def test_download_resumes_with_range(tmp_path):
    parts = {"p0": bytes(range(200))}
    files = {**parts, ka.SUMS: sums_for(parts)}
    (tmp_path / "p0").write_bytes(parts["p0"][:60])
    seen: list = []
    ka.download_archive(serve(files, seen), tmp_path, ["p0"])
    assert ("p0", "bytes=60-") in seen
    assert (tmp_path / "p0").read_bytes() == parts["p0"]
