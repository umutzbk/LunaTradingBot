"""Leakage machinery: the L9 canaries, the K7 concentration audit and the structured, sealed leakage reports
(spec §11 L1 / L9 and "L1–L14 apply to every model, classifiers included", §16 K1 / K7, DEC-19, S-13.4,
S-10.3 – S-10.6; DK-26, DK-27, SC-02, red-team M2 / M4; AMB-33).

* **L9a (DK-27):** per horizon, the label record (``y_reg``, ``y_pos``, ``label_known_at``) is permuted within
  decision dates (``cv.decision_dates``; ``numpy.random.default_rng(seed)``, the frozen seed, fresh per
  horizon, dates ascending) and the real out-of-fold pipeline runs on it: ``cv.run_fold`` (in-fold
  hyperparameter selection, refit, calibration) over exactly the outer folds of the real run's fold models.
  For BOTH regressor families at ALL THREE horizons: the pooled out-of-fold date-level IC (``cv.date_ic``) and
  SE = std(date ICs) / sqrt(#dates) (``cv.date_ic_se``); FAIL if |IC| > 2 SE (an undefined IC / SE fails).
* **L9b (DK-27):** per horizon, the feature "label shifted 1 decision earlier" = ``y_reg`` of the same
  (symbol, side) at the previous decision time of the panel, with its true ``known_at`` (that label's
  ``label_known_at``). PASS iff the real L1 assertion (``features.assert_pit``, the check the candidate
  builder applies to every input frame) refuses it. In a sandbox without L1 the feature replaces the model
  column ``SANDBOX_COLUMN`` and the same out-of-fold pipeline runs; its IC (both families) is REPORTED only.
* **K7 (DK-26):** per fitted model (every fold model, every family incl. classifiers, and the final model) a
  feature's share = (its column + its ``__missing`` indicator) over the sum over all columns of the LightGBM
  gain (``feature_importance("gain")``) or of the absolute linear weight on the standardized design.
  Evaluated: the fold models at h* and the final model, families of ML* (``arms.families``: regressor and
  classifier); the others are reported. TRIGGER if a share > 0.50 on an evaluated model. A trigger is not a
  NO-GO: it blocks ``passed`` until ``resolve_k7`` records the audit ("explained" with text); an "unexplained"
  resolution is a K1 failure.
* **Reports** are frozen dataclasses built only here (a module-private issuer object checked in
  ``__post_init__``) and carry a content sha256; ``verify_report`` recomputes it and requires it to have been
  issued by this module in this process. ``passed`` = verified, every check executed, none failed, no open or
  unexplained K7 trigger. ``run_holdout_checks`` builds the holdout-phase report (L1 at every holdout
  decision); ``verdict`` derives "NO-GO: leakage" from its findings.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from luna.v7 import cv, features, models
from luna.v7.arms import families
from luna.v7.book.engine import BookRun
from luna.v7.constants import FROZEN
from luna.v7.features import features_for_horizon
from luna.v7.models import Fitted
from luna.v7.runner import model_hash
from luna.v7.runrecord import run_output_sha256
from luna.v7.timeutil import MIN, ns_array
from luna.v7.universe import LeakageError

HORIZONS = tuple(FROZEN.horizons_h)
SHARE_MAX = FROZEN.k7_max_share
SE_MULT = FROZEN.l9_se_mult
LABEL_COLS = ("y_reg", "y_pos", "label_known_at")
SHIFT_NAME = "label_shift_1"
SANDBOX_COLUMN = "f1_fl1"
MISSING = "__missing"
RESOLUTIONS = ("explained", "unexplained")
HOLDOUT_CHECKS = ("L1",)
_SHA = re.compile(r"\A[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")

_ISSUER = object()  # module-private: only the check runners below construct reports
_ISSUED: set[str] = set()  # content sha256 of every report issued by this module (this process)


# ---------------------------------------------------------------------------------------------- results


@dataclass(frozen=True)
class L9aResult:
    family: str
    horizon: int
    ic: float
    se: float
    n_dates: int
    n_folds: int
    passed: bool


@dataclass(frozen=True)
class SandboxIC:
    family: str
    ic: float
    se: float
    n_dates: int


@dataclass(frozen=True)
class L9bResult:
    horizon: int
    refused: bool
    n_decisions: int  # decisions at which the shifted label exists
    n_refused: int  # of those, refused by the L1 assertion
    first_refusal: str
    sandbox_column: str
    sandbox: tuple[SandboxIC, ...]


@dataclass(frozen=True)
class K7Result:
    horizon: int
    model: str  # "fold<id>" or "final"
    family: str
    evaluated: bool
    max_share: float
    feature: str | None
    total: float
    trigger: bool
    resolution: str | None = None  # "explained" / "unexplained" (resolve_k7)
    resolution_text: str = ""

    @property
    def key(self) -> tuple[int, str, str]:
        return (self.horizon, self.model, self.family)


# ---------------------------------------------------------------------------------------------- sealing


def _clean(o: object) -> object:
    if isinstance(o, float | np.floating):
        return float(o) if math.isfinite(float(o)) else None
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, Mapping):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, tuple | list):
        return [_clean(v) for v in o]
    return o


def _content(obj: object) -> dict:
    out = {"kind": type(obj).__name__}
    for f in dataclasses.fields(obj):  # type: ignore[arg-type]
        if f.name in ("sha256", "_issuer"):
            continue
        v = getattr(obj, f.name)
        if isinstance(v, tuple) and v and dataclasses.is_dataclass(v[0]):
            v = [dataclasses.asdict(x) for x in v]
        out[f.name] = _clean(v)
    return out


def _content_sha(obj: object) -> str:
    text = json.dumps(_content(obj), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


class _Sealed:
    def __post_init__(self) -> None:
        if getattr(self, "_issuer", None) is not _ISSUER:
            raise TypeError(f"a {type(self).__name__} is built only by the leakage check runners")


def _issue(cls: type, **content):
    probe = cls(**content, sha256="", _issuer=_ISSUER)
    sha = _content_sha(probe)
    _ISSUED.add(sha)
    return cls(**content, sha256=sha, _issuer=_ISSUER)


def verify_report(report: object) -> bool:
    """True iff ``report`` is a LeakageReport / HoldoutLeakageReport issued by this module (in this
    process) whose content still hashes to its ``sha256``."""
    if type(report) not in (LeakageReport, HoldoutLeakageReport):
        return False
    if getattr(report, "_issuer", None) is not _ISSUER:
        return False
    try:
        sha = _content_sha(report)
    except (TypeError, ValueError):
        return False
    return sha == report.sha256 and sha in _ISSUED  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------------------------- reports


@dataclass(frozen=True)
class LeakageReport(_Sealed):
    """The pre-opening leakage report (L9a, L9b, K7) with DT4-consistent provenance. Built only by
    ``run_leakage_checks`` / ``resolve_k7``; see the module docstring."""

    ml_star: tuple[str, int]
    seed: int
    l9a: tuple[L9aResult, ...]
    l9b: tuple[L9bResult, ...]
    k7: tuple[K7Result, ...]
    k7_expected: tuple[tuple[int, str, str], ...]
    commit: str
    dirty: bool
    data_manifests: tuple[tuple[str, str], ...]
    panel_sha256s: tuple[tuple[int, str], ...]
    fold_model_hashes: tuple[tuple[int, tuple[str, ...]], ...]  # per h, ordered by test quarter
    final_model_hash: str | None
    sha256: str
    _issuer: object = field(default=None, repr=False, compare=False)

    @property
    def missing(self) -> tuple[str, ...]:
        """Checks not executed (any -> not passed)."""
        out: list[str] = []
        have = {(x.family, x.horizon) for x in self.l9a}
        out += [f"L9a {f}@{h}h not executed" for h in HORIZONS for f in models.REGRESSORS
                if (f, h) not in have]
        hb = {x.horizon for x in self.l9b}
        out += [f"L9b @{h}h not executed" for h in HORIZONS if h not in hb]
        k7 = {k.key: k for k in self.k7}
        exp = set(self.k7_expected)
        fams = set(families(self.ml_star[0]))
        if not any(m != "final" for _, m, _ in exp):
            out.append(f"K7: no fold model at h* = {self.ml_star[1]}h evaluated (not executed)")
        if {f for _, m, f in exp if m == "final"} != fams:
            out.append("K7: final model families incomplete (not executed)")
        out += [f"K7 {h}h/{m}/{f}: not executed" + (" (final model missing)" if m == "final" else "")
                for (h, m, f) in sorted(exp) if (h, m, f) not in k7 or not k7[(h, m, f)].evaluated]
        return tuple(out)

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def open_triggers(self) -> tuple[K7Result, ...]:
        """Evaluated K7 triggers without a recorded audit resolution."""
        return tuple(k for k in self.k7 if k.evaluated and k.trigger and k.resolution is None)

    @property
    def k1(self) -> bool:
        """An evaluated K7 trigger whose audit is recorded as unexplained (K1)."""
        return any(k.evaluated and k.trigger and k.resolution == "unexplained" for k in self.k7)

    @property
    def failures(self) -> tuple[str, ...]:
        """Executed checks that failed (leakage findings: K1 before the opening)."""
        out = [f"L9a {x.family}@{x.horizon}h: |IC| {abs(x.ic):.6g} > {SE_MULT:g} SE ({x.se:.6g}) or undefined"
               for x in self.l9a if not x.passed]
        out += [f"L9b @{x.horizon}h: the shifted-label feature is not refused by L1" for x in self.l9b
                if not x.refused]
        out += [f"K7 {k.horizon}h/{k.model}/{k.family}: {k.feature} share {k.max_share:.4f} unexplained "
                f"(K1): {k.resolution_text}" for k in self.k7 if k.evaluated and k.trigger
                and k.resolution == "unexplained"]
        return tuple(out)

    @property
    def blocking(self) -> tuple[str, ...]:
        """Everything that keeps the report from passing."""
        return (*(["not verified (not issued by run_leakage_checks or altered)"]
                  if not verify_report(self) else []), *self.missing, *self.failures,
                *(f"K7 {k.horizon}h/{k.model}/{k.family}: {k.feature} share {k.max_share:.4f} > "
                  f"{SHARE_MAX:g}: mandatory leakage audit pending" for k in self.open_triggers))

    @property
    def passed(self) -> bool:
        return not self.blocking


@dataclass(frozen=True)
class HoldoutLeakageReport(_Sealed):
    """Holdout-phase leakage findings (S-13.4 "NO-GO: leakage"), built only by ``run_holdout_checks``."""

    pre_sha256: str
    ml_star: tuple[str, int]
    runs: tuple[str, ...]  # output sha256 of every run whose decisions were checked
    checks: tuple[tuple[str, int], ...]  # (check, items checked)
    findings: tuple[tuple[str, str], ...]  # (check, detail)
    sha256: str
    _issuer: object = field(default=None, repr=False, compare=False)

    @property
    def complete(self) -> bool:
        return {c for c, _ in self.checks} >= set(HOLDOUT_CHECKS)


def covers(report: HoldoutLeakageReport, runs: Sequence[BookRun]) -> bool:
    """Every run's decisions were checked by this holdout report (output sha256 recorded)."""
    have = set(report.runs)
    return all(run_output_sha256(r) in have for r in runs)


