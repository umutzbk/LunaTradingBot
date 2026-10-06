"""Research trade lists: every (strategy, params, key) traded alone, equivalent to the engine.

A trade list is what the backtest engine (``luna.engine.backtest.run_backtest``) would trade for one
strategy instance on one key when no portfolio limit ever binds: one position at a time, entry
decided at the signal bar's close and filled on the next bar, exits decided at the close and filled
on the next bar, stops evaluated at the close exactly as the strategy's ``should_exit``, forward-
filled / missing bars never fill, perp IOC (taker) fills within the price band, spot post-only
(maker) fills, spot shorts dropped. The exact life cycle lives in ``luna.research.kernel``.

Deliberately NOT modelled: portfolio constraints (max positions, open risk, correlation buckets,
leverage, minimum notional), risk-engine vetoes (daily loss, drawdown kill switch, orders per day)
and emergency stops (§6.2: they depend on account equity, not on the strategy).

Per-unit convention: one unit is 1 USD of gross notional at the signal bar's close (the mids the
engine sizes the legs with; legs split by ``|w| / sum|w|``). Columns (fractions of that unit):

- ``ret_gross``  frictionless P&L between the entry and exit fill bars' opens
                 (= sum_l w_l (open_exit / open_entry - 1) / sum|w| when the entry bar opens at the
                 signal close);
- ``cost``       round-trip fees + half spread + slippage of all legs (~ 2 (fee + half_spread +
                 slippage) |w| / sum|w| per leg; exact to the engine's fill prices);
- ``funding``    perp funding at the known hourly rates while held (0 on spot);
- ``funding_unknown_bars``  hours (per held leg) whose rate is unknown; they add 0;
- ``ret_net``    ret_gross - cost + funding  (== engine pnl_net / gross notional at signal mids);
- ``r_net``      ret_net / stop_distance (stop distance of the candidate at the signal bar).

``signal_ts`` is the decision bar, ``entry_ts`` / ``exit_ts`` the fill bars (as the engine's trades),
``regime`` the regime label at the signal bar, ``day`` the UTC date of ``exit_ts``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from luna.engine.backtest import _Funding, _Market
from luna.execution.costs import CostModel
from luna.research.kernel import (
    REASONS,
    STOP_PRICE,
    STOP_TRAIL,
    STOP_Z_ABS,
    STOP_Z_DIR,
    simulate,
)
from luna.strategy.base import SignalFrame, SignalStrategy
from luna.strategy.mean_reversion import STOP_DZ, MeanReversion
from luna.strategy.momentum import Momentum
from luna.strategy.naive import NaiveMeanReversion, NaiveMomentum
from luna.types import AccountKind

_KEY_META = b"luna_cache_key"
CODE_VERSION = "tradelists-v1"  # bump when trade-list semantics change (invalidates caches)

TRADE_COLUMNS = [
    "strategy", "params_id", "key", "regime", "direction", "signal_ts", "entry_ts", "exit_ts",
    "exit_reason", "ret_gross", "cost", "funding", "funding_unknown_bars", "ret_net",
    "stop_distance", "r_net", "day",
]


class ResearchMarket:
    """The engine's view of the data (union bar index, last closes, real-bar mask, hourly funding
    rates), built once and shared by every trade list over the same panel."""

    def __init__(self, panel: dict[str, pd.DataFrame], funding: dict[str, pd.DataFrame],
                 kind: AccountKind) -> None:
        idx = pd.DatetimeIndex([])
        for df in panel.values():
            idx = idx.union(df.index)
        self.idx = pd.DatetimeIndex(idx).sort_values()
        self.kind = kind
        self.mkt = _Market(panel, self.idx)
        n = len(self.idx)
        if kind == "perp":
            fund = _Funding(funding, self.mkt, "known_only")
            self.boundary = fund.boundary
            self.rate = fund.rate
        else:
            self.boundary = np.zeros(n, dtype=bool)
            self.rate = np.full((n, len(self.mkt.assets)), np.nan)

    def cols(self, assets: list[str]) -> list[int]:
        return [self.mkt.col[a] for a in assets]


# --- exit rules -----------------------------------------------------------------------------------


def _exit_rule(strategy: object, sf: SignalFrame) -> tuple[int, float, float]:
    """(stop_mode, stop_z, trail_mult) replicating ``strategy.should_exit`` in the kernel.

    Matched on the implementing functions (not just the class), so a subclass that overrides any
    exit hook is rejected rather than silently simulated with the parent's rule."""
    cls = type(strategy)
    if getattr(cls, "should_exit", None) is not SignalStrategy.should_exit:
        raise TypeError(f"{cls.__name__}: no trade-list exit rule for a custom should_exit")
    stop_exit, stop_hit, stop = cls._stop_exit, getattr(cls, "_stop_hit", None), cls._stop
    k = sf.exit_spec.trailing_atr_mult
    if stop_exit is Momentum._stop_exit and stop is Momentum._stop:
        return STOP_TRAIL, math.nan, math.nan if k is None else float(k)
    if stop_exit is SignalStrategy._stop_exit:
        if stop_hit is NaiveMomentum._stop_hit and stop is NaiveMomentum._stop:
            return STOP_PRICE, math.nan, math.nan
        if stop_hit is NaiveMeanReversion._stop_hit and stop is NaiveMeanReversion._stop:
            return STOP_Z_ABS, float(sf.stop_z), math.nan
        if stop_hit is MeanReversion._stop_hit and stop is MeanReversion._stop:
            return STOP_Z_DIR, STOP_DZ, math.nan
    raise TypeError(f"{cls.__name__}: no trade-list exit rule")


