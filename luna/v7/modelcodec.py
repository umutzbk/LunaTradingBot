"""Canonical, non-executable serialisation of a fitted ``cv.FoldModel`` (the §15 frozen forward model; ruling
R54, Task 28 round 2). No pickle, joblib, eval or any code-executing loader (Task 26a: no module unpickles).

* The format is canonical JSON (sorted keys, compact separators, ASCII) of typed values: a float is
  ``{"f": float.hex}`` (exact, NaN / inf included), a tuple ``{"t": [...]}``, a list ``{"l": [...]}``, a numpy
  array ``{"a": dtype, "s": shape, "d": [...]}`` (float64 elements as float.hex, int64 as ints, object /
  str as strings), a dict ``{"m": [[key, value], ...]}`` (string keys, sorted); ints, bools, strings and None
  are JSON scalars. Anything else is refused.
* Estimators: a closed whitelist (``ElasticNet``, ``LogisticRegression``, ``IsotonicRegression``) stored as
  their instance ``__dict__`` (constructor parameters and fitted attributes; the isotonic interpolant ``f_``
  is rebuilt from the thresholds with the estimator's own ``_build_f``), rebuilt with ``cls.__new__`` and the
  attributes set, so ``predict`` runs the identical library code on identical arrays (bit-equal). LightGBM
  boosters are stored as their own text model (``model_to_string(num_iteration=-1)``), reloaded with
  ``lgb.Booster(model_str=...)``.
* Preprocessor parameters, calibrators (linear a, b; isotonic estimator), configs, selections and
  convergence records are stored field by field. ``decode(encode(m))`` re-encodes to the same bytes, so the
  model identity is ``sha256(encode(m))`` (``model_sha256``).
"""

from __future__ import annotations

import hashlib
import json

import lightgbm as lgb
import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import ElasticNet, LogisticRegression

from luna.v7.cv import FoldModel, IsotonicCalibrator, LinearCalibrator, Selection, _Clf
from luna.v7.features import Preprocessor, _ColParams
from luna.v7.models import Config, Convergence, Fitted
from luna.v7.windows import Fold

FORMAT = "luna-v7-foldmodel-1"
_ESTIMATORS = {"ElasticNet": ElasticNet, "LogisticRegression": LogisticRegression,
               "IsotonicRegression": IsotonicRegression}
_EXCLUDED = {"IsotonicRegression": ("f_",)}


class CodecError(ValueError):
    pass


# --- typed values ----------------------------------------------------------------------------------------
def _enc(v: object) -> object:
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return {"f": float(v).hex()}
    if isinstance(v, np.ndarray):
        k = v.dtype.kind
        flat = v.reshape(-1).tolist()
        if k == "f":
            d = [float(x).hex() for x in flat]
        elif k in "iu":
            d = [int(x) for x in flat]
        elif k in "OU" and all(isinstance(x, str) for x in flat):
            d = [str(x) for x in flat]
        else:
            raise CodecError(f"array dtype {v.dtype} is not serialisable")
        return {"a": str(v.dtype), "s": list(v.shape), "d": d}
    if isinstance(v, tuple):
        return {"t": [_enc(x) for x in v]}
    if isinstance(v, list):
        return {"l": [_enc(x) for x in v]}
    if isinstance(v, dict):
        if not all(isinstance(k, str) for k in v):
            raise CodecError("dict keys must be strings")
        return {"m": [[k, _enc(v[k])] for k in sorted(v)]}
    raise CodecError(f"value of type {type(v).__name__} is not serialisable")


def _dec(v: object) -> object:
    if v is None or isinstance(v, (bool, str, int)):
        return v
    if not isinstance(v, dict) or len(v) not in (1, 3):
        raise CodecError("malformed value")
    if "f" in v:
        return float.fromhex(v["f"])
    if "t" in v:
        return tuple(_dec(x) for x in v["t"])
    if "l" in v:
        return [_dec(x) for x in v["l"]]
    if "m" in v:
        return {str(k): _dec(x) for k, x in v["m"]}
    if set(v) == {"a", "s", "d"}:
        dt = np.dtype(v["a"])
        if dt.kind == "f":
            data = [float.fromhex(x) for x in v["d"]]
        elif dt.kind in "iuOU":
            data = list(v["d"])
        else:
            raise CodecError(f"array dtype {dt} is not allowed")
        return np.array(data, dtype=dt).reshape(tuple(v["s"]))
    raise CodecError("malformed value")


# --- components ------------------------------------------------------------------------------------------
def _est(m: object) -> dict:
    name = type(m).__name__
    if _ESTIMATORS.get(name) is not type(m):
        raise CodecError(f"estimator {name} is not in the whitelist")
    state = {k: v for k, v in vars(m).items() if k not in _EXCLUDED.get(name, ())}
    return {"cls": name, "state": _enc(state)}


def _unest(d: dict) -> object:
    cls = _ESTIMATORS.get(d.get("cls"))  # type: ignore[arg-type]
    if cls is None:
        raise CodecError(f"estimator {d.get('cls')!r} is not in the whitelist")
    m = cls.__new__(cls)
    state = _dec(d["state"])
    if not isinstance(state, dict):
        raise CodecError("estimator state")
    m.__dict__.update(state)
    if cls is IsotonicRegression:
        m._build_f(m.X_thresholds_, m.y_thresholds_)
    return m


def _cfg(c: Config) -> dict:
    return {"family": c.family, "params": _enc(dict(c.params)), "seed": int(c.seed)}


