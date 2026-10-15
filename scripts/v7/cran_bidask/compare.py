"""Compare CRAN bidask 2.1.5 AR output (r_output.json) with the repo's Python transcription and, optionally,
luna.v7.spread. Core check is stdlib only; --luna needs numpy + pandas (run it with the repo's .venv python).

    python -I compare.py --repo C:/path/to/LunaTradingBot-v7 [--vectors vectors.json] [--r-output r_output.json]
                         [--luna]

Tolerance (stated): PASS if both values are missing (R NaN/NA <-> Python None), or the floats are identical,
or |a - b| <= 1e-12 * max(|a|, |b|).
Ill-conditioned windows (signed checks only): when the window mean of the pair products is zero in exact
arithmetic (cancelling products), sqrt(|mean|) of a rounding-level residue is not reproducible to 1e-12 relative
across summation orders (data.table::frollmean vs a left-to-right sum). For the signed checks (sign, luna_pi,
roll_sign, roll_luna_pi) a value that fails the relative rule is reported as PASS-COND iff the recovered signed
means agree to |m_py - m_R| <= 1e-12 * mean(|pi|) over the window, i.e. a summation-level difference. PASS-COND
is listed explicitly; with --strict it counts as FAIL. The truncated (spec) checks never use it.
Exit status 1 on any FAIL.

Checks per window [first, last] (0-based later-bar indices of the pair products, see vectors.json):
  map   R full-sample value == R endpoints value (self-check of the spread() <-> window mapping)
  ref   tests/v7/ar_reference.py::ar_window(h, l, c, first, last)  vs  max(R_signed, 0)   (spec: negative -> 0)
  sign  signed sqrt of the mean of ar_reference.ar_s2 over the window  vs  R_signed   (sign = TRUE itself)
  luna_pi  (--luna) signed sqrt of the mean of luna.v7.spread.pair_products pi over the window  vs  R_signed
  luna_h   (--luna) luna.v7.spread.half_spread(bars first-1..last, tau = close of bar last + 1 min)
           vs  max(lam * max(R_signed, 0) / 2, floor)        (windows of at most 96 bars only)
Rolling rows r (spread(width = w)): ref/sign on window [r - w + 2, r]; with --luna and w == 96 also luna_pi and
luna_h on the WHOLE frame at tau = close of bar r + 1 min (the S-4.5 24 h window = exactly those 95 pairs).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path

REL_TOL = 1e-12


def dec(s):
    return math.nan if s is None else float.fromhex(s)


def is_missing(v):
    return v is None or v != v


def close_enough(a, b) -> bool:
    if is_missing(a) or is_missing(b):
        return is_missing(a) and is_missing(b)
    if a == b:
        return True
    return abs(a - b) <= REL_TOL * max(abs(a), abs(b))


def signed_sqrt_mean(vals):
    vals = [v for v in vals if v == v]
    if not vals:
        return None
    m = sum(vals) / len(vals)
    return math.copysign(math.sqrt(abs(m)), m)


def mean_abs(vals):
    vals = [abs(v) for v in vals if v == v]
    return sum(vals) / len(vals) if vals else None


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="repo root containing tests/v7/ar_reference.py")
    ap.add_argument("--vectors", default=str(here / "vectors.json"))
    ap.add_argument("--r-output", default=str(here / "r_output.json"))
    ap.add_argument("--luna", action="store_true", help="also check luna.v7.spread (needs numpy, pandas)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--strict", action="store_true", help="PASS-COND counts as FAIL")
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    vec_bytes = Path(a.vectors).read_bytes()
    vec = json.loads(vec_bytes)
    rout = json.loads(Path(a.r_output).read_text(encoding="utf-8"))
    sha = hashlib.sha256(vec_bytes).hexdigest()
    hdr_ok = True
    print(f"vectors sha256 {sha}")
    print(f"r_output: {rout['r_version']}, bidask {rout['bidask_version']}, data.table {rout['data_table_version']}"
          f", vectors_sha256 {rout['vectors_sha256']}")
    if rout["vectors_sha256"].lower() != sha:
        print("FAIL header: r_output.json was computed from a different vectors.json")
        hdr_ok = False
    if rout["bidask_version"] != "2.1.5":
        print("FAIL header: bidask version is not 2.1.5")
        hdr_ok = False

    spec = importlib.util.spec_from_file_location("ar_reference", repo / "tests" / "v7" / "ar_reference.py")
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)
    print(f"transcription: {Path(spec.origin)}")

    luna = None
    if a.luna:
        sys.path.insert(0, str(repo))
        import numpy as np  # noqa: F401
        import pandas as pd

        from luna.v7 import spread as luna_spread
        from luna.v7.constants import FROZEN
        from luna.v7.timeutil import MIN, to_ns

        T0, Q = to_ns("2024-03-01T00:00:00Z"), 15 * MIN
        print(f"luna: {Path(luna_spread.__file__)} (lam={FROZEN.spread.lam}, floor={FROZEN.spread.floor}, "
              f"window_ns={FROZEN.spread.window_ns})")

        def frame(h, l, c, lo_i=0, hi_i=None):
            rows = []
            for i in range(lo_i, len(c) if hi_i is None else hi_i + 1):
                if h[i] != h[i] and l[i] != l[i] and c[i] != c[i]:
                    continue  # a missing bar is an absent row
                t = T0 + i * Q
                rows.append(dict(open_ts=t, o=c[i], h=h[i], l=l[i], c=c[i], vol_base=1.0, known_at=t + Q))
            cols = ["open_ts", "o", "h", "l", "c", "vol_base", "known_at"]
            return pd.DataFrame(rows, columns=cols).astype({"open_ts": "int64", "known_at": "int64"})

        def luna_pi_window(pp, first, last):
            lo_t, hi_t = T0 + (first - 1) * Q, T0 + (last - 1) * Q
            sel = pp["pi"][(pp["open_ts"] >= lo_t) & (pp["open_ts"] <= hi_t)]
            return signed_sqrt_mean([float(v) for v in sel])

        def expected_h(r_signed):
            if is_missing(r_signed):
                return None
            return max(FROZEN.spread.lam * max(r_signed, 0.0) / 2.0, FROZEN.spread.floor)

        luna = dict(frame=frame, pi=luna_pi_window, eh=expected_h, mod=luna_spread, T0=T0, Q=Q, MIN=MIN)

    by_name = {c["name"]: c for c in vec["cases"]}
    if [c["name"] for c in vec["cases"]] != [r["name"] for r in rout["results"]]:
        print("FAIL header: case lists differ between vectors.json and r_output.json")
        hdr_ok = False

    totals = {}
    n_fail_cases = 0
    n_cond_cases = 0
    for res in rout["results"]:
        cs = by_name[res["name"]]
        h, l, c = ([dec(s) for s in cs[k]] for k in ("high", "low", "close"))
        s2 = ref.ar_s2(h, l, c)
        counts = {}
        fails = []
        conds = []

        def chk(kind, got, want, where, scale=None):
            counts.setdefault(kind, [0, 0])
            counts[kind][1] += 1
            if (not close_enough(got, want) and scale is not None and not is_missing(got) and not is_missing(want)
                    and abs(math.copysign(got * got, got) - math.copysign(want * want, want)) <= REL_TOL * scale):
                msg = (f"{kind} {where}: python={got!r} R={want!r}; means {math.copysign(got * got, got)!r} vs "
                       f"{math.copysign(want * want, want)!r}, mean|pi|={scale!r}")
                if a.strict:
                    fails.append("PASS-COND rejected by --strict: " + msg)
                else:
                    counts[kind][0] += 1
                    conds.append(msg)
                return
            if close_enough(got, want):
                counts[kind][0] += 1
                exact = (is_missing(got) and is_missing(want)) or got == want
                if a.verbose:
                    print(f"    ok {kind} {where}: {got!r} vs {want!r}{'' if exact else ' (within 1e-12 rel)'}")
            else:
                fails.append(f"{kind} {where}: python={got!r} R={want!r}")

        pp = None
        if luna:
            full_frame = luna["frame"](h, l, c)
            pp = luna["mod"].pair_products(full_frame)

        for w in res["windows"]:
            first, last = w["first"], w["last"]
            r_full = dec(w["full_hex"])
            where = f"[{first},{last}]"
            if w["endpoints_used"]:
                chk("map", dec(w["endpoints_hex"]), r_full, where)
            want_trunc = None if is_missing(r_full) else max(r_full, 0.0)
            chk("ref", ref.ar_window(h, l, c, first, last), want_trunc, where)
            sc = mean_abs(s2[first:last + 1])
            chk("sign", signed_sqrt_mean(s2[first:last + 1]), r_full, where, sc)
            if luna:
                chk("luna_pi", luna["pi"](pp, first, last), r_full, where, sc)
                if last - first + 2 <= 96:
                    sub = luna["frame"](h, l, c, first - 1, last)
                    tau = luna["T0"] + (last + 1) * luna["Q"] + luna["MIN"]
                    chk("luna_h", luna["mod"].half_spread(sub, tau), luna["eh"](r_full), where)

        if res["rolling"] is not None:
            wd, r0 = res["rolling"]["width"], res["rolling"]["first_row"]
            for k, sv in enumerate(res["rolling"]["values_hex"]):
                r = r0 + k
                first, last = r - wd + 2, r
                rv = dec(sv)
                where = f"roll{wd}@{r}"
                chk("roll_ref", ref.ar_window(h, l, c, first, last), None if is_missing(rv) else max(rv, 0.0), where)
                sc = mean_abs(s2[first:last + 1])
                chk("roll_sign", signed_sqrt_mean(s2[first:last + 1]), rv, where, sc)
                if luna:
                    chk("roll_luna_pi", luna["pi"](pp, first, last), rv, where, sc)
                    if wd == 96:
                        tau = luna["T0"] + (r + 1) * luna["Q"] + luna["MIN"]
                        chk("roll_luna_h", luna["mod"].half_spread(full_frame, tau), luna["eh"](rv), where)

        status = "FAIL" if fails else ("PASS-COND" if conds else "PASS")
        n_cond_cases += bool(conds) and not fails
        n_fail_cases += bool(fails)
        summary = " ".join(f"{k}={p}/{t}" for k, (p, t) in counts.items())
        print(f"{status} {res['name']}: {summary}")
        for f in conds[:5]:
            print(f"    cond: {f}")
        for f in fails[:10]:
            print(f"    {f}")
        if len(fails) > 10:
            print(f"    ... {len(fails) - 10} more")
        for k, (p, t) in counts.items():
            totals.setdefault(k, [0, 0])
            totals[k][0] += p
            totals[k][1] += t

    print("totals: " + " ".join(f"{k}={p}/{t}" for k, (p, t) in totals.items()))
    n = len(rout["results"])
    ok = hdr_ok and n_fail_cases == 0
    print(f"{'PASS' if ok else 'FAIL'}: {n - n_fail_cases}/{n} cases"
          f" ({n_cond_cases} of them PASS-COND)"
          f"{'' if hdr_ok else ' (header check failed)'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
