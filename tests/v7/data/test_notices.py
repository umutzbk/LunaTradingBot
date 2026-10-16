from dataclasses import replace

from luna.v7.data.instances import Instance
from luna.v7.data.notices import (
    InstanceBases,
    NoticeVersion,
    U6State,
    confident_match,
    known_at,
    match_reason,
    scheduled_ts,
)
from luna.v7.timeutil import D, H, to_ns
from luna.v7.universe import Identity

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


def test_each_version_evaluated_by_itself():
    i = INSTS[0]
    v1 = _v(CTX.format(s="XXXUSDT"), nid="a")  # null time: only postponement clears
    v2 = _v(_txt(), nid="b", release=T0 + 7 * D, capture=T0 + 7 * D)
    st = U6State([v1, v2], "XXXUSDT", INSTS)
    assert st.active(i, SCHED + 30 * D, never)  # v1 still in force after v2's 72 h
    t1 = _v(_txt(t="2024-06-11 09:00 (UTC)"), nid="c")
    t2 = _v(_txt(t="2024-06-20 09:00 (UTC)"), nid="d", release=T0 + 7 * D, capture=T0 + 7 * D)
    st2 = U6State([t1, t2], "XXXUSDT", INSTS)
    assert st2.active(i, SCHED + 80 * H, lambda ts: ts >= to_ns("2024-06-20"))  # t1 cleared? t1 halted-test
    only_t2 = to_ns("2024-06-20 09:00") + 72 * H
    assert not st2.active(i, only_t2, never)
    post = _v(CTX.format(s="XXXUSDT") + "Cancelled.", kind="cancellation", nid="e",
              release=T0 + 8 * D, capture=T0 + 8 * D)
    assert not U6State([v1, v2, post], "XXXUSDT", INSTS).active(i, T0 + 9 * D, never)


def test_seconds_exact_and_token_continuation():
    assert scheduled_ts(_v(_txt(t="2024-06-11 09:00:30 (UTC)")), "XXXUSDT") == SCHED + 30 * 10**9
    assert not confident_match(_v(_txt("XXXUSDT_240628")), "XXXUSDT")
    assert not confident_match(_v(_txt("XXXUSDT-240628")), "XXXUSDT")
    assert confident_match(_v(_txt("XXXUSDT") + " Also XXXUSDT."), "XXXUSDT")


def test_version_in_first_16_min_binds_to_new_instance_used_once_current():
    """DK-108: a version known inside the new instance's first 16 min is attributed to it (not dropped), and
    U6 counts only once that instance is current (first_bar_ts + 15 min <= t - 1 min)."""
    first = T0 + 20 * D
    insts = [Instance(0, T0, T0 + 2 * D, T0 + 9 * D), Instance(1, first, None, None)]
    v = _v(_txt(t="2024-07-01 09:00 (UTC)"), release=first + 5 * 60 * 10**9, capture=first + 5 * 60 * 10**9)
    st = U6State([v], "XXXUSDT", insts)
    assert st.active(insts[1], first + 16 * 60 * 10**9, never)
    assert not st.active(insts[1], first + 16 * 60 * 10**9 - 1, never)


# ------------------------------------------------------------- A-1 (AMENDMENTS-2026-10-16): base-asset match


KA = T0 + 6 * D  # known_at of _v's default version
AKRO = ("Binance Futures Will Delist AKRO and DODO USDT-Margined Contracts\n"
        "Binance Futures will close all positions and remove the AKRO and DODO contracts at "
        "2024-06-11 09:00 (UTC).")


def _id(base, quote="USDT", ok=True):
    return Identity(base, "PERPETUAL", quote, "COIN", 1.0, ok)


def _bases(**syms):
    """symbol -> (first_bar_ts, identity) of one instance (idx 0)."""
    insts = {s: [Instance(0, fb, None, None)] for s, (fb, _) in syms.items()}
    ids = {s: {0: ident} for s, (_, ident) in syms.items()}
    return InstanceBases(insts, ids)


def test_a1_base_asset_match():
    b = _bases(AKROUSDT=(T0, _id("AKRO")), DODOUSDT=(T0, _id("DODO")), AKROUSDC=(T0, _id("AKRO", "USDC")))
    v = _v(AKRO)
    assert match_reason(v, "AKROUSDT", b) == (True, "base_asset_match:AKRO;usdm_context:usdt-margined")
    assert match_reason(v, "DODOUSDT", b)[0]
    assert not confident_match(v, "AKROUSDT")  # DEC-40 alone (no base resolver, nothing carried): no match
    assert match_reason(v, "AKROUSDC", b) == (False, "no_symbol_token;base_asset:no_live_usdt_identity")


def test_a1_multiplier_base_is_its_own_token():
    b = _bases(**{"1000BTTCUSDT": (T0, _id("1000BTTC")), "BTTCUSDT": (T0, _id("BTTC"))})
    v = _v("Binance Futures Will Delist 1000BTTC and YFII USDT-Margined Contracts")
    assert match_reason(v, "1000BTTCUSDT", b)[0]
    assert match_reason(v, "BTTCUSDT", b) == (False, "no_symbol_token;base_asset:no_base_token:BTTC")


def test_a1_ambiguous_base_gives_no_match():
    """Two live USD-M USDT instances with base AKRO at known_at (a perpetual and a delivery contract)."""
    b = _bases(AKROUSDT=(T0, _id("AKRO")), AKROUSDT_240628=(T0, _id("AKRO")))
    assert match_reason(_v(AKRO), "AKROUSDT", b) == (False, "no_symbol_token;base_asset_ambiguous:AKRO:2")


