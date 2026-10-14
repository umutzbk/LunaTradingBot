"""M3-1 step 3: classify each eligible W_DEV termination (A / B / C) from the reconstructed archived notices.

Inputs: m31/events.json, m31/notices.json (m31_notices.py). The termination notice and the scheduled settlement time
per contract were read from the archived capture text (sentence naming the contract and its automatic-settlement /
close-all-positions time); NOTICE below records that reading with the capture used. Outcome-free: no price is read.

Classes (user decision M3-1):
  A  exact settlement reconstructible: needs an official historical settlement price or the 1-second index series
     (1,800 observations) to reproduce the official formula. Neither exists (archive index klines: finest 1 m; no
     settlement / delivery dataset; m31/archive_dataset_and_index_intervals.txt) -> no A event is possible.
  B  official scheduled settlement time reconstructible from an immutable archived notice and consistent with the
     observed cessation (the last traded 15-minute bar closes at the scheduled time or within 15 minutes after it).
  C  no reconstructible notice / time, or a notice whose time conflicts with the observed cessation.
"""
import calendar, json, sys, time
from pathlib import Path

OUT = Path(sys.argv[1])
H, D = 3600, 86400

# (symbol, instance) -> (notice article code prefix, capture_ts used, scheduled settlement UTC, mapping evidence)
NOTICE = {
    ("ANCUSDT", 0): ("0bfb6cfd", "20220513023034", "2022-05-13 04:00", "single contract named; 'automatic settlement on the USDT-Margined ANC Perpetual Contract ... at 2022-05-13 04:00 AM (UTC)'"),
    ("AKROUSDT", 0): ("d1cb9596", "20220518143101", "2022-05-26 09:00", "explicit per-contract time 'AKRO USDT-Margined Contract at 2022-05-26 09:00 AM (UTC)'"),
    ("DODOUSDT", 0): ("d1cb9596", "20220518143101", "2022-05-27 09:00", "explicit per-contract time 'DODO USDT-Margined Contract at 2022-05-27 09:00 AM (UTC)'"),
    ("LUNAUSDT", 0): ("ef3ce76d", "20220512150146", "2022-05-12 15:30", "single contract named; 'automatic settlement on the LUNA USDT-Margined Contract ... at 2022-05-12 15:30 (UTC)'"),
    ("TLMUSDT", 0): ("af469aee", "20220602113539", "2022-06-09 09:00", "explicit per-contract time 'TLM USDT-Margined Contracts at 2022-06-09 09:00 (UTC)'"),
    ("ICPUSDT", 0): ("af469aee", "20220602113539", "2022-06-10 09:00", "explicit per-contract time 'ICP USDT-Margined Contracts at 2022-06-10 09:00 (UTC)'"),
    ("SRMUSDT", 0): ("9a667666", "20221114091646", "2022-11-15 04:30", "'RAYUSDT and SRMUSDT ... 2022-11-15 04:00 and 2022-11-15 04:30 (UTC) respectively' (explicit ordered mapping)"),
    ("BNXUSDT", 0): ("4d23ada5", "20230210043740", "2023-02-11 04:00", "'At 2023-02-11 04:00 (UTC), Binance Futures will close all positions' (BNX swap notice)"),
    ("COCOSUSDT", 0): ("45852dc1", "20230518081658", "2023-05-25 09:00", "'At 2023-05-25 09:00 (UTC), Binance Futures will close all positions' (COCOS swap notice, futures section)"),
    ("STRAXUSDT", 0): ("2e6eb8e6", "20240308095620", "2024-03-15 09:00", "'USDⓈ-M STRAXUSDT Perpetual Contract ... At 2024-03-15 09:00 (UTC), Binance Futures will close all positions'"),
    ("WAVESUSDT", 0): ("f65faeee", "20240603030208", "2024-06-11 09:00", "'automatic settlement on the USDⓈ-M WAVESUSDT Perpetual Contract at 2024-06-11 09:00 (UTC)'"),
    ("RNDRUSDT", 0): ("d1f2ae8d", "20240710075935", "2024-07-16 09:00", "'automatic settlement on the RNDRUSDT USDⓈ-M Perpetual Contracts at 2024-07-16 09:00 (UTC)'"),
    ("MATICUSDT", 0): ("6a6de383", "20240828094940", "2024-09-04 09:00", "'automatic settlement on the MATICUSDT and MATICUSDC USDⓈ-M Perpetual Contracts at 2024-09-04 09:00 (UTC)'"),
    ("UNFIUSDT", 0): ("043c8407", "20241101134821", "2024-10-30 09:00", "'automatic settlement on the UNFIUSDT USDⓈ-M Perpetual Contract at 2024-10-30 09:00 (UTC)'"),
    ("XEMUSDT", 0): ("cfe97d52", "20250429054216", "2024-12-09 09:00", "'automatic settlement on the USDⓈ-M XEMUSDT, ORBSUSDT, and LOOMUSDT ... at 2024-12-09 09:00 (UTC)' (one time for all three)"),
    ("MAVIAUSDT", 0): ("27eac26", "20241206174323", "2024-12-16 09:00", "'automatic settlement on the USDⓈ-M MAVIAUSDT, OMGUSDT, and BONDUSDT ... at 2024-12-16 09:00 (UTC)' (one time for all three)"),
}