# ---------------------------------------------------------------------------------------------- L9a


def permute_within_dates(samples: pd.DataFrame, horizon: int, seed: int) -> pd.DataFrame:
    """The canonical samples with the label record (``LABEL_COLS``) permuted within each decision date."""
    s = cv.canonical(samples, horizon)
    rng = np.random.default_rng(int(seed))
    dates = cv.decision_dates(s["decision_ts"])
    perm = np.arange(len(s))
    for d in np.unique(dates):
        idx = np.flatnonzero(dates == d)
        perm[idx] = idx[rng.permutation(len(idx))]
    out = s.copy()
    for c in LABEL_COLS:
        out[c] = s[c].to_numpy()[perm]
    return out


def oof(samples: pd.DataFrame, horizon: int, folds: Sequence) -> dict[str, tuple]:
    """Pooled out-of-fold (pred, y_reg, dates) per regressor family: ``cv.run_fold`` on every fold."""
    s = cv.canonical(samples, horizon)
    parts: dict[str, list] = {f: [] for f in models.REGRESSORS}
    for fold in folds:
        m = cv.run_fold(fold, s, horizon)
        test = s.iloc[m.test_idx]
        d = cv.decision_dates(test["decision_ts"])
        for f in models.REGRESSORS:
            pred = m.test_pred[f"yhat_{f}"].to_numpy(np.float64)
            parts[f].append((pred, test["y_reg"].to_numpy(np.float64), d))
    return {f: tuple(np.concatenate([p[i] for p in v]) if v else np.empty(0) for i in range(3))
            for f, v in parts.items()}


