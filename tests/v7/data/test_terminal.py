import pandas as pd
import pytest

from luna.v7.data.instances import Instance
from luna.v7.data.notices import NoticeVersion
from luna.v7.data.schema import IntegrityBlocker, validate
from luna.v7.data.terminal import R12_EMPTY_OK, TerminalEvent, classify, reject_approximate, validate_r12
from luna.v7.timeutil import MIN, D, to_ns

SCHED = to_ns("2024-06-11 09:00")
TXT = "Binance Futures: settlement on the USDⓈ-M XXXUSDT Perpetual Contract at 2024-06-11 09:00 (UTC)."
NOTICE = NoticeVersion("n", 1, "delisting", SCHED - D, SCHED - D, TXT, "h")
INST = Instance(0, SCHED - 100 * D, SCHED, SCHED + 7 * D)


def _row(**kw):
    r = {
        "venue": "binance", "contract_instance_id": "XXXUSDT#0", "settlement_ts": SCHED,
        "settlement_price": 1.2345, "source": "official_historical", "source_timestamp": SCHED,
        "known_at": SCHED, "retrieved_at": SCHED, "sha256": "abc",
    }
    r.update(kw)
    return r


def _ev(cess=SCHED):
    return TerminalEvent(INST, cess, "C", None, None, None)


def test_terminal_class_a():
    e = classify(_ev(), NOTICE, _row(), "XXXUSDT")
    assert (e.klass, e.settle_ts, e.settle_price) == ("A", SCHED, 1.2345)


def test_terminal_class_b_c():
    b = classify(_ev(), NOTICE, None, "XXXUSDT")  # time known, no exact price
    assert (b.klass, b.settle_ts, b.settle_price) == ("B", SCHED, None)
    assert classify(_ev(SCHED + 15 * MIN), NOTICE, None, "XXXUSDT").klass == "B"
    c_none = classify(_ev(), None, None, "XXXUSDT")
    assert c_none.klass == "C" and c_none.settle_ts is None
    c_late = classify(_ev(SCHED + 15 * MIN + 1), NOTICE, _row(), "XXXUSDT")  # conflict
    assert c_late.klass == "C" and c_late.settle_price is None
    assert classify(_ev(SCHED - 1), NOTICE, _row(), "XXXUSDT").klass == "C"
    assert classify(_ev(), NOTICE, None, "YYYUSDT").klass == "C"  # notice does not match this symbol
    post = NoticeVersion("n", 2, "postponement", 1, 1, TXT, "h")
    assert classify(_ev(), post, None, "XXXUSDT").klass == "C"


def test_row_for_other_time_is_not_a():
    assert classify(_ev(), NOTICE, _row(settlement_ts=SCHED + 60 * MIN), "XXXUSDT").klass == "B"


def test_r12_rejects_approximate_rows():
    reject_approximate(_row())
    for bad in (
        _row(source="kline_1m_close"), _row(source="approx"), _row(settlement_price=float("nan")),
        _row(settlement_price=0.0), _row(sha256=""), _row(known_at=None),
    ):
        with pytest.raises(IntegrityBlocker):
            reject_approximate(bad)
    with pytest.raises(IntegrityBlocker):
        classify(_ev(), NOTICE, _row(source="synthetic"), "XXXUSDT")


def test_r12_empty_is_valid():
    assert R12_EMPTY_OK
    cols = ["venue", "contract_instance_id", "settlement_ts", "settlement_price", "source",
            "source_timestamp", "known_at", "retrieved_at", "sha256"]
    empty = pd.DataFrame(columns=cols)
    validate("R12", empty)
    validate_r12(empty)
    validate_r12(pd.DataFrame([_row()]))
    with pytest.raises(IntegrityBlocker):
        validate_r12(pd.DataFrame([_row(source="approx")]))


def test_notice_without_known_at_is_c():
    cur = NoticeVersion("n", 1, "delisting", SCHED - D, None, TXT, "h")
    assert classify(_ev(), cur, _row(), "XXXUSDT").klass == "C"


def test_row_bound_to_instance():
    with pytest.raises(IntegrityBlocker):
        classify(_ev(), NOTICE, _row(contract_instance_id="XXXUSDT#1"), "XXXUSDT")
    with pytest.raises(IntegrityBlocker):
        classify(_ev(), NOTICE, _row(venue="other"), "XXXUSDT")


def test_non_numeric_price_is_blocker():
    with pytest.raises(IntegrityBlocker):
        reject_approximate(_row(settlement_price="n/a"))