def ep(s, fmt):
    return calendar.timegm(time.strptime(s, fmt))


def main():
    events = json.loads((OUT / "events.json").read_text())
    notices = {(r["symbol"], r["instance"]): r for r in json.loads((OUT / "notices.json").read_text(encoding="utf-8"))}
    rows = []
    for ev in events:
        k = (ev["symbol"], ev["instance"])
        r = notices[k]
        close = ep(ev["last_trade_bar_close"][:19], "%Y-%m-%dT%H:%M:%S")
        row = {"symbol": ev["symbol"], "instance": ev["instance"], "instance_start": ev["instance_start"][:16],
               "last_traded_bar_close": ev["last_trade_bar_close"][:16], "last_eligible_decision": ev["last_eligible_decision"][:16],
               "official_settlement_price_source": "none (no official historical source)",
               "index_1s_observations": "not archived (finest archived index kline: 1 m)"}
        if k in NOTICE:
            frag, cts, sched, why = NOTICE[k]
            n = next((n for n in r["notices"] if frag in n["url"] and n.get("capture_ts") == cts), None)
            assert n is not None, k
            s_ep = ep(sched, "%Y-%m-%d %H:%M")
            consistent = s_ep <= close <= s_ep + 15 * 60  # the last traded 15m bar ends at or within 15 min after the scheduled time
            cap_ep = ep(cts, "%Y%m%d%H%M%S")
            # U6: first decision (00:05/08:05/16:05) >= known_at + 1 min, filled at decision + 15 min, before cessation?
            d0 = (cap_ep + 60 + D) // D * D - D
            decs = [d0 + o * 60 for o in (5, 485, 965)] + [d0 + D + o * 60 for o in (5, 485, 965)]
            first_dec = min(x for x in decs if x >= cap_ep + 60)
            u6_exit_before = first_dec + 15 * 60 <= min(s_ep, close - 15 * 60)
            strict_token = n["exact_symbol_token_hits"] > 0
            row.update({"notice_url": n["url"], "capture_ts_utc": cts, "capture_sha256": n["sha256"],
                        "known_at_lower_bound": time.strftime("%Y-%m-%d %H:%M", time.gmtime(cap_ep)),
                        "scheduled_settlement_utc": sched, "mapping_evidence": why,
                        "consistent_with_observed_cessation": consistent,
                        "DEC40_strict_exact_symbol_token": strict_token,
                        "U6_exit_fill_before_cessation_if_matched": u6_exit_before,
                        "class": "B" if consistent else "C",
                        "class_reason": "settlement time known; exact settlement price not reconstructible" if consistent
                        else "archived notice time conflicts with the observed cessation"})
        else:
            row.update({"notice_url": None, "class": "C",
                        "class_reason": "no immutable archived notice with the termination time found under the guard",
                        "search": f"catalog-161 titles; slug CDX {r['search_window'][0]}..{r['search_window'][1]}; status-200 captures only",
                        "candidates_checked": [(n.get('capture_ts'), (n.get('title') or '')[:80]) for n in r["notices"]]})
        rows.append(row)
    (OUT / "classification.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    from collections import Counter
    print(Counter(x["class"] for x in rows))
    for x in rows:
        print(x["class"], x["symbol"], x["instance"], x["last_traded_bar_close"], x.get("scheduled_settlement_utc"), x.get("known_at_lower_bound"),
              "strict" if x.get("DEC40_strict_exact_symbol_token") else "-", "u6exit" if x.get("U6_exit_fill_before_cessation_if_matched") else "-")


if __name__ == "__main__":
    main()
