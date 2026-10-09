"""Backup of closed days to ``LUNA_FORWARD_BACKUP_DIR`` (spec §6).

For every day with a manifest and no receipt: copy its files to ``<target>/forward/raw/...`` and
``<target>/forward/manifest/...`` (tmp -> replace), verify each copy's sha256 against the manifest,
and only then write ``var/forward/backup/date=YYYY-MM-DD.json``. A missing/unreachable target or a
failed verification never stops collection: it is reported (``ok=False`` + reason) and retried.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from luna.forward.envelope import NS, iso_ns, ns_from_iso
from luna.forward.writer import atomic_write_text, replace_with_retry, sha256_file

ENV_VAR = "LUNA_FORWARD_BACKUP_DIR"
OVERDUE_S = 36 * 3600


@dataclass
class BackupResult:
    ok: bool
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    backed_up: list[str] = field(default_factory=list)


def resolve_backup_dir(value: str | os.PathLike | None) -> Path | None:
    """An unset or empty/blank setting means "not configured" (compose passes "" when unset)."""
    if value is None or not str(value).strip():
        return None
    return Path(str(value).strip())


def _copy(src: Path, dst: Path) -> None:
    shutil.copyfile(src, dst)  # contents only: never carry the read-only bit over


def _copy_atomic(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    _copy(src, tmp)
    replace_with_retry(tmp, dst)


def receipt_path(root: Path, day: str) -> Path:
    return Path(root) / "backup" / f"date={day}.json"


def _pending(root: Path) -> list[tuple[str, dict]]:
    out = []
    for mpath in sorted((Path(root) / "manifest").glob("date=*.json")):
        day = mpath.stem.removeprefix("date=")
        if not receipt_path(root, day).exists():
            out.append((day, json.loads(mpath.read_text(encoding="utf-8"))))
    return out


def _backup_day(root: Path, target: Path, day: str, manifest: dict) -> str | None:
    """Copy + verify one day; the failure reason, or None when every copy matches."""
    base = target / "forward"
    for f in manifest["files"]:
        _copy_atomic(root / f["path"], base / f["path"])
    mrel = Path("manifest") / f"date={day}.json"
    _copy_atomic(root / mrel, base / mrel)
    for f in manifest["files"]:
        if sha256_file(base / f["path"]) != f["sha256"]:
            return f"sha256 mismatch for backup copy {f['path']}"
    if sha256_file(base / mrel) != sha256_file(root / mrel):
        return f"sha256 mismatch for backup copy {mrel.as_posix()}"
    return None


def run_backup(root: Path, target: Path | None, now_ns: int) -> BackupResult:
    root = Path(root)
    pending = _pending(root)
    res = BackupResult(ok=True)
    if target is None:
        res.ok, res.reason = False, f"{ENV_VAR} not set: closed days are not backed up"
    elif not Path(target).is_dir():
        res.ok, res.reason = False, f"backup target unreachable: {target}"
    else:
        for day, manifest in pending:
            try:
                err = _backup_day(root, Path(target), day, manifest)
            except OSError as e:
                err = f"copy failed: {type(e).__name__}: {e}"
            if err is not None:
                res.ok, res.reason = False, f"{day}: {err}"
                continue
            receipt = {"date": day, "target": str(target), "verified_ts": iso_ns(now_ns),
                       "files": len(manifest["files"])}
            atomic_write_text(receipt_path(root, day), json.dumps(receipt, indent=1))
            res.backed_up.append(day)
    done = set(res.backed_up)
    for day, manifest in pending:
        if day not in done and now_ns - ns_from_iso(manifest["closed_ts"]) > OVERDUE_S * NS:
            res.ok = False
            res.warnings.append(
                f"backup overdue: {day} (closed {manifest['closed_ts']}) has no receipt after 36h"
            )
    return res
