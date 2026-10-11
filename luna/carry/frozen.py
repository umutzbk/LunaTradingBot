"""Frozen historical input copy for the V5 backtest and parity (protocol §2.2, R2).

Every parity / backtest input is read from one sha-pinned copy cut before the sealed holdout
(2026-07-01T00:00Z), never from the live ``var/bars`` files the paper bots append to.

- Live bar files are read with a parquet filter, so holdout rows are never materialised; bars are
  labelled by open_ts and kept only when they close by the cut.
- ``history_v4`` / ``history_v5`` series are already cut by their downloaders: a row at or after the
  cut raises ``HoldoutLeak`` instead of being dropped.
- The copy keeps the V4 directory layout (``bars/<venue>/<asset>.parquet``,
  ``history_v4/<kind>/<asset>.parquet``), so the frozen V4 loaders can point at it unchanged.
- ``manifest.json`` lists every file (sha256, rows, first / last ts); ``verify_frozen`` re-hashes
  every file, refuses unlisted or missing files and re-checks the cut on the data itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from luna.v2.data import HoldoutLeak

CUTOFF = pd.Timestamp("2026-07-01T00:00:00Z")
BAR = pd.Timedelta("15min")
BAR_VENUES = ("kraken_futures", "kraken_spot")
HISTORY_BAR_KINDS = ("mark", "index")
HISTORY_SERIES_KINDS = ("funding", "spreads")
INSTRUMENTS_REL = "config/v4_instruments.json"
MANIFEST = "manifest.json"


class FrozenInputError(RuntimeError):
    """The frozen copy does not match its manifest."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_cutoff(cutoff: pd.Timestamp) -> None:
    if cutoff > CUTOFF:
        raise HoldoutLeak(f"cutoff {cutoff} is after the sealed holdout start {CUTOFF}")


def _utc(df: pd.DataFrame) -> pd.DataFrame:
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.sort_index()


def _index_column(path: Path) -> str:
    cols = pq.read_schema(path).pandas_metadata.get("index_columns") or []
    return cols[0] if cols and isinstance(cols[0], str) else "__index_level_0__"


def _read_bars_before(path: Path, cutoff: pd.Timestamp) -> tuple[pd.DataFrame, int]:
    """Bars closing by ``cutoff`` (open_ts <= cutoff - 15m), filtered inside the parquet reader.
    Also returns how many rows the file holds past that bound (counted from metadata-free row
    totals, never materialised)."""
    total = pq.ParquetFile(path).metadata.num_rows
    df = _utc(pd.read_parquet(path, filters=[(_index_column(path), "<=", cutoff - BAR)]))
    return df, total - len(df)


def assert_bars_before(df: pd.DataFrame, name: str, cutoff: pd.Timestamp = CUTOFF) -> pd.DataFrame:
    if len(df) and df.index.max() > cutoff - BAR:
        raise HoldoutLeak(f"{name}: bar {df.index.max()} closes after {cutoff}")
    return df


def assert_series_before(df: pd.DataFrame, name: str, cutoff: pd.Timestamp = CUTOFF) -> pd.DataFrame:
    if len(df) and df.index.max() >= cutoff:
        raise HoldoutLeak(f"{name}: row {df.index.max()} at or after {cutoff}")
    return df


def _make_read_only(path: Path) -> None:
    os.chmod(path, stat.S_IREAD)


def _write_frame(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, path)
    _make_read_only(path)


def _entry(path: Path, df: pd.DataFrame, source: Path, dropped: int, kind: str) -> dict:
    return {
        "kind": kind,
        "source": str(source),
        "rows": int(len(df)),
        "first": df.index.min().isoformat() if len(df) else None,
        "last": df.index.max().isoformat() if len(df) else None,
        "rows_dropped_at_or_after_cutoff": int(dropped),
        "sha256": sha256_file(path),
    }


def _bar_kind(rel: str) -> bool:
    parts = rel.split("/")
    return parts[0] == "bars" or (parts[0] == "history_v4" and parts[1] in HISTORY_BAR_KINDS)