def params_id(strategy: object) -> str:
    """The strategy's ``params_id`` (grid strategies), else ``k=v`` pairs of its params."""
    pid = getattr(strategy, "params_id", None)
    if isinstance(pid, str):
        return pid
    return "_".join(f"{k}{v}" for k, v in sorted(strategy.params.items())) or "default"


# --- one trade list -------------------------------------------------------------------------------


def _on_union(series: pd.Series | None, rows: np.ndarray, ok: np.ndarray, fill, dtype) -> np.ndarray:
    out = np.full(len(rows), fill, dtype=dtype)
    if series is not None:
        out[ok] = series.to_numpy(dtype=dtype)[rows[ok]]
    return out


def build_tradelist(
    sf: SignalFrame,
    panel: dict[str, pd.DataFrame],
    funding: dict[str, pd.DataFrame],
    costs: CostModel,
    kind: AccountKind,
    *,
    strategy: object,
    band: float,
    market: ResearchMarket | None = None,
) -> pd.DataFrame:
    """Trade list of ``strategy`` on ``sf.key`` (module docstring). ``band`` is the account's
    ``max_price_band`` (``settings.accounts[kind].max_price_band``; the engine cancels perp IOC
    fills beyond it, and it is required so callers cannot silently diverge from the engine);
    ``market`` reuses a prepared ``ResearchMarket`` over the same panel/funding/kind. Pairs on a
    spot account have no trades (a short leg is never allowed)."""
    if kind == "spot" and sf.legs_beta is not None:
        return _empty()
    mk = market or ResearchMarket(panel, funding, kind)
    mkt = mk.mkt
    idx = mk.idx
    n = len(idx)
    pairs = sf.legs_beta is not None
    assets = sf.key.split("/") if pairs else [sf.key]
    cols = mk.cols(assets)
    rows = sf.entry.index.get_indexer(idx)
    ok = rows >= 0

    ref = _on_union(sf.ref, rows, ok, np.nan, float)
    dist = _on_union(sf.stop_distance, rows, ok, np.nan, float)
    z = _on_union(sf.z, rows, ok, np.nan, float)
    atr = _on_union(sf.atr, rows, ok, np.nan, float)
    if pairs:
        beta = _on_union(sf.legs_beta, rows, ok, np.nan, float)
        weight = np.column_stack([np.ones(n), -beta])
    else:
        weight = np.ones((n, 1))

    # entries the engine would submit: candidate_at (finite features), legs on real bars,
    # ranker/constructor (stop distance > 0), spot long-only
    d = _on_union(sf.entry, rows, ok, 0, np.int8)
    valid = (d != 0) & ok
    feats = [ref, dist] + [
        _on_union(s, rows, ok, np.nan, float)
        for s in (sf.z, sf.atr, sf.legs_beta, *sf.features.values()) if s is not None
    ]
    for f in feats:
        valid &= np.isfinite(f)
    valid &= dist > 0
    real = mkt.real[:, cols]
    valid &= real.all(axis=1)
    if kind == "spot":
        valid &= (d[:, None] * weight >= 0).all(axis=1)
    entry = np.where(valid, d, 0).astype(np.int8)

    mode, stop_z, trail_mult = _exit_rule(strategy, sf)
    perp = kind == "perp"
    fric = np.array([costs.half_spread(a) + costs.slippage for a in assets])
    half_spread = np.array([costs.half_spread(a) for a in assets])
    fee = costs.fee_rate("taker" if perp else "maker")

    out = simulate(
        np.ascontiguousarray(mkt.open[:, cols]), np.ascontiguousarray(mkt.high[:, cols]),
        np.ascontiguousarray(mkt.low[:, cols]), np.ascontiguousarray(mkt.last[:, cols]),
        np.ascontiguousarray(real), entry, np.ascontiguousarray(weight), ok,
        _on_union(sf.exit_long, rows, ok, False, bool), _on_union(sf.exit_short, rows, ok, False, bool),
        ref, dist, z, atr, mode, stop_z, trail_mult, int(sf.exit_spec.max_hold_bars), not perp,
        float(band), fric, fee, half_spread, mk.boundary, np.ascontiguousarray(mk.rate[:, cols]),
    )
    sig, ent, ext, dirn, rsn, gross, cost, fund, unk = out
    regime = sf.regime.to_numpy(dtype=object)[rows[sig]] if len(sig) else np.array([], dtype=object)
    stop_distance = dist[sig]
    ret_net = gross - cost + fund
    exit_ts = idx[ext]
    return pd.DataFrame({
        "strategy": strategy.name,
        "params_id": params_id(strategy),
        "key": sf.key,
        "regime": regime.astype(str),
        "direction": dirn.astype("int64"),
        "signal_ts": idx[sig],
        "entry_ts": idx[ent],
        "exit_ts": exit_ts,
        "exit_reason": np.array(REASONS, dtype=object)[rsn],
        "ret_gross": gross,
        "cost": cost,
        "funding": fund,
        "funding_unknown_bars": unk.astype("int64"),
        "ret_net": ret_net,
        "stop_distance": stop_distance,
        "r_net": ret_net / stop_distance,
        "day": exit_ts.date,
    }, columns=TRADE_COLUMNS)


