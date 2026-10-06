"""Kraken spot OHLCVT archive: resumable download, SHA256 verification, 15m extraction."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pandas as pd

from luna.data.store import _empty_bars, _normalize

ARCHIVE_BASE = "https://assets.kraken.com/marketing/institutions/"
PARTS = [f"Kraken_OHLCVT_Full_2026Q2.zip.part0{i}" for i in range(5)]
SUMS = "OHLCVT_Full_PARTS_SHA256SUMS.txt"
CHUNK = 8 * 1024 * 1024
_CSV_COLUMNS = ["time", "open", "high", "low", "close", "volume", "trades"]


class ChecksumError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _parse_sums(text: str) -> dict[str, str]:
    sums = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            sums[fields[-1].lstrip("*")] = fields[0].lower()
    return sums


def _fetch(client: httpx.Client, url: str, path: Path) -> None:
    """Stream url to path, resuming from the existing file size with a Range request."""
    offset = path.stat().st_size if path.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    with client.stream("GET", url, headers=headers) as resp:
        if resp.status_code == 416:  # local file is not a prefix of the remote one
            path.unlink()
            return _fetch(client, url, path)
        resp.raise_for_status()
        mode = "ab" if offset and resp.status_code == 206 else "wb"
        with path.open(mode) as f:
            for chunk in resp.iter_bytes(CHUNK):
                f.write(chunk)


def download_archive(client: httpx.Client, dest: Path, parts: list[str] = PARTS) -> list[Path]:
    """Download (or resume) each part into dest and verify its SHA256; return local paths.

    Parts whose local file already matches their checksum are not requested again."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    sums_path = dest / SUMS
    resp = client.get(ARCHIVE_BASE + SUMS)
    resp.raise_for_status()
    sums_path.write_bytes(resp.content)
    sums = _parse_sums(resp.text)
    paths = []
    for name in parts:
        if name not in sums:
            raise ChecksumError(f"{name}: no checksum listed in {SUMS}")
        path = dest / name
        if not (path.exists() and _sha256(path) == sums[name]):
            _fetch(client, ARCHIVE_BASE + name, path)
            actual = _sha256(path)
            if actual != sums[name]:
                raise ChecksumError(f"{name}: sha256 {actual} != expected {sums[name]}")
        paths.append(path)
    return paths


class MultiPartFile(io.RawIOBase):
    """Seekable read-only view of several files concatenated in order."""

    def __init__(self, paths: list[Path]):
        super().__init__()
        self._files = [Path(p).open("rb") for p in paths]
        self._sizes = [Path(p).stat().st_size for p in paths]
        self._starts = [sum(self._sizes[:i]) for i in range(len(paths))]
        self._size = sum(self._sizes)
        self._pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def readinto(self, b) -> int:
        view = memoryview(b).cast("B")
        want = min(len(view), max(0, self._size - self._pos))
        done = 0
        while done < want:
            i = max(j for j, s in enumerate(self._starts) if s <= self._pos)
            f = self._files[i]
            f.seek(self._pos - self._starts[i])
            n = f.readinto(view[done:want])
            if not n:
                break
            done += n
            self._pos += n
        return done

    def close(self) -> None:
        if not self.closed:
            for f in self._files:
                f.close()
        super().close()


def extract_15m(parts: list[Path], pairs: list[str]) -> dict[str, pd.DataFrame]:
    """15-minute bar frames (filled=False) for the exact pairs found in the archive.

    Only bars that traded are present; gaps are left for the caller to handle."""
    wanted = {f"{p}_15.csv": p for p in pairs}
    out: dict[str, pd.DataFrame] = {}
    with MultiPartFile(parts) as raw, zipfile.ZipFile(io.BufferedReader(raw, 1 << 20)) as zf:
        for info in zf.infolist():
            pair = wanted.get(info.filename.rsplit("/", 1)[-1])
            if pair is None or info.is_dir():
                continue
            with zf.open(info) as f:
                df = pd.read_csv(f, header=None, names=_CSV_COLUMNS)
            if df.empty:
                out[pair] = _empty_bars()
                continue
            df.index = pd.to_datetime(df.pop("time"), unit="s", utc=True)
            df.index.name = None
            df = df.astype({"trades": "int64"})
            out[pair] = _normalize(df[~df.index.duplicated(keep="last")].sort_index())
    return out