def freeze_inputs(
    var_dir: Path,
    instruments: Path,
    dst: Path,
    *,
    assets: list[str] | tuple[str, ...],
    cutoff: pd.Timestamp = CUTOFF,
    history_v5_spreads: Path | None = None,
) -> dict:
    """Copy every input the parity / backtest reads into ``dst`` (which must not exist), cut before
    ``cutoff``, read-only, with a manifest. Returns the manifest."""
    _check_cutoff(cutoff)
    var_dir, dst = Path(var_dir), Path(dst)
    if dst.exists():
        raise FileExistsError(f"{dst} exists: a frozen copy is never overwritten")
    stage = dst.with_name(dst.name + ".partial")
    if stage.exists():
        shutil.rmtree(stage, onerror=lambda f, p, e: (os.chmod(p, stat.S_IWRITE), f(p)))
    files: dict[str, dict] = {}
    missing: list[str] = []

    for venue in BAR_VENUES:
        for a in assets:
            rel = f"bars/{venue}/{a}.parquet"
            src = var_dir / rel
            if not src.exists():
                missing.append(rel)
                continue
            df, dropped = _read_bars_before(src, cutoff)
            assert_bars_before(df, rel, cutoff)
            _write_frame(df, stage / rel)
            files[rel] = _entry(stage / rel, df, src, dropped, "bars")

    for kind in (*HISTORY_BAR_KINDS, *HISTORY_SERIES_KINDS):
        for a in assets:
            rel = f"history_v4/{kind}/{a}.parquet"
            src = var_dir / rel
            if not src.exists():
                missing.append(rel)
                continue
            df = _utc(pd.read_parquet(src))
            if kind in HISTORY_BAR_KINDS:
                assert_bars_before(df, rel, cutoff)
            else:
                assert_series_before(df, rel, cutoff)
            _write_frame(df, stage / rel)
            files[rel] = _entry(stage / rel, df, src, 0, kind)

    if history_v5_spreads is not None:
        for a in assets:
            rel = f"history_v5/spreads/{a}.parquet"
            src = Path(history_v5_spreads) / f"{a}.parquet"
            if not src.exists():
                missing.append(rel)
                continue
            df = assert_series_before(_utc(pd.read_parquet(src)), rel, cutoff)
            _write_frame(df, stage / rel)
            files[rel] = _entry(stage / rel, df, src, 0, "spreads_v5")

    inst_dst = stage / INSTRUMENTS_REL
    inst_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(instruments, inst_dst)
    _make_read_only(inst_dst)
    files[INSTRUMENTS_REL] = {
        "kind": "instruments",
        "source": str(instruments),
        "sha256": sha256_file(inst_dst),
    }

    manifest = {
        "cutoff": cutoff.isoformat(),
        "assets": list(assets),
        "created_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "missing_sources": missing,
        "files": dict(sorted(files.items())),
    }
    man_path = stage / MANIFEST
    man_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    _make_read_only(man_path)
    os.replace(stage, dst)
    return manifest


def _listed(dst: Path) -> set[str]:
    return {
        p.relative_to(dst).as_posix()
        for p in dst.rglob("*")
        if p.is_file() and p.name != MANIFEST
    }


def verify_frozen(dst: Path, expected_manifest_sha256: str | None = None) -> dict:
    """Re-hash every file against the manifest and re-check the cut on the data. Raises
    ``FrozenInputError`` (mismatch) or ``HoldoutLeak`` (a row at or after the cut)."""
    dst = Path(dst)
    man_path = dst / MANIFEST
    if not man_path.exists():
        raise FrozenInputError(f"{man_path} missing")
    if expected_manifest_sha256 is not None and sha256_file(man_path) != expected_manifest_sha256:
        raise FrozenInputError("manifest sha256 differs from the pinned value")
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    cutoff = pd.Timestamp(manifest["cutoff"])
    _check_cutoff(cutoff)
    files = manifest["files"]
    extra = sorted(_listed(dst) - set(files))
    if extra:
        raise FrozenInputError(f"files not in the manifest: {extra}")
    for rel, e in files.items():
        p = dst / rel
        if not p.exists():
            raise FrozenInputError(f"{rel} missing")
        if sha256_file(p) != e["sha256"]:
            raise FrozenInputError(f"{rel}: sha256 differs from the manifest")
        if rel.endswith(".parquet"):
            df = _utc(pd.read_parquet(p))
            if _bar_kind(rel):
                assert_bars_before(df, rel, cutoff)
            else:
                assert_series_before(df, rel, cutoff)
            if len(df) != e["rows"]:
                raise FrozenInputError(f"{rel}: {len(df)} rows, manifest says {e['rows']}")
    return manifest


def open_frozen(dst: Path, expected_manifest_sha256: str | None = None) -> Path:
    """The only way parity / backtest code gets the frozen root: verified first."""
    verify_frozen(dst, expected_manifest_sha256)
    return Path(dst)