def test_a1_coin_m_veto_still_applies():
    b = _bases(AKROUSDT=(T0, _id("AKRO")))
    v = _v(AKRO + " COIN-M AKROUSD_PERP is not affected.")
    assert match_reason(v, "AKROUSDT", b) == (False, "base_asset:AKRO;coin_m_veto:coin-m")
    assert match_reason(_v("Binance Will Delist AKRO spot pairs."), "AKROUSDT", b) == (
        False, "base_asset:AKRO;no_usdm_context")


def test_a1_pit_instance_not_yet_known_cannot_match():
    """Only instances live at the version's known_at count: a later instance can neither be matched nor make
    the base ambiguous; an unverified identity never matches."""
    later = _bases(AKROUSDT=(KA + 1, _id("AKRO")))
    assert match_reason(_v(AKRO), "AKROUSDT", later) == (
        False, "no_symbol_token;base_asset:no_live_usdt_identity")
    b = _bases(AKROUSDT=(T0, _id("AKRO")), AKROUSDT_240628=(KA + 1, _id("AKRO")))
    assert match_reason(_v(AKRO), "AKROUSDT", b)[0]  # the delivery contract starts after known_at
    unverified = _bases(AKROUSDT=(T0, _id("AKRO", ok=False)))
    assert not match_reason(_v(AKRO), "AKROUSDT", unverified)[0]
    assert not match_reason(_v(AKRO, capture=None), "AKROUSDT", b)[0]  # no known_at: never matched


def test_a1_carried_base_match_drives_u6_and_keeps_dec37():
    """The ingest decides the base match (PIT, with the identity set) and carries it on the version
    (``base_matched``); U6 uses it; scheduled_ts is unchanged (symbol-token sentences only: null here)."""
    v = replace(_v(AKRO), base_matched=("AKROUSDT",))
    assert confident_match(v, "AKROUSDT") and not confident_match(v, "DODOUSDT")
    assert match_reason(v, "AKROUSDT")[1] == "base_asset_match:carried;usdm_context:usdt-margined"
    assert scheduled_ts(v, "AKROUSDT") is None
    st = U6State([v], "AKROUSDT", INSTS)
    assert not st.active(INSTS[0], KA - 1, never) and st.active(INSTS[0], KA + 30 * D, never)


# ------------------------------------------------- A-5 (AMENDMENTS-2026-10-16, AMB-46): same-sentence context


def test_a5_base_in_pair_sentence_context_elsewhere_gives_no_match():
    """BTC named only as a pair / example; the USDⓈ-M phrase is in another sentence: no base match."""
    b = _bases(BTCUSDT=(T0, _id("BTC")), AKROUSDT=(T0, _id("AKRO")))
    v = _v("Binance Futures Will Delist AKRO USDT-Margined Contracts\n"
           "Users can convert their AKRO into BTC before the deadline.")
    assert match_reason(v, "BTCUSDT", b) == (False, "base_asset:BTC;no_same_sentence_usdm_context")
    assert match_reason(v, "AKROUSDT", b) == (True, "base_asset_match:AKRO;usdm_context:usdt-margined")
    # the sentence split is DEC-37's _SENT_RE (".", ";", "!", "?" + whitespace, or a newline)
    v2 = _v("The AKRO USDT-margined perpetual will be removed. Trading pairs such as AKRO/BTC stay open.")
    assert not match_reason(v2, "BTCUSDT", b)[0] and match_reason(v2, "AKROUSDT", b)[0]


def test_a5_base_and_context_in_same_sentence_match():
    b = _bases(AKROUSDT=(T0, _id("AKRO")))
    v = _v("Notice of Futures Changes\nBinance Futures will remove the AKRO USDⓈ-M perpetual contract.")
    assert match_reason(v, "AKROUSDT", b) == (True, "base_asset_match:AKRO;usdm_context:usds-m")
    # the "perpetual contract" + "futures" pair counts only with both words in the base's sentence
    pair = _v("Binance Futures update.\nThe AKRO perpetual contract will be settled.")
    assert match_reason(pair, "AKROUSDT", b) == (False, "base_asset:AKRO;no_same_sentence_usdm_context")
    pair2 = _v("Update.\nBinance Futures will settle the AKRO perpetual contract.")
    assert match_reason(pair2, "AKROUSDT", b) == (
        True, "base_asset_match:AKRO;usdm_context:perpetual contract+futures")


def test_a5_title_sentence_matches_and_veto_stays_notice_wide():
    b = _bases(AKROUSDT=(T0, _id("AKRO")), DODOUSDT=(T0, _id("DODO")))
    v = _v("Will Delist AKRO and DODO USDT-Margined Contracts\n"
           "Positions will be closed at 2024-06-11 09:00 (UTC).")
    assert match_reason(v, "AKROUSDT", b)[0] and match_reason(v, "DODOUSDT", b)[0]
    vetoed = _v("Will Delist AKRO and DODO USDT-Margined Contracts\nCOIN-M contracts are not affected.")
    assert match_reason(vetoed, "AKROUSDT", b) == (False, "base_asset:AKRO;coin_m_veto:coin-m")


def test_a5_exact_symbol_match_keeps_per_notice_context():
    """DEC-40 (exact symbol token) is unchanged: the context may be in another sentence."""
    b = _bases(AKROUSDT=(T0, _id("AKRO")))
    v = _v("Binance Futures Will Delist USDT-Margined Contracts\nAKROUSDT will be removed.")
    assert match_reason(v, "AKROUSDT", b) == (True, "usdm_context:usdt-margined")
    assert confident_match(v, "AKROUSDT")
