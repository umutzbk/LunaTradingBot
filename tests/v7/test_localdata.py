"""Task 25 (SC-03): W_HOLD / M6 guards on local data (files and frames) with K9 logging, read-only opens, and
the K9 record of the outcome guard (labels, engine). Synthetic files in tmp_path only; never var/."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from luna.v7 import guard, k9, localdata, windows
from luna.v7.forward_store import SealedBeforeL7
from luna.v7.timeutil import MIN, D, to_ns
from tests.v7 import auth_fixtures as af

HOLD_LO = windows.W_HOLD[0]
M6_LO = windows.W_HOLD[1]
DEV_T = to_ns("2024-03-01")


@pytest.fixture
def log(tmp_path):
    lg = k9.K9Log(tmp_path / "k9")
    with k9.using(lg):
        yield lg


@pytest.fixture(scope="module")
def _opening(tmp_path_factory):
    return af.opening_once(tmp_path_factory.mktemp("opening"))


@pytest.fixture
def opened(_opening, monkeypatch):
    """The module's opening, ``guard._roots`` pointed at it for this test only (review m-7)."""
    return af.use_opening(monkeypatch, _opening)


def test_k9_log_record_shape_and_incident(log):
    e = guard.HoldoutBlocked("x")
    k9.log_refusal(e, request={"kind": "t"}, rule="S-1.7 W_HOLD seal")
    (r,) = log.records()
    assert set(r) >= {"utc", "request", "rule", "reason", "exception", "sha256"}
    assert r["reason"] == "x" and r["exception"] == "HoldoutBlocked" and r["request"] == {"kind": "t"}
    (inc,) = log.incidents()
    assert r["sha256"] in inc.read_text(encoding="utf-8")


def test_guards_refuse_to_run_without_a_k9_log(tmp_path):
    """Review I-3: fail closed. Every guard / local-data entry refuses (K9NotConfigured) without a log, for
    an allowed span too; a refusal reaching ``log_refusal`` without a log is still refused."""
    f = tmp_path / "BTCUSDT-15m-2025-06.zip"
    f.write_bytes(b"x")
    df = pd.DataFrame(dict(open_ts=[DEV_T]))
    with k9.using(None):
        with pytest.raises(k9.K9NotConfigured):
            k9.require()
        for call in (lambda: guard.check_data_span(DEV_T, DEV_T + D),
                     lambda: guard.check_outcome_window(DEV_T, DEV_T + D, now_ns=to_ns("2026-10-15")),
                     lambda: guard.check_listing_only(str(f), access="content"),
                     lambda: localdata.check_file(f), lambda: localdata.read_bytes(f),
                     lambda: localdata.check_frame(df, ("open_ts",))):
            with pytest.raises(k9.K9NotConfigured):
                call()
        with pytest.raises(k9.K9NotConfigured) as ei:
            k9.log_refusal(guard.HoldoutBlocked("x"), request={}, rule="r")
        assert isinstance(ei.value.__cause__, guard.HoldoutBlocked)
    guard.check_listing_only(str(f), access="listing")  # listings need no log decision


