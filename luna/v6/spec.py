"""The frozen V6 constants (spec §0: a constant that contradicts the spec voids the freeze).

The constants file is read only when its sha256 equals the value pinned at the spec freeze, so a
changed number cannot reach any V6 code path silently."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "docs" / "superpowers" / "specs" / "luna-v6-profitability-spec.md"
CONSTANTS_PATH = ROOT / "docs" / "superpowers" / "specs" / "luna-v6-frozen-constants.json"
SPEC_SHA256 = "c659d0cd6928c6356be43cbe97eaa91075197640ef195d09aba1ac5d582b2c5f"
CONSTANTS_SHA256 = "152af4cb19e688b3551feb8210aeec5c471aec78b708592fcbb6eeef08946974"
SPEC_TAG = "v6-profitability-spec-frozen"


class SpecMismatch(RuntimeError):
    pass


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_spec(path: Path = SPEC_PATH) -> str:
    h = _sha(path)
    if h != SPEC_SHA256:
        raise SpecMismatch(f"{path}: sha256 {h} is not the frozen {SPEC_SHA256}")
    return h


@lru_cache(maxsize=4)
def _load(path: str) -> dict:
    p = Path(path)
    h = _sha(p)
    if h != CONSTANTS_SHA256:
        raise SpecMismatch(f"{p}: sha256 {h} is not the frozen {CONSTANTS_SHA256}")
    return json.loads(p.read_text(encoding="utf-8"))


def constants(path: Path | None = None) -> dict:
    """The frozen constants (a deep copy: callers cannot change the cached values)."""
    return json.loads(json.dumps(_load(str(path or CONSTANTS_PATH))))
