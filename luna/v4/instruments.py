"""Freeze-time instrument metadata snapshot (spec §3 minimum order, §7.1) -> ``config/v4_instruments.json``.

Reads ONLY the collector's raw ``fut_instruments`` and ``spot_assetpairs`` envelopes (exchange
metadata, not outcomes): the earliest ok envelope of each received at or after ``collector_start_ts``.
Per eligible asset:
- perp: quantity step = 10^-contractValueTradePrecision (also the minimum order size; PF_ contracts
  have no separate minimum), tick = tickSize; contractSize must be 1 (linear);
- spot: step = 10^-lot_decimals, min_qty = ordermin, min_cost = costmin, tick = tick_size.
A missing asset or field fails loudly; nothing is guessed.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from luna.forward.dq import _spot_key_pair
from luna.instruments import perp_symbol, spot_pair
from luna.v4.constants import V4_UNIVERSE
from luna.v4.data import collector_start, iter_forward

DEFAULT_ASSETS = V4_UNIVERSE


def _earliest(root: Path, source: str) -> dict:
    best = None
    for env in iter_forward(
        root, source, collector_start(root), pd.Timestamp.now(tz="UTC") + pd.Timedelta("1D")
    ):
        if (
            env.get("ok")
            and env.get("payload") is not None
            and (best is None or env["_recv_ns"] < best["_recv_ns"])
        ):
            best = env
    if best is None:
        raise ValueError(f"no ok {source} envelope at or after collector_start_ts")
    return best


def _num(d: dict, key: str, where: str) -> float:
    if key not in d or d[key] is None:
        raise ValueError(f"{where}: field {key} missing")
    try:
        v = float(d[key])
    except (TypeError, ValueError) as e:
        raise ValueError(f"{where}: field {key} not numeric ({d[key]!r})") from e
    if not math.isfinite(v):
        raise ValueError(f"{where}: field {key} not finite")
    return v


def _perp(payload: dict, asset: str) -> dict[str, float]:
    sym = perp_symbol(asset)
    row = next((i for i in payload.get("instruments") or [] if i.get("symbol") == sym), None)
    if row is None:
        raise ValueError(f"{asset}: {sym} not in fut_instruments")
    where = f"{asset} perp {sym}"
    if _num(row, "contractSize", where) != 1:
        raise ValueError(f"{where}: contractSize {row['contractSize']} != 1 (not a linear PF_ contract)")
    step = 10.0 ** -int(_num(row, "contractValueTradePrecision", where))
    return {"step": step, "min_qty": step, "min_cost": 0.0, "tick": _num(row, "tickSize", where)}


def _spot(payload: dict, asset: str) -> dict[str, float]:
    want = spot_pair(asset)
    rows = (payload.get("result") or {}) if isinstance(payload, dict) else {}
    row = next(
        (
            v
            for k, v in rows.items()
            if isinstance(v, dict) and (v.get("altname") == want or _spot_key_pair(k) == want)
        ),
        None,
    )
    if row is None:
        raise ValueError(f"{asset}: {want} not in spot_assetpairs")
    where = f"{asset} spot {want}"
    return {
        "step": 10.0 ** -int(_num(row, "lot_decimals", where)),
        "min_qty": _num(row, "ordermin", where),
        "min_cost": _num(row, "costmin", where),
        "tick": _num(row, "tick_size", where),
    }


def build_snapshot(root: Path, assets: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    assets = tuple(assets if assets is not None else DEFAULT_ASSETS)
    fut, spot = _earliest(root, "fut_instruments"), _earliest(root, "spot_assetpairs")
    return {
        "spec": "luna-research-v4-design.md §3 / §7.1 (freeze-time instrument metadata)",
        "universe": list(assets),
        "source": {
            s: {"recv_ts": e["recv_ts"], "seq": e.get("seq")}
            for s, e in (("fut_instruments", fut), ("spot_assetpairs", spot))
        },
        "assets": {a: {"perp": _perp(fut["payload"], a), "spot": _spot(spot["payload"], a)} for a in assets},
    }


def write_snapshot(snapshot: dict, out: Path) -> str:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(snapshot, indent=1, sort_keys=True) + "\n").encode("utf-8")
    out.write_bytes(data)
    return hashlib.sha256(data).hexdigest()
