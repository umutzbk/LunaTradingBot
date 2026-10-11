"""V4 H2-L30-k2 reference reproduction (V5 backtest protocol §5.1). Runs INSIDE a detached worktree of
the frozen V4 Stage 1 code (b074b69) with that worktree first on PYTHONPATH; V4 code is imported, never
modified. Inputs come only from the frozen copy (verified by the V5 caller before this runs).

Read-only instrumentation: ``_Sim.fill`` / ``_Sim.valuation_prices`` / ``_Sim.margin_check`` /
``_Sim.liquidation_check`` / ``accounting._rebalance`` are wrapped to log every fill with its source
(rebalance, margin, liquidation, delisting) and every day-end valuation price. Wrappers call the
original and return its value unchanged.

Usage: python v4_reference_harness.py <frozen_root> <out_dir>
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

import luna
from luna.v4 import accounting, stats
from luna.v4.accounting import LegMeta, run_h2
from luna.v4.costs import cost_rows
from luna.v4.data import load_historical
from luna.v4.runner import stage1_inputs
from luna.v4.strategies import CONFIGS, H2Rule

CID = "H2-L30-k2"
_ctx: list[str] = []
FILLS: list[dict] = []
VALUATIONS: list[dict] = []


def _with_ctx(name, fn):
    def wrapped(*a, **k):
        _ctx.append(name)
        try:
            return fn(*a, **k)
        finally:
            _ctx.pop()
    return wrapped


_orig_fill = accounting._Sim.fill
_orig_val = accounting._Sim.valuation_prices


def _fill(self, asset, leg, dq, px, ts, obs, penalty=0.0, other_px=None):
    half = self.half(asset, leg, obs)
    q0 = self.book.perp_q.get(asset, 0.0)
    out = _orig_fill(self, asset, leg, dq, px, ts, obs, penalty, other_px)
    FILLS.append({
        "row": self.row.name, "source": _ctx[-1] if _ctx else "delisting", "ts": ts.isoformat(),
        "asset": asset, "leg": leg, "dq": float(dq), "px": float(px), "obs": None if obs is None else float(obs),
        "half_spread": float(half), "fee_rate": float(self.row.fee(leg)), "penalty": float(penalty),
        "q_before": float(q0), "q_after": float(self.book.perp_q.get(asset, 0.0)),
    })
    return out


def _valuation(self, day):
    out = _orig_val(self, day)
    VALUATIONS.append({"row": self.row.name, "day": day.isoformat(), "perp": {a: float(v) for a, v in out[1].items()}})
    return out


def install():
    accounting._Sim.fill = _fill
    accounting._Sim.valuation_prices = _valuation
    accounting._Sim.margin_check = _with_ctx("margin", accounting._Sim.margin_check)
    accounting._Sim.liquidation_check = _with_ctx("liquidation", accounting._Sim.liquidation_check)
    accounting._rebalance = _with_ctx("rebalance", accounting._rebalance)


def main(frozen: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    raw = (frozen / "config" / "v4_instruments.json").read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    data = json.loads(raw)
    meta = {a: {leg: LegMeta(**v) for leg, v in legs.items()} for a, legs in (data.get("assets") or {}).items()}
    install()
    inp = stage1_inputs(load_historical(frozen), meta, instruments_sha256=sha)
    cfg = CONFIGS[CID]
    rows = cost_rows()
    res = {}
    for name in ("PRIMARY", "S-COST2X"):
        res[name] = run_h2(inp.market, H2Rule(cfg, inp.table), inp.days, rows[name], inp.meta)
        res[name].daily.to_csv(out / f"{CID}__{name}.daily.csv")
        pd.DataFrame(res[name].trades).to_csv(out / f"{CID}__{name}.trades.csv", index=False)
    p = res["PRIMARY"]
    d = p.daily
    s1 = stats.stage1_decision("h2", d["ret"], None, d["carry_net"], int(d["active"].sum()))
    pnl_cols = ["spot_price_pnl", "perp_price_pnl", "funding_cf", "fees", "spread_slippage", "liquidation_penalty"]
    summary = {
        "luna_module": luna.__file__,
        "instruments_sha256": sha,
        "window": [inp.days[0].isoformat(), inp.days[-1].isoformat()],
        "days": len(inp.days),
        "stage1": s1,
        "metrics": {
            "mean_daily": float(d["ret"].mean()),
            "sharpe": stats.sharpe(d["ret"]),
            "max_drawdown": stats.max_drawdown(d["ret"]),
            "worst_day": stats.worst_day(d["ret"]),
            "es975": stats.es975(d["ret"]),
            "cost2x_mean_daily": float(res["S-COST2X"].daily["ret"].mean()),
        },
        "rows": {
            name: {
                "final_equity": float(r.daily["equity"].iloc[-1]),
                "mean_daily": float(r.daily["ret"].mean()),
                "sharpe": stats.sharpe(r.daily["ret"]),
                "max_drawdown": stats.max_drawdown(r.daily["ret"]),
                "pnl": {c: float(r.daily[c].sum()) for c in pnl_cols},
                "counters": {k: float(v) for k, v in r.counters.items()},
                "entries": len(r.trades),
            }
            for name, r in res.items()
        },
    }
    (out / "reference_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    (out / "fills.json").write_text(json.dumps(FILLS), encoding="utf-8")
    (out / "valuations.json").write_text(json.dumps(VALUATIONS), encoding="utf-8")
    # signal table at Monday decisions (B3): FL30 + coverage for every asset
    tab = inp.table.reset_index()
    tab = tab[pd.to_datetime(tab["day"]).dt.weekday == 0]
    tab.to_csv(out / "signal_table_mondays.csv", index=False)
    print(json.dumps(summary["metrics"]), file=sys.stderr)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
