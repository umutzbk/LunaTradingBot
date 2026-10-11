"""Protocol rev 3 §10: the final verdict from the pre-registered rows (replay-only; paper never imports it).

- Base row: PRIMARY, or S-NOKILL when the R4 kill fired in W or E (KILL-AFFECTED).
- S-PIT10 (always) and S-FB-CONS (only when FALLBACK-AFFECTED) are read with the same kill setting as
  the base row (``/NOKILL`` rows when KILL-AFFECTED).
- Final = the most conservative verdict over those rows; G5 (kill only in E, PRIMARY row) caps it at
  CARRY ONLY. A NO ADVANTAGE SHOWN can therefore become WORSE.
"""

from __future__ import annotations

ORDER = ("WORSE", "NO ADVANTAGE SHOWN", "CARRY ONLY", "ADVANTAGE")


def _rank(v: str) -> int:
    if v not in ORDER:
        raise ValueError(f"unknown verdict {v!r}")
    return ORDER.index(v)


def combine(rows: dict[str, str], *, kill_affected: bool, fallback_affected: bool,
            e_only_kill: bool) -> tuple[str, list[str], list[str]]:
    """-> (final verdict, tags, rows used in order)."""
    sfx = "/NOKILL" if kill_affected else ""
    base = "S-NOKILL" if kill_affected else "PRIMARY"
    used = [base, "S-PIT10" + sfx] + (["S-FB-CONS" + sfx] if fallback_affected else [])
    vs = [rows[r] for r in used]
    final = min(vs, key=_rank)
    tags = []
    if kill_affected:
        tags.append("KILL-AFFECTED")
    if vs[1] != vs[0]:
        tags.append("PIT-SENSITIVE")
    if fallback_affected:
        tags.append("FALLBACK-AFFECTED")
    if e_only_kill:
        final = min(final, "CARRY ONLY", key=_rank)
    return final, tags, used