# --- all grids + cache ----------------------------------------------------------------------------


def _instances(cls: type, use_regime: bool) -> list:
    grid = getattr(cls, "GRID", None)
    if grid:
        return [cls(dict(g), use_regime=use_regime) for g in grid]
    return [cls()]


_SHARED_SOURCES = (
    "luna/research/kernel.py", "luna/research/tradelists.py", "luna/strategy/base.py",
    "luna/regime/rules.py", "luna/engine/backtest.py",
)
_ROOT = Path(__file__).resolve().parents[2]


def _read_source(path: Path) -> bytes:
    return path.read_bytes()


def _source_files(strategies: dict[str, type]) -> list[Path]:
    files = {_ROOT / f for f in _SHARED_SOURCES}
    files |= set((_ROOT / "luna" / "features").glob("*.py"))
    for cls in strategies.values():  # defining module of each strategy class and its bases
        for base in cls.__mro__:
            mod = sys.modules.get(base.__module__)
            src = getattr(mod, "__file__", None)
            if src and Path(src).resolve().is_relative_to(_ROOT / "luna"):
                files.add(Path(src).resolve())
    return sorted(files)


def _code_hash(strategies: dict[str, type]) -> str:
    """Hash of the source text of every module whose code shapes the trade lists."""
    h = hashlib.sha256()
    for f in _source_files(strategies):
        h.update(f.relative_to(_ROOT).as_posix().encode())
        h.update(_read_source(f))
    return h.hexdigest()


