"""Guard of the single V5 historical outcome run (frozen protocol rev 3: §12 outcome guard, R1, R9).

``authorize`` is the only producer of the ``OutcomeAuthorization`` that ``backtest.run_arm`` /
``run_study`` / ``parity.run_bridge`` require for the real frozen inputs. It refuses unless:

- the annotated tag ``v5-backtest-protocol-frozen`` exists and is an ancestor of HEAD;
- the working tree is clean;
- the protocol file at HEAD is the one at the tag, with the frozen sha256;
- the frozen input copy verifies against the pinned manifest sha256 (every file re-hashed, no row at
  or after the cut);
- the user's explicit approval phrase is given;
- no outcome run was ever started: exclusive marker files under ``<code repo>/var/ops/v5_backtest`` and
  next to the frozen copy (``var/ops/v5_backtest``), both pinned, never user-chosen (R9: a later
  corrected run is a separate, explicitly approved decision, never automatic);
- the strategy configuration is the committed ``config/default.yaml`` carry section (no environment or
  ``.env`` override) and equals the protocol DEFAULT values (R3, §4, §7); its sha256 is recorded.

The repo checked is the checkout whose code is running (``code_repo``), never a user-supplied path.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pandas as pd

import yaml

import luna
from luna.carry import frozen
from luna.carry.authz import OutcomeAuthorization, OutcomeBlocked
from luna.carry.config import CarryConfig

PROTOCOL = "docs/superpowers/specs/luna-v5-historical-backtest-protocol.md"
TAG = "v5-backtest-protocol-frozen"
PROTOCOL_SHA = "4bd90f5a169025e0483d13c636964a937d01f10fce3355ced1c81058dfcaba54"
FROZEN_MANIFEST_SHA = "eecdc6a21626ccd5e2677cce2503f304a5730845de552554281a6ea54cc53f14"
MARKER = "OUTCOME_RUN_STARTED.json"

# R3 / §4 / §7 DEFAULT profile (the arms' shared parameters) and the carry.risk values the protocol names
FROZEN_DEFAULTS = {
    "capital_usd": 265.0, "k": 2, "gross": 1.0, "signal_hours": 720, "switch_horizon_hours": 168.0,
    "min_carry_improvement_bps": 5.0, "switch_cost_multiplier": 1.5, "safety_buffer_bps": 5.0,
    "min_hold_hours": 24.0, "slippage_bps": 2.0, "max_net_frac": 0.10, "leg_stop_frac": 0.15,
    "stop_cooldown_hours": 24.0,
}
FROZEN_RISK = {"daily_loss_limit": 0.02, "max_drawdown": 0.15, "live_enabled": False}
UNIVERSE = ["ADA", "AVAX", "BNB", "BTC", "DOGE", "ENA", "ETH", "FET", "HYPE", "LINK", "NEAR", "PEPE", "PUMP", "SOL",
            "SUI", "TAO", "UNI", "XLM", "XRP", "ZEC"]


def code_repo() -> Path:
    """The git checkout whose ``luna`` package is running."""
    return Path(luna.__file__).resolve().parents[1]


def marker_paths(repo: Path, frozen_root: Path) -> list[Path]:
    return [Path(repo) / "var" / "ops" / "v5_backtest" / MARKER,
            Path(frozen_root).parent.parent / "ops" / "v5_backtest" / MARKER]


def frozen_carry_config(path: Path | None = None) -> tuple[CarryConfig, str]:
    """The committed YAML carry section only (environment and .env ignored), checked against the
    protocol DEFAULT values; returns the config and the YAML file's sha256."""
    path = Path(path) if path is not None else code_repo() / "config" / "default.yaml"
    raw = path.read_bytes()
    cfg = CarryConfig(**(yaml.safe_load(raw.decode("utf-8")) or {})["carry"])
    bad = {k: (getattr(cfg, k), v) for k, v in FROZEN_DEFAULTS.items() if getattr(cfg, k) != v}
    bad |= {f"risk.{k}": (getattr(cfg.risk, k), v) for k, v in FROZEN_RISK.items() if getattr(cfg.risk, k) != v}
    if list(cfg.universe) != UNIVERSE:
        bad["universe"] = (cfg.universe, UNIVERSE)
    if bad:
        raise OutcomeBlocked(f"outcome run refused: configuration differs from the frozen protocol: {bad}")
    return cfg, hashlib.sha256(raw).hexdigest()


def approval_phrase(protocol_sha: str = PROTOCOL_SHA) -> str:
    return f"I approve the single V5 historical outcome run under protocol {protocol_sha[:16]}"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def preflight(repo: Path, frozen_root: Path, *, protocol_rel: str = PROTOCOL, tag: str = TAG,
              protocol_sha: str = PROTOCOL_SHA, manifest_sha: str = FROZEN_MANIFEST_SHA) -> dict:
    """Every §12 check except the approval and the single-run marker; raises ``OutcomeBlocked``."""
    repo = Path(repo)
    fails = []
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if _git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{tag}").returncode != 0:
        fails.append(f"tag {tag} missing")
    else:
        if _git(repo, "cat-file", "-t", tag).stdout.strip() != "tag":
            fails.append(f"{tag} is not an annotated tag")
        if _git(repo, "merge-base", "--is-ancestor", tag, "HEAD").returncode != 0:
            fails.append(f"{tag} is not an ancestor of HEAD")
        if _git(repo, "diff", "--quiet", tag, "HEAD", "--", protocol_rel).returncode != 0:
            fails.append("protocol differs from the tagged version")
    if _git(repo, "status", "--porcelain").stdout.strip():
        fails.append("working tree not clean")
    blob = subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{protocol_rel}"], capture_output=True)
    got = hashlib.sha256(blob.stdout).hexdigest() if blob.returncode == 0 else None  # committed bytes
    if got != protocol_sha:
        fails.append(f"protocol sha256 {got} != frozen {protocol_sha}")
    try:
        frozen.verify_frozen(Path(frozen_root), manifest_sha)
    except Exception as e:  # noqa: BLE001 - any verification failure blocks the run
        fails.append(f"frozen inputs: {e}")
    if fails:
        raise OutcomeBlocked("outcome run refused: " + "; ".join(fails))
    return {"head": head, "tag": tag, "protocol_sha256": protocol_sha, "manifest_sha256": manifest_sha}


def authorize(repo: Path, frozen_root: Path, approval: str | None, *, config_path: Path | None = None,
              **kw) -> OutcomeAuthorization:
    """Checks, the explicit approval, the frozen configuration, then the exclusive single-run markers
    (created before any run)."""
    psha = kw.get("protocol_sha", PROTOCOL_SHA)
    if approval != approval_phrase(psha):
        raise OutcomeBlocked("outcome run refused: the user's explicit approval phrase was not given")
    markers = marker_paths(repo, frozen_root)
    done = [str(m) for m in markers if m.exists()]
    if done:
        raise OutcomeBlocked(f"outcome run refused: an outcome run was already started ({done}); "
                             "R9: a corrected run is a separate explicit decision")
    rec = preflight(repo, frozen_root, **kw)
    _, cfg_sha = frozen_carry_config(config_path)
    rec = {**rec, "config_sha256": cfg_sha, "approved": True, "started_at": pd.Timestamp.now(tz="UTC").isoformat()}
    created = []
    for m in markers:
        m.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(m, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise OutcomeBlocked(f"outcome run refused: {m} appeared concurrently") from None
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1)
        created.append(m)
    return OutcomeAuthorization(OutcomeAuthorization._secret, rec)
