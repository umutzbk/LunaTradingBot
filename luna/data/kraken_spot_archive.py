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
SUMS = "OHLCVT_Full_PARTS_SHA256SUMS.txt"  # remote file name (informational only)
REMOTE_SUMS = "OHLCVT_Full_PARTS_SHA256SUMS.remote.txt"
ARCHIVE_VERSION = "Kraken_OHLCVT_Full_2026Q2"
# Hashes verified when the 2026Q2 parts were first downloaded (2026-10-06). Pinned in the repo:
# Kraken later replaced the remote sums file with a newer archive's, so it cannot be trusted.
PINNED_SHA256 = {
    f"{ARCHIVE_VERSION}.zip.part00": "3dcb68a95e2016c5544787ac8ec8269b478c8267b71dec4fc1169e2e83a68b97",
    f"{ARCHIVE_VERSION}.zip.part01": "bc500956623e3317c3b7083189a779ac0c99ff60c0ba36b9b684304a5f8200aa",
    f"{ARCHIVE_VERSION}.zip.part02": "0e328cadfc6038cf3a5c80b1ad169c022ede9940fa9fd6e065624cf2c6cb114f",
    f"{ARCHIVE_VERSION}.zip.part03": "6300b1c97e5d142435bbeb2946c73ee886b0bbc7d2ec92ba39e120d5b5506c37",
    f"{ARCHIVE_VERSION}.zip.part04": "1815279f0470724f187faf3f68392ddc27f644f52cc1bd10cc6ca3de2b7fad00",
}
PARTS = list(PINNED_SHA256)
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


def download_archive(
    client: httpx.Client,
    dest: Path,
    parts: list[str] = PARTS,
    pinned: dict[str, str] = PINNED_SHA256,
) -> list[Path]:
    """Download (or resume) each part into dest and verify it against the PINNED SHA256.

    The pinned hashes are committed in the repo; the remote sums file (Kraken replaces it when
    a newer archive is published) is saved for information only and never used to verify.
    Parts whose local file already matches their pinned hash are not requested again."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    try:  # informational only, separate file; its content is never trusted or consulted
        resp = client.get(ARCHIVE_BASE + SUMS)
        resp.raise_for_status()
        (dest / REMOTE_SUMS).write_bytes(resp.content)
    except httpx.HTTPError:
        pass
    paths = []
    for name in parts:
        expected = pinned.get(name)
        if expected is None:
            raise ChecksumError(f"{name}: no pinned checksum for archive {ARCHIVE_VERSION}")
        path = dest / name
        if not (path.exists() and _sha256(path) == expected):
            try:
                _fetch(client, ARCHIVE_BASE + name, path)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    raise RuntimeError(
                        f"{name}: archive {ARCHIVE_VERSION} is no longer available at "
                        f"{ARCHIVE_BASE + name} (404) and no verified local copy exists"
                    ) from e
                raise
            actual = _sha256(path)
            if actual != expected:
                raise ChecksumError(f"{name}: sha256 {actual} != pinned {expected}")
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
