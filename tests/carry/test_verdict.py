"""Protocol rev 3 §10: combining the pre-registered row-switching rules (KILL, PIT-SENSITIVE,
FALLBACK-AFFECTED, G5). Pure logic; no outcomes."""

from __future__ import annotations

import itertools

import pytest

from luna.carry.verdict import ORDER, combine

V = ORDER  # WORSE < NO ADVANTAGE SHOWN < CARRY ONLY < ADVANTAGE


@pytest.mark.parametrize("kill,fb,g5", list(itertools.product([False, True], repeat=3)))
def test_verdict_matrix_is_the_minimum_over_the_applicable_rows(kill, fb, g5):
    rows = {"PRIMARY": "ADVANTAGE", "S-NOKILL": "CARRY ONLY",
            "S-PIT10": "ADVANTAGE", "S-PIT10/NOKILL": "NO ADVANTAGE SHOWN",
            "S-FB-CONS": "WORSE", "S-FB-CONS/NOKILL": "CARRY ONLY"}
    v, tags, used = combine(rows, kill_affected=kill, fallback_affected=fb, e_only_kill=g5)
    base = "S-NOKILL" if kill else "PRIMARY"
    sfx = "/NOKILL" if kill else ""
    expect_rows = [base, "S-PIT10" + sfx] + (["S-FB-CONS" + sfx] if fb else [])
    assert used == expect_rows
    expect = min((rows[r] for r in expect_rows), key=V.index)
    if g5:
        expect = min(expect, "CARRY ONLY", key=V.index)
    assert v == expect
    assert ("KILL-AFFECTED" in tags) == kill and ("FALLBACK-AFFECTED" in tags) == fb
    assert ("PIT-SENSITIVE" in tags) == (rows["S-PIT10" + sfx] != rows[base])


def test_no_tags_when_every_row_agrees():
    rows = {"PRIMARY": "CARRY ONLY", "S-PIT10": "CARRY ONLY"}
    assert combine(rows, kill_affected=False, fallback_affected=False, e_only_kill=False) == (
        "CARRY ONLY", [], ["PRIMARY", "S-PIT10"])


def test_a_missing_required_row_or_unknown_verdict_is_refused():
    with pytest.raises(KeyError):
        combine({"PRIMARY": "ADVANTAGE"}, kill_affected=False, fallback_affected=False, e_only_kill=False)
    with pytest.raises(ValueError):
        combine({"PRIMARY": "GREAT", "S-PIT10": "ADVANTAGE"}, kill_affected=False, fallback_affected=False,
                e_only_kill=False)