def _data_hash(panel: dict[str, pd.DataFrame], funding: dict[str, pd.DataFrame]) -> str:
    h = hashlib.sha256()
    for name, frames in (("bars", panel), ("funding", funding)):
        for a in sorted(frames):
            df = frames[a]
            h.update(f"{name}:{a}:{len(df)}:{list(df.columns)}".encode())
            h.update(pd.util.hash_pandas_object(df, index=True).to_numpy().tobytes())
    return h.hexdigest()


def cache_key(strategies: dict[str, type], panel, funding, costs: CostModel, kind: AccountKind,
              keys: dict[str, list[str]], band: float, use_regime: bool) -> str:
    spec = {
        "code": CODE_VERSION,
        "source": _code_hash(strategies),
        "kind": kind,
        "band": repr(float(band)),
        "use_regime": use_regime,
        "costs": costs.cfg.model_dump(),
        "strategies": {
            name: {"cls": f"{cls.__module__}.{cls.__qualname__}",
                   "grid": [p.params for p in _instances(cls, use_regime)],
                   "keys": sorted(keys.get(name, []))}
            for name, cls in sorted(strategies.items())
        },
        "data": _data_hash(panel, funding),
    }
    return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()


def build_all_tradelists(
    strategies: dict[str, type],
    panel: dict[str, pd.DataFrame],
    funding: dict[str, pd.DataFrame],
    costs: CostModel,
    kind: AccountKind,
    keys: dict[str, list[str]],
    *,
    band: float,
    use_regime: bool = True,
    cache_dir: Path | None = Path("var/research"),
) -> pd.DataFrame:
    """Trade lists of every grid point (or the single default instance of a non-grid strategy)
    on every key of ``keys[name]``; ``strategies`` maps each class's ``name`` to the class.
    Spot skips pair keys ("A/B"; they need a short leg). ``band`` as in ``build_tradelist``.

    Cached to ``cache_dir/tradelists_<kind>_<key[:16]>.parquet``, the full key stored in the
    parquet metadata and checked on read. The key hashes the data content (bars + funding), the
    source text of the modules that shape the trades (kernel, this module, the strategies' modules,
    strategy base, regime, features, engine), ``CODE_VERSION``, the grids, keys, costs, band and
    ``use_regime``. Writes go to a temporary file renamed into place, so an interrupted write never
    leaves a file that passes the check. ``cache_dir=None`` disables the cache.
    """
    for name, cls in strategies.items():
        if getattr(cls, "name", None) != name:
            raise ValueError(f"strategies key {name!r} != {cls.__name__}.name {getattr(cls, 'name', None)!r}")
    key = cache_key(strategies, panel, funding, costs, kind, keys, band, use_regime)
    path = None
    if cache_dir is not None:
        path = Path(cache_dir) / f"tradelists_{kind}_{key[:16]}.parquet"
        if path.exists() and _stored_key(path) == key:
            return pd.read_parquet(path)

    market = ResearchMarket(panel, funding, kind)
    frames = []
    for name, cls in strategies.items():
        ks = [k for k in keys.get(name, []) if not (kind == "spot" and "/" in k)]
        if not ks:
            continue
        for strat in _instances(cls, use_regime):
            for sf in strat.signals(panel, ks).values():
                frames.append(build_tradelist(sf, panel, funding, costs, kind, strategy=strat,
                                              band=band, market=market))
    frames = [f for f in frames if len(f)]
    out = pd.concat(frames, ignore_index=True) if frames else _empty()
    if path is not None:
        _write_cache(out, path, key)
    return out


def _stored_key(path: Path) -> str | None:
    try:
        meta = pq.read_schema(path).metadata or {}
    except (OSError, pa.ArrowInvalid):
        return None
    raw = meta.get(_KEY_META)
    return raw.decode() if raw is not None else None


def _write_cache(df: pd.DataFrame, path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    table = table.replace_schema_metadata({**(table.schema.metadata or {}), _KEY_META: key.encode()})
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    pq.write_table(table, tmp)
    os.replace(tmp, path)


def _empty() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=object) for c in TRADE_COLUMNS})
