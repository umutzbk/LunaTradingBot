"""Display only: writes what ``verdict`` decided (spec: classes come from ``verdict.ladder`` /
``holdout_decision`` / ``ForwardFamily`` only; nothing here decides a class). Every file is listed
with its sha256 in ``manifest.json``."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

from luna.v6.declared import DECLARED
from luna.v6.spec import CONSTANTS_SHA256, SPEC_SHA256


def _clean(x):
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (np.floating, float)):
        return None if not math.isfinite(float(x)) else float(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (pd.Timestamp,)):
        return x.isoformat()
    if isinstance(x, np.ndarray):
        return [_clean(v) for v in x.tolist()]
    return x


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _write_excl(p: Path, text: str) -> None:
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def write_holdout(result: dict, out: Path, meta: dict) -> dict:
    """Claims ``out`` exclusively first (``CLAIM``): the holdout report is written once; a crashed
    write is not retried over (the directory stays claimed and the failure is visible)."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        _write_excl(out / "CLAIM", pd.Timestamp.now(tz="UTC").isoformat())
    except FileExistsError:
        raise FileExistsError(f"{out} is already claimed (the holdout report is written once)") from None
    return _summary(result, out, meta)


def _summary(result: dict, out: Path, meta: dict) -> dict:
    d = result["decision"]
    summary = {
        "meta": {**meta, "spec_sha256": SPEC_SHA256, "constants_sha256": CONSTANTS_SHA256},
        "holm": d.holm, "p_go": d.p, "forward_candidates": d.cand, "feasibility_slots": d.feas,
        "verdicts": {c: {"class": v.cls, "label": v.label, "reasons": v.reasons} for c, v in d.verdicts.items()},
        "configs": {}, "declared_interpretations": DECLARED,
        "references": {k: ({kk: vv for kk, vv in v.items() if not isinstance(vv, pd.DataFrame)}
                           if isinstance(v, dict) else v) for k, v in result.get("references", {}).items()},
    }
    tables = {}
    for name, ev in result["evals"].items():
        g = result["gates"][name]
        summary["configs"][name] = {"net": ev.net, "carry": ev.carry, "P": ev.P, "evidence": ev.evidence,
                                    "maker_bound": ev.maker_bound, "stats": {k: v for k, v in (ev.stats or {}).items()},
                                    "gate": {"class": g.cls, "R": g.R, "slope": g.c, "ci_R": g.ci_R, "ci_slope": g.ci_c,
                                             "n_dates": g.n_dates, "imputed": g.n_imputed,
                                             "slope_demeaned": g.c_demeaned,
                                             "bootstrap_replicates_dropped": g.n_boot_dropped}}
        for row, r in result["runs"][name].items():
            for kind in ("daily", "fills", "settlements"):
                df = getattr(r, kind)
                p = out / "tables" / f"{name}_{row}_{kind}.parquet"
                p.parent.mkdir(parents=True, exist_ok=True)
                df.astype({c: str for c in df.columns if df[c].dtype == object}).to_parquet(p)
                tables[f"{name}:{row}:{kind}"] = {"path": str(p.relative_to(out)).replace("\\", "/"), "sha256": _sha(p)}
    for k, v in result.get("references", {}).items():
        if isinstance(v, dict):
            for arm, df in v.items():
                if isinstance(df, pd.DataFrame):
                    p = out / "tables" / f"reference_{k}_{arm}_daily.parquet"
                    df.reset_index().astype(str).to_parquet(p)
                    tables[f"reference:{k}:{arm}"] = {"path": str(p.relative_to(out)).replace("\\", "/"), "sha256": _sha(p)}
    sp = out / "summary.json"
    _write_excl(sp, json.dumps(_clean(summary), indent=1, default=str))
    manifest = {"summary_sha256": _sha(sp), "tables": tables}
    _write_excl(out / "manifest.json", json.dumps(manifest, indent=1))
    return manifest
