"""Task 27 (SC-04, user override 1): the Kraken exploratory path (S-1.6, S-2.4, S-13.3 #5, L11, DEC-17;
AMB-41).

Synthetic Kraken-format frozen copies are built in code (never var/). The L11 guard refuses without / with a
forged authorization, on an M6 overlap, before 2025-10-06 and on a manifest mismatch, each K9-logged; the
adapter maps the V5 / V6 formats to V7 arrays with exact known_at; comparison #5 is exploratory, UNAVAILABLE
(STOPPED, AMB-41) and can change only the S-13.3 secondary report (Holm over exactly 6)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from luna.v7 import guard, k9, kraken, runner_select, runrecord, stats
from luna.v7 import verdict as V
from luna.v7.accounting import account
from luna.v7.timeutil import MIN, D, H, to_ns
from tests.v7 import auth_fixtures as af
from tests.v7.run_fixtures import funding_run
from tests.v7.test_verdict import LO, _inputs, _nets

T0 = to_ns("2025-10-06T00:00:00Z")
WIN = (T0, T0 + D)
SYMS = ("BTC", "ETH")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _idx(start: str, n: int, freq: str) -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq=freq, tz="UTC")


def build_copy(root: Path, syms=SYMS) -> str:
    """A tiny frozen V5 copy in the V4 / V5 layout (V5 ``frozen.freeze_inputs``); returns its manifest sha."""
    files: dict[str, dict] = {}

    def put(rel: str, df: pd.DataFrame) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(p)
        files[rel] = {"kind": rel.split("/")[1], "rows": len(df), "sha256": _sha(p)}

    for k, a in enumerate(syms):
        n15 = 4 * 24 * 3  # 2025-10-05 .. 2025-10-08
        i15 = _idx("2025-10-05", n15, "15min")
        px = 100.0 * (k + 1) + np.arange(n15) * 0.01
        filled = np.zeros(n15, dtype=bool)
        filled[4 * 24 + 8] = True  # one V4 gap-fill bar inside the window (2025-10-06 02:00)
        put(f"bars/kraken_futures/{a}.parquet", pd.DataFrame(
            {"open": px, "high": px + 0.5, "low": px - 0.5, "close": px + 0.1, "volume": np.full(n15, 3.0),
             "trades": np.zeros(n15, dtype=np.int64), "filled": filled}, index=i15))
        for kind in ("mark", "index"):
            put(f"history_v4/{kind}/{a}.parquet", pd.DataFrame({"close": px + 0.2}, index=i15))
        ih = _idx("2025-10-01", 24 * 7, "1h")  # V4 history before 2025-10-06 (§3) and after
        put(f"history_v4/funding/{a}.parquet", pd.DataFrame(
            {"funding_abs": np.full(len(ih), 0.01), "funding_rel": np.full(len(ih), 1e-5)}, index=ih))
        i5 = _idx("2025-10-06", 12 * 24 * 2, "5min")
        rel = f"history_v5/spreads/{a}.parquet"
        put(rel, pd.DataFrame({"spread_full": np.full(len(i5), 4e-4)}, index=i5))
        prel = f"history_v5/spreads/{a}.provenance.json"
        (root / prel).write_text(json.dumps({"requested_interval_s": 300, "observed_cadence_s": 300,
                                             "real_resolution_matches_interval": True,
                                             "sha256": files[rel]["sha256"]}), encoding="utf-8")
        files[prel] = {"kind": "provenance_v5", "sha256": _sha(root / prel)}
    inst = root / "config/v4_instruments.json"
    inst.parent.mkdir(parents=True, exist_ok=True)
    inst.write_text(json.dumps({"assets": {a: {"perp": {"step": 0.001}} for a in syms}}), encoding="utf-8")
    files["config/v4_instruments.json"] = {"kind": "instruments", "sha256": _sha(inst)}
    man = root / kraken.MANIFEST
    man.write_text(json.dumps({"cutoff": "2026-07-01T00:00:00+00:00", "assets": list(syms),
                               "files": dict(sorted(files.items()))}, indent=1), encoding="utf-8")
    return _sha(man)


@pytest.fixture(scope="module")
def _opening(tmp_path_factory):
    return af.opening_once(tmp_path_factory.mktemp("opening"))


@pytest.fixture
def opened(_opening, monkeypatch):
    return af.use_opening(monkeypatch, _opening)


@pytest.fixture
def log(tmp_path):
    lg = k9.K9Log(tmp_path / "k9")
    with k9.using(lg):
        yield lg


@pytest.fixture
def copy(tmp_path, monkeypatch):
    root = tmp_path / "frozen"
    monkeypatch.setattr(kraken, "V5_MANIFEST_SHA256", build_copy(root))  # the pin of THIS synthetic copy
    return root


def _refused(log, fn, cause, rule_part):
    with pytest.raises(kraken.KrakenBlocked) as ei:
        fn()
    assert cause is None or isinstance(ei.value.__cause__, cause), ei.value.__cause__
    recs = log.records()
    assert recs and rule_part in recs[-1]["rule"] and recs[-1]["rule"].startswith("L11 Kraken outcome guard")
    return ei.value


# ------------------------------------------------------------------------------------------- constants / V6


def test_pinned_constants():
    assert kraken.V5_MANIFEST_SHA256 == "eecdc6a21626ccd5e2677cce2503f304a5730845de552554281a6ea54cc53f14"
    assert kraken.WINDOW == (to_ns("2025-10-06T00:00:00Z"), V.windows.W_HOLD[1])  # DEC-17, ends at M6 start
    assert kraken.EXPLORATORY == "exploratory (previously observed window)" == V.EXPLORATORY
    assert kraken.COMPARISON == V.SECONDARY[5] == "ML* Binance vs Kraken"
    assert kraken.HEDGE == "BTC"
    assert {g[1] for g in kraken.GAPS} <= {"yes", "no", "fallback", "n/a", "clarification"}
    assert set(kraken.STOPPED) == {"S-2.4 gates", "Kraken ML* book / labels / IC"}
    assert "AMB-41" in kraken.UNAVAILABLE_WHY and "normative amendment required" in kraken.UNAVAILABLE_WHY


def test_v6_universe_and_fees_come_from_the_hash_checked_constants():
    v6 = kraken.v6_kraken()  # guard._roots() = the real code repository here
    assert v6.universe == ("ADA", "AVAX", "BNB", "BTC", "DOGE", "ENA", "ETH", "FET", "HYPE", "LINK", "NEAR",
                           "PEPE", "PUMP", "SOL", "SUI", "TAO", "UNI", "XLM", "XRP", "ZEC")
    assert (v6.futures_maker, v6.futures_taker) == (0.0002, 0.0005)
    assert v6.fee_snapshot_sha256.startswith("118e7918")
    assert v6.constants_sha256 == kraken.spec.V6_CONSTANTS_SHA256


# ---------------------------------------------------------------------------------------------- L11 guard


def test_refused_without_authorization(copy, log):
    for bad in (None, {"approved": True}):
        _refused(log, lambda b=bad: kraken.authorize_kraken(b, *WIN, copy), guard.HoldoutBlocked, "S-1.7")
    assert len(log.records()) == 2 and log.records()[0]["request"]["kind"] == "kraken"


def test_refused_with_a_forged_authorization(copy, log, opened):
    forged = object.__new__(guard.HoldoutAuthorization)
    _refused(log, lambda: kraken.authorize_kraken(forged, *WIN, copy), guard.HoldoutBlocked, "S-1.7")
    with pytest.raises(TypeError):  # the Kraken authorization cannot be built outside authorize_kraken
        kraken.KrakenAuthorization(opened.auth, "x" * 64, WIN, copy, "y" * 64, {}, kraken.v6_kraken())
    with pytest.raises(kraken.KrakenBlocked):
        kraken.load_symbol(object(), "ETH", *WIN)  # type: ignore[arg-type]


def test_m6_overlap_is_refused_even_with_the_opening(copy, log, opened):
    hi = kraken.WINDOW[1] + D  # reaches into M6 = [2026-07-01, R_V6)
    e = _refused(log, lambda: kraken.authorize_kraken(opened.auth, kraken.WINDOW[1] - 30 * D, hi, copy),
                 guard.EmbargoBlocked, "S-1.3 M6 embargo")
    assert "M6" in str(e)


def test_window_before_2025_10_06_is_refused(copy, log, opened):
    _refused(log, lambda: kraken.authorize_kraken(opened.auth, T0 - D, T0 + D, copy), None, "DEC-17")
    _refused(log, lambda: kraken.authorize_kraken(opened.auth, T0 + D, T0, copy), None, "DEC-17")  # empty


def test_manifest_mismatch_is_refused(tmp_path, log, opened, monkeypatch):
    root = tmp_path / "frozen"
    sha = build_copy(root)
    # the real pinned sha (not this copy's): refused before any data file is read
    _refused(log, lambda: kraken.authorize_kraken(opened.auth, *WIN, root), kraken.FrozenCopyMismatch,
             "manifest")
    monkeypatch.setattr(kraken, "V5_MANIFEST_SHA256", sha)
    assert kraken.authorize_kraken(opened.auth, *WIN, root).manifest_sha256 == sha
    p = root / "history_v4/funding/ETH.parquet"  # a data file altered after the freeze
    p.write_bytes(p.read_bytes() + b"x")
    e = _refused(log, lambda: kraken.authorize_kraken(opened.auth, *WIN, root), kraken.FrozenCopyMismatch,
                 "manifest")
    assert "ETH.parquet" in str(e)
    p.unlink()
    _refused(log, lambda: kraken.authorize_kraken(opened.auth, *WIN, root), kraken.FrozenCopyMismatch,
             "manifest")


def test_unlisted_file_is_refused(copy, log, opened):
    (copy / "bars/kraken_futures/XRP.parquet").write_bytes(b"not listed")
    _refused(log, lambda: kraken.authorize_kraken(opened.auth, *WIN, copy), kraken.FrozenCopyMismatch,
             "manifest")


# --------------------------------------------------------------------------------------------- the adapter


def test_adapter_maps_formats_with_exact_known_at(copy, log, opened):
    ka = kraken.authorize_kraken(opened.auth, *WIN, copy)
    assert ka.label == kraken.EXPLORATORY and ka.window == WIN and ka.opening_id == opened.auth.opening_id
    x = kraken.load_symbol(ka, "ETH", T0 - 5 * D, T0 + D)  # look-back before 2025-10-06 is point in time
    assert x.label == kraken.EXPLORATORY and x.filled_dropped == 1
    b = x.bars15
    assert list(b.columns) == ["open_ts", "o", "h", "l", "c", "vol_base", "known_at"]
    assert b["open_ts"].dtype == np.int64 and (b["known_at"] - b["open_ts"] == 15 * MIN).all()
    assert (T0 + 2 * H) not in set(b["open_ts"])  # the synthesised bar is dropped, not used
    assert len(b) == 4 * 24 * 2 - 1 and b["open_ts"].min() == T0 - D and b["open_ts"].max() < T0 + D
    assert b.loc[b["open_ts"] == T0, "c"].item() == pytest.approx(200.0 + 96 * 0.01 + 0.1)
    for f in (x.mark15, x.index15):
        assert list(f.columns) == ["open_ts", "c", "known_at"]
        assert (f["known_at"] - f["open_ts"] == 15 * MIN).all()
    fu = x.funding
    assert list(fu.columns) == ["period_start_ts", "rate_rel", "rate_abs", "known_at"]
    assert (fu["known_at"] - fu["period_start_ts"] == H).all()  # a period's rate is known at its end
    assert fu["period_start_ts"].min() == to_ns("2025-10-01T00:00:00Z")  # frozen V4 funding history
    sp = x.spreads5m
    assert (sp["known_at"] - sp["label_ts"] == 5 * MIN).all() and sp["label_ts"].min() == T0  # T + 5 min
    assert kraken.load_hedge(ka, *WIN).symbol == "BTC"
    for sym, lo, hi in (("XRP", *WIN), ("ETH", T0, T0 + 2 * D)):  # not loaded: off the copy / past the window
        with pytest.raises(kraken.KrakenBlocked):
            kraken.load_symbol(ka, sym, lo, hi)
    with pytest.raises(kraken.KrakenBlocked):
        kraken.load_symbol(ka, "DOGE", *WIN)  # a universe symbol missing from the copy: refused, never empty


def test_copied_authorization_or_comparison_never_verifies(copy, log, opened):
    ka = kraken.authorize_kraken(opened.auth, *WIN, copy)
    wide = replace(ka, window=(WIN[0], kraken.WINDOW[1] + 30 * D))  # a widened copy keeps the issuer field
    _refused(log, lambda: kraken.load_symbol(wide, "ETH", *WIN), None, "DEC-17")
    c = kraken.comparison(ka, _ml_run())
    assert kraken.verify_comparison(c) and not kraken.verify_comparison(replace(c, available=True))


def test_loader_rechecks_the_file_hash(copy, log, opened):
    ka = kraken.authorize_kraken(opened.auth, *WIN, copy)
    p = copy / "bars/kraken_futures/ETH.parquet"
    p.write_bytes(p.read_bytes() + b"x")
    _refused(log, lambda: kraken.load_symbol(ka, "ETH", *WIN), kraken.FrozenCopyMismatch, "manifest")


# --------------------------------------------------------------------------------------------- comparison 5


def _ml_run():
    return replace(funding_run(_nets(0.002, 1), arm="A2", h=72), research=True)


def test_comparison_is_exploratory_unavailable_and_recorded(copy, log, opened):
    ka = kraken.authorize_kraken(opened.auth, *WIN, copy)
    ml = _ml_run()
    c = kraken.comparison(ka, ml)
    assert kraken.verify_comparison(c) and c.label == kraken.EXPLORATORY and c.name == V.KRAKEN
    assert not c.available and c.why == kraken.UNAVAILABLE_WHY
    assert c.ml_run_sha256 == runrecord.run_output_sha256(ml)
    assert isinstance(c.pair, V.Unavailable) and c.pair.why.startswith(kraken.EXPLORATORY)
    assert dict(c.provenance) == {"kraken_v5_frozen_manifest": kraken.V5_MANIFEST_SHA256,
                                  "v6_frozen_constants": kraken.spec.V6_CONSTANTS_SHA256,
                                  "v6_code_freeze_record": kraken.spec.V6_RECORD_SHA256,
                                  "v6_kraken_fee_snapshot": ka.v6.fee_snapshot_sha256}
    with pytest.raises(TypeError):
        replace(c, available=True, _issuer=None)
    for bad in (replace(ml, research=False), replace(ml, execution="E_M"), replace(ml, arm="A0")):
        with pytest.raises(ValueError, match="ML\\* E_T"):
            kraken.comparison(ka, bad)
    # DT4: the V5 manifest sha and the V6 artefact hashes, plus the exploratory block
    rec = runrecord.run_record(commit="a" * 40, dirty=False, config={"x": 1},
                               data_manifests={"binance": "b" * 64}, seeds={"s": 1}, uv_lock_sha256="c" * 64,
                               libraries={}, convergence=[], runs=[ml], kraken=c)
    assert rec["data_manifest_sha256s"]["kraken_v5_frozen_manifest"] == kraken.V5_MANIFEST_SHA256
    assert rec["data_manifest_sha256s"]["v6_frozen_constants"] == kraken.spec.V6_CONSTANTS_SHA256
    assert rec["kraken_exploratory"]["label"] == kraken.EXPLORATORY
    assert rec["kraken_exploratory"]["available"] is False
    with pytest.raises(ValueError):
        runrecord.run_record(commit="a" * 40, dirty=False, config={"x": 1}, data_manifests={"b": "b" * 64},
                             seeds={"s": 1}, uv_lock_sha256="c" * 64, libraries={}, convergence=[], runs=[ml],
                             kraken=object())


def test_holdout_inputs_takes_comparison_5_only_from_the_kraken_module(copy, log, opened):
    ka = kraken.authorize_kraken(opened.auth, *WIN, copy)
    ml = _ml_run()
    c = kraken.comparison(ka, ml)
    pin = guard.verify_authorization(opened.auth)
    fam = {n: None for n in V.SECONDARY}
    out = runner_select._kraken_secondary(fam, c, pin, ml)
    assert isinstance(out[V.KRAKEN], V.Unavailable) and set(out) == set(V.SECONDARY)
    assert runner_select._kraken_secondary(fam, None, pin, ml) == fam  # absent: "not supplied"
    assert runner_select._kraken_secondary(None, None, pin, ml) is None
    raw = {**fam, V.KRAKEN: (ml, ml)}
    with pytest.raises(ValueError, match="kraken.comparison"):
        runner_select._kraken_secondary(raw, None, pin, ml)  # a raw Kraken pair never enters
    with pytest.raises(ValueError, match="secondary"):
        runner_select._kraken_secondary(None, c, pin, ml)
    with pytest.raises(ValueError, match="issued"):
        runner_select._kraken_secondary(fam, object(), pin, ml)
    with pytest.raises(ValueError, match="opening"):
        runner_select._kraken_secondary(fam, c, {**pin, "opening_id": "0" * 64}, ml)
    with pytest.raises(ValueError, match="ML\\*"):
        runner_select._kraken_secondary(fam, c, pin, funding_run(_nets(0.001, 2), arm="A2", h=72))


def _family(kraken_pair):
    a0 = funding_run(_nets(0.0, 12), arm="A0", h=72)
    weak = [funding_run(_nets(0.0, 20 + i), arm="A2", h=72) for i in range(10)]
    pairs = {name: (weak[2 * i], weak[2 * i + 1]) for i, name in enumerate(V.SECONDARY[:5])}
    pairs["A1 vs A0"] = (funding_run(_nets(0.0005, 11), arm="A1", h=72), a0)
    pairs[V.KRAKEN] = kraken_pair
    return pairs


def test_holm_over_exactly_6_includes_comparison_5_when_present():
    ml = funding_run(_nets(0.004, 31), arm="A2", h=72)
    kr = funding_run(_nets(-0.004, 32), arm="A2", h=72)  # an extreme Kraken result
    sec, _ = V.secondary_family(_family((ml, kr)), None, 2000, 7)
    raw = {n: sec[n]["p"] for n in V.SECONDARY}
    assert len(raw) == 6 and sec[V.KRAKEN]["available"] and raw[V.KRAKEN] == pytest.approx(1 / 2001)
    assert {n: sec[n]["p_holm"] for n in V.SECONDARY} == stats.holm(raw)  # m = 6 with #5 in it
    assert sec[V.KRAKEN]["p_holm"] == pytest.approx(6 / 2001) and sec[V.KRAKEN]["rejects"]
    assert sec[V.KRAKEN]["label"] == V.EXPLORATORY
    void, _ = V.secondary_family(_family(V.Unavailable("STOPPED (AMB-41)")), None, 2000, 7)
    assert not void[V.KRAKEN]["available"] and void[V.KRAKEN]["why"] == "STOPPED (AMB-41)"
    assert void[V.KRAKEN]["label"] == V.EXPLORATORY and void[V.KRAKEN]["p_holm"] is None
    rv = {n: void[n]["p"] for n in V.SECONDARY}
    assert rv[V.KRAKEN] is None
    assert {n: void[n]["p_holm"] for n in V.SECONDARY} == stats.holm(rv)  # still m = 6
    assert all("label" not in sec[n] for n in V.SECONDARY if n != V.KRAKEN)


def test_comparison_5_never_changes_the_class_criteria_or_reasons():
    base = _inputs()
    ml = funding_run(_nets(0.004, 31), arm="A2", h=72)
    verdicts = []
    for kp in (V.Unavailable("STOPPED (AMB-41)"),
               (ml, funding_run(_nets(-0.004, 33), arm="A2", h=72)),  # extreme in either direction
               (ml, funding_run(_nets(0.008, 34), arm="A2", h=72)), None):
        x = replace(base, secondary=_family(kp))
        if isinstance(kp, tuple):  # the pure public path refuses a raw Kraken pair (round 1, item 4) ...
            with pytest.raises(ValueError, match="kraken"):
                V.holdout(x)
            verdicts.append(V._holdout(x))  # ... the computation holdout_verdict runs is checked directly
        else:
            verdicts.append(V.holdout(x))
    ref = V.holdout(base)
    for v in verdicts:
        assert (v.cls, v.criteria, v.reasons, v.applicable, v.p1_em) == (ref.cls, ref.criteria, ref.reasons,
                                                                         ref.applicable, ref.p1_em)
        assert {k: v.details[k] for k in ref.details if k.startswith("p")} == \
            {k: ref.details[k] for k in ref.details if k.startswith("p")}
    assert verdicts[1].secondary[V.KRAKEN]["rejects"] and verdicts[2].secondary[V.KRAKEN]["available"]
    assert not verdicts[2].secondary[V.KRAKEN]["rejects"]
    assert verdicts[0].secondary[V.KRAKEN]["available"] is False
    assert LO < kraken.WINDOW[0]  # the synthetic runs are W_DEV-dated plumbing (the W_HOLD gate is elsewhere)


def test_pure_verdict_scans_the_secondary_pairs_for_w_hold_and_fails_closed():
    base = _inputs()
    fam = _family(None)
    hold_run = funding_run(_nets(0.0, 40), arm="A2", h=72, lo=V.windows.W_HOLD[0])
    with pytest.raises(ValueError, match="W_HOLD"):
        V.holdout(replace(base, secondary={**fam, "ML* E_M vs E_T": (fam["ML* E_M vs E_T"][0], hold_run)}))
    with pytest.raises(ValueError, match="W_HOLD"):  # an accounted Ledger is scanned too
        V.holdout(replace(base, secondary={**fam, "A1 vs A0": (fam["A1 vs A0"][0], account(hold_run))}))
    for bad in ("x", (object(), None), [fam["A1 vs A0"][0], fam["A1 vs A0"][1]]):
        with pytest.raises(ValueError, match="fail closed"):
            V.holdout(replace(base, secondary={**fam, "A1 vs A0": bad}))


def test_comparison_5_pairs_on_the_common_days():
    """AMB-41 (f): both series restricted to their common days (S-13.1 "same days"); other members keep the
    strict alignment."""
    ml = funding_run(_nets(0.004, 31), arm="A2", h=72)
    kr = funding_run(_nets(-0.004, 32)[:90], arm="A2", h=72, lo=LO + 30 * D)  # Kraken starts later
    sec, _ = V.secondary_family(_family((ml, kr)), None, 2000, 7)
    la, lb = account(ml), account(kr)
    da, db = stats.ledger_days(la), stats.ledger_days(lb)
    keep_a, keep_b = np.isin(da, db), np.isin(db, da)
    assert keep_a.sum() == 90 and keep_b.all()
    ra, rb = stats.ledger_returns(la)[keep_a], stats.ledger_returns(lb)[keep_b]
    p = stats.p_max_over_blocks(ra - rb, stats.BLOCKS, 2000, 7)
    assert sec[V.KRAKEN]["available"] and sec[V.KRAKEN]["p"] == p
    assert sec[V.KRAKEN]["mean_x"] == pytest.approx(float(np.mean(ra - rb)))
    other = _family(None)
    other["ML* E_M vs E_T"] = (ml, kr)  # not #5: misaligned days stay "not comparable"
    assert V.secondary_family(other, None, 2000, 7)[0]["ML* E_M vs E_T"]["why"].startswith("not comparable")
    tiny = funding_run(_nets(0.0, 3)[:1], arm="A2", h=72, lo=LO + 200 * D)  # no common day
    assert V.secondary_family(_family((ml, tiny)), None, 2000, 7)[0][V.KRAKEN]["why"].startswith(
        "not comparable")


def test_after_the_verdict_nothing_kraken_opens(tmp_path, monkeypatch, log):
    """Round 1, item 2: once the W_HOLD verdict marker exists, authorize_kraken and every use of an existing
    KrakenAuthorization are refused, each with an L11 K9 entry."""
    o = af.authorize(tmp_path / "op", monkeypatch)  # a private opening: the verdict closes it for good
    root = tmp_path / "frozen"
    monkeypatch.setattr(kraken, "V5_MANIFEST_SHA256", build_copy(root))
    ka = kraken.authorize_kraken(o.auth, *WIN, root)
    guard.mark_verdict_produced(o.auth, "d" * 64)
    n0 = len(log.records())
    _refused(log, lambda: kraken.authorize_kraken(o.auth, *WIN, root), guard.HoldoutBlocked, "S-1.7")
    _refused(log, lambda: kraken.load_symbol(ka, "ETH", *WIN), guard.HoldoutBlocked, "S-1.7")
    _refused(log, lambda: kraken.comparison(ka, _ml_run()), guard.HoldoutBlocked, "S-1.7")
    recs = log.records()[n0:]
    assert len(recs) == 3 and all(r["rule"].startswith("L11 Kraken outcome guard") for r in recs)
    assert all("verdict" in r["reason"] for r in recs)


def test_any_unexpected_failure_is_an_l11_refusal(copy, log, opened, monkeypatch):
    """Round 1, item 3: fail closed, every failure inside the guard / loader is an L11 refusal, K9-logged."""
    def boom(*a, **k):
        raise ZeroDivisionError("unexpected")

    ka = kraken.authorize_kraken(opened.auth, *WIN, copy)
    monkeypatch.setattr(kraken, "_spreads", boom)
    _refused(log, lambda: kraken.load_symbol(ka, "ETH", *WIN), ZeroDivisionError, "input verification")
    monkeypatch.setattr(kraken, "verify_copy", boom)
    _refused(log, lambda: kraken.authorize_kraken(opened.auth, *WIN, copy), ZeroDivisionError,
             "input verification")


def test_only_the_root_manifest_is_exempt(copy, log, opened):
    (copy / "bars" / kraken.MANIFEST).write_text("{}", encoding="utf-8")  # a nested manifest.json is unlisted
    _refused(log, lambda: kraken.authorize_kraken(opened.auth, *WIN, copy), kraken.FrozenCopyMismatch,
             "manifest")