def _ic_se(pred, y, dates) -> tuple[float, float, int]:
    if len(pred) == 0:
        return float("nan"), float("nan"), 0
    return cv.date_ic(pred, y, dates), cv.date_ic_se(pred, y, dates), len(cv.date_ics(pred, y, dates))


def l9a_result(family: str, horizon: int, pred, y, dates, n_folds: int) -> L9aResult:
    ic, se, n = _ic_se(pred, y, dates)
    ok = math.isfinite(ic) and math.isfinite(se) and abs(ic) <= SE_MULT * se
    return L9aResult(family, int(horizon), ic, se, n, int(n_folds), bool(ok))


def l9a(samples: pd.DataFrame, horizon: int, folds: Sequence, seed: int) -> tuple[L9aResult, ...]:
    """L9a at one horizon for both regressor families (see module docstring)."""
    o = oof(permute_within_dates(samples, horizon, seed), horizon, folds)
    return tuple(l9a_result(f, horizon, *o[f], n_folds=len(folds)) for f in models.REGRESSORS)


# ---------------------------------------------------------------------------------------------- L9b


def shifted_label_feature(samples: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Per canonical row: ``value`` = y_reg of the same (symbol, side) at the panel's previous decision time,
    ``known_at`` = that label's ``label_known_at`` (missing: NaN / <NA>)."""
    s = cv.canonical(samples, horizon)
    ts = ns_array(s["decision_ts"], "decision_ts")
    times = np.unique(ts)
    prev = pd.Series(np.r_[np.iinfo(np.int64).min, times[:-1]], index=times)
    keys = s[cv.KEY].copy()
    keys["_prev"] = prev.reindex(ts).to_numpy()
    src = s[cv.KEY + ["y_reg", "label_known_at"]].rename(
        columns={"decision_ts": "_prev", "y_reg": "value", "label_known_at": "known_at"})
    out = keys.merge(src, on=["_prev", "symbol", "side"], how="left", validate="many_to_one")
    out["known_at"] = out["known_at"].astype("Int64")
    return out[[*cv.KEY, "value", "known_at"]].reset_index(drop=True)


def l9b_refusal(feature: pd.DataFrame) -> tuple[int, int, str]:
    """(decisions with the feature, decisions refused by the L1 assertion, first refusal message): the real
    ``features.assert_pit`` on the feature's rows at each decision t."""
    refused = 0
    first = ""
    have = feature[feature["value"].notna()]
    ts = ns_array(have["decision_ts"], "decision_ts")
    ka = have["known_at"].to_numpy(np.int64)
    order = np.argsort(ts, kind="stable")
    times, starts = np.unique(ts[order], return_index=True)
    for t, idx in zip(times, np.split(order, starts[1:]), strict=True):
        try:
            features.assert_pit({SHIFT_NAME: pd.DataFrame({"known_at": ka[idx]})}, int(t))
        except LeakageError as e:
            refused += 1
            first = first or f"decision {int(t)}: {e}"
    return len(times), refused, first


def l9b(samples: pd.DataFrame, horizon: int, folds: Sequence,
        sandbox_column: str = SANDBOX_COLUMN) -> L9bResult:
    """L9b at one horizon (see module docstring)."""
    if sandbox_column not in features_for_horizon(horizon) or sandbox_column == "side":
        raise ValueError(f"sandbox column {sandbox_column!r} is not a model feature")
    s = cv.canonical(samples, horizon)
    f = shifted_label_feature(s, horizon)
    n, refused, first = l9b_refusal(f)
    sb = s.copy()
    sb[sandbox_column] = f["value"].to_numpy(np.float64)
    o = oof(sb, horizon, folds)
    box = tuple(SandboxIC(fam, *_ic_se(*o[fam])) for fam in models.REGRESSORS)
    return L9bResult(int(horizon), bool(refused > 0), n, refused, first, sandbox_column, box)


# ---------------------------------------------------------------------------------------------- K7


def feature_shares(fitted: Fitted) -> tuple[dict[str, float], float]:
    """({feature: share}, total): column + ``__missing`` indicator over the sum over all columns of the
    LightGBM gain or of the absolute linear weight (standardized design). total 0 -> every share 0."""
    fam = fitted.cfg.family
    m = fitted.model
    if fam in ("lgb_reg", "lgb_clf"):
        names = [str(c) for c in m.feature_name()]
        gain = m.feature_importance(importance_type="gain", iteration=fitted.best_iteration)
        w = np.asarray(gain, np.float64)
    elif fam in ("en", "logit_en"):
        names = [str(c) for c in m.feature_names_in_]
        w = np.abs(np.asarray(m.coef_, np.float64).reshape(-1))
    else:
        raise ValueError(f"K7: unknown family {fam!r}")
    if len(names) != len(w) or not np.isfinite(w).all() or (w < 0).any():
        raise ValueError("K7: malformed importances")
    merged: dict[str, float] = {}
    for n, v in zip(names, w, strict=True):
        base = n[: -len(MISSING)] if n.endswith(MISSING) else n
        merged[base] = merged.get(base, 0.0) + float(v)
    total = math.fsum(w)
    return {k: (v / total if total > 0 else 0.0) for k, v in merged.items()}, float(total)


def concentration(fitted: Fitted) -> tuple[float, str | None, float]:
    """(max share, its feature, total); ties broken by feature name; an all-zero model -> (0.0, None, 0.0)."""
    shares, total = feature_shares(fitted)
    if total <= 0 or not shares:
        return 0.0, None, total
    feat = min(shares, key=lambda k: (-shares[k], k))
    return shares[feat], feat, total


def _fitted(model: object) -> list[tuple[str, Fitted]]:
    out: list[tuple[str, Fitted]] = []
    for attr in ("reg", "clf"):
        maps = getattr(model, attr, None)
        if not isinstance(maps, Mapping):
            raise ValueError("K7 needs cv.FoldModel-like models (reg / clf family maps of fitted models)")
        for fam in sorted(maps):
            f = maps[fam]
            f = getattr(f, "fitted", f)
            if not isinstance(f, Fitted):
                raise ValueError(f"K7: {attr}[{fam}] is not a fitted model")
            out.append((fam, f))
    return out


def _k7(h: int, label: str, model: object, evaluated_fams: tuple[str, ...]) -> list[K7Result]:
    rows = []
    for fam, f in _fitted(model):
        share, feat, total = concentration(f)
        ev = fam in evaluated_fams
        rows.append(K7Result(int(h), label, fam, ev, share, feat, total, bool(share > SHARE_MAX)))
    return rows


# ---------------------------------------------------------------------------------------------- runner


def _frame_sha(df: pd.DataFrame) -> str:
    h = hashlib.sha256(repr([(str(c), str(t)) for c, t in df.dtypes.items()]).encode())
    h.update(pd.util.hash_pandas_object(df, index=False).to_numpy().tobytes())
    return h.hexdigest()


def _provenance(commit: str, dirty: bool, data_manifests: Mapping[str, str]) -> tuple:
    if not _COMMIT.match(str(commit)):
        raise ValueError("commit must be a 40-hex git commit")
    if not isinstance(dirty, bool):
        raise ValueError("dirty must be a bool (runrecord.code_state)")
    if dirty:
        raise ValueError("leakage checks are research: they need a clean working tree (code_dirty)")
    if not data_manifests:
        raise ValueError("data manifest sha256s are required")
    for name, sha in data_manifests.items():
        if not _SHA.match(str(sha)):
            raise ValueError(f"{name}: not a sha256")
    return tuple(sorted((str(k), str(v)) for k, v in data_manifests.items()))


def run_leakage_checks(*, samples: Mapping[int, pd.DataFrame], fold_models: Mapping[int, Sequence],
                       final_model: object | None, ml_star: tuple[str, int], commit: str, dirty: bool,
                       data_manifests: Mapping[str, str], seed: int = models.SEED,
                       sandbox_column: str = SANDBOX_COLUMN) -> LeakageReport:
    """The pre-opening leakage checks (L9a, L9b, K7) -> a sealed LeakageReport.

    ``samples``: per horizon the W_DEV label panel of the real run (``cv`` samples); ``fold_models``: per
    horizon the real run's outer-fold models (``cv.FoldModel``; their folds are the folds the canaries
    mirror); ``final_model``: ML*'s ``cv.final_model`` at h*; ``ml_star`` = (A2 / A3, h*). A horizon missing
    from ``samples`` / ``fold_models`` or a missing final model is recorded as not executed (never passed).
    """
    arm, h_star = str(ml_star[0]), int(ml_star[1])
    fams = families(arm) if arm in ("A2", "A3") else None
    if fams is None:
        raise ValueError(f"ML* must be A2 or A3, got {arm!r}")
    if h_star not in HORIZONS:
        raise ValueError(f"unknown h* {h_star}")
    prov = _provenance(commit, dirty, data_manifests)
    if final_model is not None and int(getattr(final_model, "horizon", -1)) != h_star:
        raise ValueError(f"the final model is not ML*'s at h* = {h_star}h")
    fms: dict[int, tuple] = {}
    for h, ms in fold_models.items():
        if int(h) not in HORIZONS:
            raise ValueError(f"unknown horizon {h}")
        ordered = tuple(sorted(ms, key=lambda m: int(m.fold.test_lo)))
        if any(int(m.horizon) != int(h) for m in ordered):
            raise ValueError(f"fold models listed under {h}h are for another horizon")
        if len({m.fold.fold_id for m in ordered}) != len(ordered):
            raise ValueError(f"duplicate fold ids at {h}h")
        fms[int(h)] = ordered
    l9a_rows: list[L9aResult] = []
    l9b_rows: list[L9bResult] = []
    panel_sha: list[tuple[int, str]] = []
    for h in HORIZONS:
        if h not in samples or not fms.get(h):
            continue
        s = cv.canonical(samples[h], h)
        panel_sha.append((h, _frame_sha(s)))
        folds = [m.fold for m in fms[h]]
        l9a_rows += l9a(s, h, folds, seed)
        l9b_rows.append(l9b(s, h, folds, sandbox_column))
    k7_rows: list[K7Result] = []
    for h in sorted(fms):
        for m in fms[h]:
            k7_rows += _k7(h, f"fold{m.fold.fold_id}", m, fams if h == h_star else ())
    if final_model is not None:
        k7_rows += _k7(h_star, "final", final_model, fams)
    expected = [(h_star, f"fold{m.fold.fold_id}", f) for m in fms.get(h_star, ()) for f in fams]
    expected += [(h_star, "final", f) for f in fams]
    return _issue(
        LeakageReport, ml_star=(arm, h_star), seed=int(seed), l9a=tuple(l9a_rows), l9b=tuple(l9b_rows),
        k7=tuple(k7_rows), k7_expected=tuple(expected), commit=str(commit), dirty=bool(dirty),
        data_manifests=prov, panel_sha256s=tuple(panel_sha),
        fold_model_hashes=tuple((h, tuple(model_hash(m) for m in fms[h])) for h in sorted(fms)),
        final_model_hash=None if final_model is None else model_hash(final_model),
    )


def resolve_k7(report: LeakageReport, key: tuple[int, str, str], resolution: str, text: str) -> LeakageReport:
    """Record the mandatory leakage audit of one K7 trigger (K7: "if unexplained -> K1"): a NEW sealed report
    (new sha256) with ``resolution`` ("explained" / "unexplained") and its non-empty ``text``. A recorded
    resolution is final."""
    if not verify_report(report) or not isinstance(report, LeakageReport):
        raise ValueError("resolve_k7 needs a LeakageReport issued by run_leakage_checks")
    if resolution not in RESOLUTIONS:
        raise ValueError(f"resolution must be one of {RESOLUTIONS}")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("the audit resolution needs an explicit text")
    key = (int(key[0]), str(key[1]), str(key[2]))
    rows = list(report.k7)
    hit = [i for i, k in enumerate(rows) if k.key == key]
    if not hit:
        raise ValueError(f"no K7 result {key}")
    k = rows[hit[0]]
    if not k.trigger:
        raise ValueError(f"{key} is not a K7 trigger")
    if k.resolution is not None:
        raise ValueError(f"{key}: the audit resolution is already recorded ({k.resolution})")
    rows[hit[0]] = dataclasses.replace(k, resolution=resolution, resolution_text=text.strip())
    content = {f.name: getattr(report, f.name) for f in dataclasses.fields(report)
               if f.name not in ("sha256", "_issuer")}
    content["k7"] = tuple(rows)
    return _issue(LeakageReport, **content)


def run_holdout_checks(pre: LeakageReport, *, runs: Sequence[BookRun],
                       records: Callable[[int], object]) -> HoldoutLeakageReport:
    """Holdout-phase L1 check: every decision t of every run, ``records(t)`` (the decision provider's
    ``record``) must report ``max_known_at`` <= t - 1 min; a ``LeakageError`` raised while building the
    decision is a finding too. Needs the passed pre-opening report (linked by its sha256)."""
    if not verify_report(pre) or not isinstance(pre, LeakageReport):
        raise ValueError("the pre-opening LeakageReport was not issued by run_leakage_checks")
    if not pre.passed:
        raise ValueError(f"the pre-opening LeakageReport did not pass: {pre.blocking}")
    if not runs or not all(isinstance(r, BookRun) for r in runs):
        raise ValueError("runs: the executed holdout books are required")
    ts: set[int] = set()
    for r in runs:
        if r.decisions is None or "t" not in r.decisions.columns:
            raise ValueError(f"{r.arm}/{r.execution}: decision log without 't' (L1 not checkable)")
        ts |= {int(t) for t in ns_array(r.decisions["t"], "decisions.t")}
    findings: list[tuple[str, str]] = []
    for t in sorted(ts):
        try:
            rec = records(t)
        except LeakageError as e:
            findings.append(("L1", f"decision {t}: {e}"))
            continue
        if not hasattr(rec, "max_known_at"):
            raise ValueError("records(t) must return a decision record with max_known_at")
        mk = rec.max_known_at
        if mk is not None and int(mk) > t - MIN:
            findings.append(("L1", f"decision {t}: max_known_at {int(mk)} > t - 1 min"))
    return _issue(HoldoutLeakageReport, pre_sha256=pre.sha256, ml_star=pre.ml_star,
                  runs=tuple(run_output_sha256(r) for r in runs), checks=(("L1", len(ts)),),
                  findings=tuple(findings))


__all__ = ["HOLDOUT_CHECKS", "HoldoutLeakageReport", "K7Result", "L9aResult", "L9bResult", "LABEL_COLS",
           "LeakageReport", "SANDBOX_COLUMN", "SHIFT_NAME", "SandboxIC", "concentration", "covers",
           "feature_shares", "l9a", "l9a_result", "l9b", "l9b_refusal", "model_hash", "oof",
           "permute_within_dates", "resolve_k7", "run_holdout_checks", "run_leakage_checks",
           "shifted_label_feature", "verify_report"]