def test_unwritable_k9_log_is_noted_and_the_refusal_still_raised(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with k9.using(k9.K9Log(blocker / "k9")), pytest.raises(guard.HoldoutBlocked) as ei:
        guard.check_data_span(HOLD_LO, HOLD_LO + D)
    assert any("K9" in n and "not written" in n for n in ei.value.__notes__)


# ----------------------------------------------------------------------------------- check_data_span


def test_data_span_reaching_w_hold_or_m6_is_refused_and_logged(log):
    guard.check_data_span(DEV_T, HOLD_LO)  # W_DEV: unaffected, nothing logged
    assert log.records() == []
    with pytest.raises(guard.HoldoutBlocked):
        guard.check_data_span(HOLD_LO - D, HOLD_LO + 1, what="bars15")
    with pytest.raises(guard.EmbargoBlocked):
        guard.check_data_span(M6_LO, M6_LO + D)
    recs = log.records()
    assert [r["exception"] for r in recs] == ["HoldoutBlocked", "EmbargoBlocked"]
    assert recs[0]["request"] == {"kind": "data_span", "what": "bars15", "lo": HOLD_LO - D, "hi": HOLD_LO + 1}
    assert recs[0]["rule"].startswith("S-1.7") and recs[1]["rule"].startswith("S-1.3")
    with pytest.raises(ValueError):
        guard.check_data_span(HOLD_LO, HOLD_LO)


def test_data_span_with_the_hardened_authorization(opened, log):
    guard.check_data_span(HOLD_LO, M6_LO, auth=opened.auth)  # W_HOLD after the opening
    with pytest.raises(guard.EmbargoBlocked):  # M6 stays refused until the canonical R_V6
        guard.check_data_span(M6_LO - D, M6_LO + 1, auth=opened.auth)
    for forged in (object(), object.__new__(guard.HoldoutAuthorization), True):
        with pytest.raises(guard.HoldoutBlocked):
            guard.check_data_span(HOLD_LO, HOLD_LO + D, auth=forged)  # type: ignore[arg-type]


def test_outcome_guard_refusals_are_k9_logged(log):
    with pytest.raises(guard.HoldoutBlocked):
        guard.check_outcome_window(HOLD_LO, HOLD_LO + D, now_ns=to_ns("2026-10-15"))
    (r,) = log.records()
    assert r["request"]["kind"] == "outcome_window" and r["rule"].startswith("S-1.7")


def test_forward_seal_refusal_is_k9_logged(log, monkeypatch):
    def sealed(*a, **k):
        raise SealedBeforeL7("W_FWD7 before L7")

    monkeypatch.setattr(guard, "_check_forward_seal", sealed)
    with pytest.raises(SealedBeforeL7):
        guard.check_data_span(DEV_T, DEV_T + D)
    assert log.records()[0]["rule"].startswith("§15")


# ---------------------------------------------------------------------------------------- files


def _file(tmp_path, name: str, data: bytes = b"raw") -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_file_content_is_guarded_by_its_period(tmp_path, log):
    dev = _file(tmp_path, "BTCUSDT-15m-2025-06.zip")
    assert localdata.check_file(dev) == (to_ns("2025-06-01"), to_ns("2025-07-01"))
    with localdata.open_readonly(dev) as f:
        assert f.read() == b"raw" and f.mode == "rb"
        with pytest.raises(OSError):
            f.write(b"x")  # type: ignore[arg-type]
    assert localdata.read_bytes(dev) == b"raw"
    for name, exc in (("BTCUSDT-15m-2025-07.zip", guard.HoldoutBlocked),
                      ("BTCUSDT-15m-2025-06-30.zip.CHECKSUM", None),
                      ("BTCUSDT-fundingRate-2026-07.zip", guard.EmbargoBlocked),
                      ("bars.parquet", guard.HoldoutBlocked)):  # no period in the name: fail closed
        p = _file(tmp_path, name)
        if exc is None:
            localdata.check_file(p)
            continue
        with pytest.raises(exc):
            localdata.open_readonly(p)
        with pytest.raises(exc):
            localdata.read_bytes(p)
    assert len(log.records()) == 6
    assert all(r["request"]["kind"] in ("data_span", "file") for r in log.records())


def test_w_hold_file_after_the_opening(opened, tmp_path, log):
    p = _file(tmp_path, "BTCUSDT-15m-2025-08.zip")
    assert localdata.read_bytes(p, auth=opened.auth) == b"raw"
    with pytest.raises(guard.EmbargoBlocked):
        localdata.read_bytes(_file(tmp_path, "BTCUSDT-15m-2026-07.zip"), auth=opened.auth)


# --------------------------------------------------------------------------------------- frames


def test_frame_with_a_w_hold_row_is_refused(log):
    ts = np.array([DEV_T, DEV_T + 15 * MIN], dtype=np.int64)
    df = pd.DataFrame(dict(open_ts=ts, known_at=ts + 16 * MIN))
    assert localdata.frame_span(df, ("open_ts",), "bars") == (DEV_T, DEV_T + 15 * MIN + 1)
    localdata.check_frame(df, ("open_ts",), what="bars")
    bad = pd.concat([df, pd.DataFrame(dict(open_ts=[HOLD_LO], known_at=[HOLD_LO + MIN]))], ignore_index=True)
    with pytest.raises(guard.HoldoutBlocked):
        localdata.check_frame(bad, ("open_ts",), what="bars")
    assert log.records()[-1]["request"]["what"] == "bars"
    assert localdata.frame_span(df.iloc[:0], ("open_ts",), "bars") is None
    localdata.check_frame(df.iloc[:0], ("open_ts",), what="bars")  # empty: nothing to guard
    with pytest.raises(ValueError):  # an unparseable timestamp cannot establish the span: fail closed
        localdata.frame_span(pd.DataFrame(dict(open_ts=[1.5])), ("open_ts",), "bars")
    with pytest.raises(ValueError, match="missing"):
        localdata.frame_span(df, ("ts",), "bars")
    na = pd.DataFrame(dict(open_ts=pd.array([DEV_T, None], dtype="Int64")))
    assert localdata.frame_span(na, ("open_ts",), "bars") == (DEV_T, DEV_T + 1)


def test_k9_records_are_json_lines(log):
    with pytest.raises(guard.HoldoutBlocked):
        guard.check_data_span(HOLD_LO, HOLD_LO + 1)
    lines = (log.log_dir / k9.REFUSALS).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["request"]["lo"] == HOLD_LO
