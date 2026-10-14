from luna.v7.data.instances import Instance
from luna.v7.data.notices import NoticeVersion, U6State, confident_match, known_at, scheduled_ts
from luna.v7.timeutil import D, H, to_ns

T0 = to_ns("2024-06-01")
SCHED = to_ns("2024-06-11 09:00")
CTX = "Binance Futures will delist the USDⓈ-M {s} Perpetual Contract. "
AT = "Binance Futures will execute automatic settlement on the USDⓈ-M {s} Perpetual Contract at {t}."


def _v(text, kind="delisting", release=T0 + 5 * D, capture=T0 + 6 * D, nid="n1", ver=1, recv=None):
    return NoticeVersion(nid, ver, kind, release, capture, text, "h", recv)


def _txt(s="XXXUSDT", t="2024-06-11 09:00 (UTC)"):
    return AT.format(s=s, t=t)


INSTS = [Instance(0, T0, None, None)]


def never(ts):
    return False


def test_known_at_is_max_release_capture():
    assert known_at(_v("x", release=10, capture=5)) == 10
    assert known_at(_v("x", release=10, capture=50)) == 50
    assert known_at(_v("x", release=10, capture=None, recv=99)) == 99


def test_current_text_never_backdated():
    v = _v(_txt(), capture=None)
    assert known_at(v) is None
    st = U6State([v], "XXXUSDT", INSTS)
    assert not st.active(INSTS[0], T0 + 30 * D, never)


def test_whole_token_match_only():
    assert confident_match(_v(_txt()), "XXXUSDT")
    assert not confident_match(_v(_txt("1000XXXUSDT")), "XXXUSDT")
    assert confident_match(_v(_txt("1000XXXUSDT")), "1000XXXUSDT")
    assert not confident_match(_v(_txt("XXXUSDTM")), "XXXUSDT")
    assert not confident_match(
        _v("The XXX USDT-Margined Contract will be delisted at 2024-06-11 09:00 (UTC)."), "XXXUSDT"
    )
    assert not confident_match(_v("XXXUSDT spot pair will be delisted."), "XXXUSDT")  # no futures context
    assert not confident_match(_v(_txt() + " COIN-M XXXUSD_PERP also."), "XXXUSDT")  # ambiguous context
    assert confident_match(_v("Binance Futures lists XXXUSDT Perpetual Contract."), "XXXUSDT")


def test_scheduled_single_and_am_pm():
    assert scheduled_ts(_v(_txt()), "XXXUSDT") == SCHED
    assert scheduled_ts(_v(_txt(t="2024-06-11 09:00 AM (UTC)")), "XXXUSDT") == SCHED
    assert scheduled_ts(_v(_txt(t="2024-06-11 09:00 PM (UTC)")), "XXXUSDT") == SCHED + 12 * H
    assert scheduled_ts(_v(_txt(t="2024-06-11 09:00")), "XXXUSDT") is None  # no explicit (UTC)
    assert scheduled_ts(_v("The XXXUSDT USDⓈ-M contract will be delisted soon."), "XXXUSDT") is None


def test_multiple_times_need_explicit_mapping():
    base = "Binance Futures USDⓈ-M Perpetual Contracts. "
    amb = _v(base + "XXXUSDT settles at 2024-06-11 09:00 (UTC) and YYYUSDT at 2024-06-12 09:00 (UTC).")
    assert scheduled_ts(amb, "XXXUSDT") is None
    resp = _v(
        base + "Settlement for XXXUSDT and YYYUSDT at 2024-06-11 09:00 (UTC) and "
        "2024-06-12 09:00 (UTC) respectively."
    )
    assert scheduled_ts(resp, "XXXUSDT") == SCHED
    assert scheduled_ts(resp, "YYYUSDT") == SCHED + D
    bad = _v(
        base + "Settlement for XXXUSDT, YYYUSDT, ZZZUSDT at 2024-06-11 09:00 (UTC) and "
        "2024-06-12 09:00 (UTC) respectively."
    )
    assert scheduled_ts(bad, "XXXUSDT") is None
    two = _v(base + "XXXUSDT settles at 2024-06-11 09:00 (UTC). XXXUSDT reopens at 2024-06-13 09:00 (UTC).")
    assert scheduled_ts(two, "XXXUSDT") is None
    one_for_all = _v(base + "XXXUSDT, YYYUSDT and ZZZUSDT settle at 2024-06-11 09:00 (UTC).")
    assert scheduled_ts(one_for_all, "YYYUSDT") == SCHED


def test_postponement_deactivates():
    ka = T0 + 6 * D
    notice = _v(_txt())
    post = _v(
        CTX.format(s="XXXUSDT") + "Postponed.", kind="postponement", release=T0 + 7 * D,
        capture=T0 + 8 * D, nid="n2",
    )
    st = U6State([notice, post], "XXXUSDT", INSTS)
    i = INSTS[0]
    assert not st.active(i, ka - 1, never)
    assert st.active(i, ka, never)
    assert st.active(i, T0 + 8 * D - 1, never)
    assert not st.active(i, T0 + 8 * D, never)
    redo = _v(_txt(), release=T0 + 9 * D, capture=T0 + 9 * D, nid="n3")
    assert U6State([notice, post, redo], "XXXUSDT", INSTS).active(i, T0 + 9 * D, never)


def test_72h_clear_only_with_unique_time():
    i = INSTS[0]
    st = U6State([_v(_txt())], "XXXUSDT", INSTS)
    clear = SCHED + 72 * H
    assert st.active(i, clear - 1, never)
    assert not st.active(i, clear, never)  # never halted -> cleared
    assert st.active(i, clear + D, lambda ts: ts >= SCHED + H)  # halted inside the window -> stays
    null_sched = U6State([_v(CTX.format(s="XXXUSDT"))], "XXXUSDT", INSTS)
    assert null_sched.active(i, SCHED + 30 * D, never)  # null time: only postponement clears


def test_late_known_at_evaluates_72h_at_known_at():
    i = INSTS[0]
    late_ka = SCHED + 80 * H
    late = _v(_txt(), release=T0 + D, capture=late_ka)
    assert not U6State([late], "XXXUSDT", INSTS).active(i, late_ka + D, never)  # passed: never activates
    assert U6State([late], "XXXUSDT", INSTS).active(i, late_ka + D, lambda ts: ts >= SCHED)


def test_u6_ends_with_instance():
    insts = [Instance(0, T0, SCHED, SCHED + 7 * D), Instance(1, SCHED + 20 * D, None, None)]
    st = U6State([_v(_txt())], "XXXUSDT", insts)

    def halted(ts):
        return True

    assert st.active(insts[0], SCHED + 7 * D - 1, halted)
    assert not st.active(insts[0], SCHED + 7 * D, halted)
    assert not st.active(insts[1], SCHED + 21 * D, halted)  # does not carry into the relisting


def test_match_binds_to_instance_live_at_known_at():
    insts = [Instance(0, T0, T0 + 2 * D, T0 + 9 * D), Instance(1, T0 + 20 * D, None, None)]
    v = _v(_txt(), release=T0 + 21 * D, capture=T0 + 21 * D)
    st = U6State([v], "XXXUSDT", insts)

    def halted(ts):
        return True

    assert st.active(insts[1], T0 + 22 * D, halted)
    assert not st.active(insts[0], T0 + 22 * D, halted)
