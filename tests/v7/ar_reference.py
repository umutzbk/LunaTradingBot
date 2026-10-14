"""Independent transcription of CRAN ``bidask`` 2.1.5 ``R/ar.R`` (method "AR", sign = TRUE).

R source (AR function, fetched read-only from the CRAN GitHub mirror):

    h <- log(high); l <- log(low); c <- log(close)
    m2 <- (h + l) / 2;  m1 <- shift(m2, 1);  c1 <- shift(c, 1)
    s2 <- 4 * (c1 - m1) * (c1 - m2)
    ar <- rmean(s2, width = width, shift = 1, na.rm = na.rm)
    ar <- sign(ar) * sqrt(abs(ar))

Missing bars are NaN on a regular grid; ``shift`` moves a vector down by one (NaN first). The spec then sets
negative window values to 0. No code is shared with luna.v7.spread.
"""

from __future__ import annotations

import math


def ar_s2(high: list[float], low: list[float], close: list[float]) -> list[float]:
    """s2[i] for i >= 1 (index 0 is NaN), NaN whenever either bar of the pair is missing."""
    n = len(close)
    h = [math.log(x) if x == x and x > 0 else math.nan for x in high]
    lo = [math.log(x) if x == x and x > 0 else math.nan for x in low]
    c = [math.log(x) if x == x and x > 0 else math.nan for x in close]
    m2 = [(a + b) / 2 for a, b in zip(h, lo, strict=True)]
    out = [math.nan] * n
    for i in range(1, n):
        out[i] = 4 * (c[i - 1] - m2[i - 1]) * (c[i - 1] - m2[i])
    return out


def ar_window(
    high: list[float], low: list[float], close: list[float], first: int, last: int
) -> float | None:
    """AR (sign = TRUE, na.rm = TRUE) of the pair products s2[first..last] (bar index of the later bar),
    negative -> 0. None if every pair is NaN."""
    s2 = [v for v in ar_s2(high, low, close)[first : last + 1] if v == v]
    if not s2:
        return None
    m = sum(s2) / len(s2)
    ar = math.copysign(math.sqrt(abs(m)), m)
    return max(ar, 0.0)