def _uncfg(d: dict) -> Config:
    return Config(d["family"], _dec(d["params"]), int(d["seed"]))  # type: ignore[arg-type]


def _conv(c: Convergence | None) -> object:
    return None if c is None else [bool(c.converged), int(c.n_iter), int(c.max_iter), list(c.messages)]


def _unconv(d: object) -> Convergence | None:
    if d is None:
        return None
    a, b, c, m = d  # type: ignore[misc]
    return Convergence(bool(a), int(b), int(c), tuple(str(x) for x in m))


def _fitted(f: Fitted) -> dict:
    if isinstance(f.model, lgb.Booster):
        model: dict = {"lgb": f.model.model_to_string(num_iteration=-1)}
    else:
        model = _est(f.model)
    return {"cfg": _cfg(f.cfg), "model": model, "best_iteration": f.best_iteration,
            "convergence": _conv(f.convergence)}


def _unfitted(d: dict) -> Fitted:
    m = d["model"]
    model = lgb.Booster(model_str=m["lgb"]) if set(m) == {"lgb"} else _unest(m)
    bi = d["best_iteration"]
    return Fitted(_uncfg(d["cfg"]), model, None if bi is None else int(bi), _unconv(d["convergence"]))


def _sel(s: Selection) -> dict:
    return {"cfg": _cfg(s.cfg), "best_iter": s.best_iter, "ic": _enc(float(s.ic)),
            "scores": [[_cfg(c), _enc(float(x))] for c, x in s.scores],
            "fits": [[_cfg(c), _conv(v)] for c, v in s.fits]}


def _unsel(d: dict) -> Selection:
    return Selection(_uncfg(d["cfg"]), None if d["best_iter"] is None else int(d["best_iter"]),
                     _dec(d["ic"]), tuple((_uncfg(c), _dec(x)) for c, x in d["scores"]),  # type: ignore[arg-type]
                     tuple((_uncfg(c), _unconv(v)) for c, v in d["fits"]))


def _pre(p: Preprocessor) -> dict:
    if p._params is None:
        raise CodecError("Preprocessor is not fitted")
    return {"columns": list(p.columns),
            "params": {c: [_enc(float(x)) for x in (q.lo, q.hi, q.mean, q.sd, q.median_z)]
                       for c, q in p._params.items()}}


def _unpre(d: dict) -> Preprocessor:
    p = Preprocessor(d["columns"])
    p._params = {c: _ColParams(*(float(_dec(x)) for x in v)) for c, v in d["params"].items()}  # type: ignore[arg-type]
    return p


# --- the fold model --------------------------------------------------------------------------------------
def _body(fm: FoldModel) -> dict:
    if not isinstance(fm, FoldModel) or fm.test_idx is not None or fm.test_pred is not None:
        raise CodecError("only a FoldModel without test predictions is serialisable")
    f = fm.fold
    return {"format": FORMAT,
            "fold": [f.fold_id, f.test_lo, f.test_hi, list(f.tr), list(f.sel), list(f.cal)],
            "horizon": int(fm.horizon), "cols": list(fm.cols),
            "selection": {k: _sel(v) for k, v in fm.selection.items()},
            "reg": {k: _fitted(v) for k, v in fm.reg.items()},
            "lin_cal": {k: [_enc(float(c.a)), _enc(float(c.b))] for k, c in fm.lin_cal.items()},
            "clf": {k: {"fitted": _fitted(c.fitted), "iso": _est(c.iso._iso)} for k, c in fm.clf.items()},
            "pre": _pre(fm.pre)}


def encode(fm: FoldModel) -> bytes:
    """The canonical bytes of ``fm``."""
    return json.dumps(_body(fm), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def model_sha256(fm: FoldModel) -> str:
    return hashlib.sha256(encode(fm)).hexdigest()


def decode(data: bytes) -> FoldModel:
    """Rebuild the FoldModel field by field from ``encode`` bytes; CodecError on anything malformed."""
    try:
        d = json.loads(data.decode("ascii"))
        if not isinstance(d, dict) or d.get("format") != FORMAT:
            raise CodecError("not a luna-v7 fold-model document")
        fid, lo, hi, tr, sel, cal = d["fold"]
        fold = Fold(int(fid), int(lo), int(hi), tuple(tr), tuple(sel), tuple(cal))  # type: ignore[arg-type]
        lin = {}
        for k, (a, b) in d["lin_cal"].items():
            c = LinearCalibrator()
            c.a, c.b = float(_dec(a)), float(_dec(b))  # type: ignore[arg-type]
            lin[k] = c
        clf = {}
        for k, v in d["clf"].items():
            iso = IsotonicCalibrator()
            iso._iso = _unest(v["iso"])  # type: ignore[assignment]
            clf[k] = _Clf(_unfitted(v["fitted"]), iso)
        fm = FoldModel(fold, int(d["horizon"]), {k: _unsel(v) for k, v in d["selection"].items()},
                       {k: _unfitted(v) for k, v in d["reg"].items()}, lin, clf, _unpre(d["pre"]),
                       tuple(d["cols"]))
    except CodecError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeDecodeError) as e:
        raise CodecError(f"malformed fold-model document: {type(e).__name__}: {e}") from None
    if encode(fm) != data:
        raise CodecError("the document is not canonical (re-encoding differs)")
    return fm


__all__ = ["FORMAT", "CodecError", "decode", "encode", "model_sha256"]
