"""The frozen constants are loaded only when their sha256 equals the value pinned at the spec freeze."""

import json

import pytest

from luna.v6 import spec


def test_pinned_constants_load_and_match_the_frozen_values():
    c = spec.constants()
    assert spec.CONSTANTS_SHA256 == "152af4cb19e688b3551feb8210aeec5c471aec78b708592fcbb6eeef08946974"
    assert spec.SPEC_SHA256 == "c659d0cd6928c6356be43cbe97eaa91075197640ef195d09aba1ac5d582b2c5f"
    assert (c["fees"]["spot_maker"], c["fees"]["spot_taker"]) == (0.0040, 0.0080)
    assert (c["fees"]["futures_maker"], c["fees"]["futures_taker"]) == (0.0002, 0.0005)
    assert c["statistics"]["forward_alpha_fw"] == [0.001, 0.001, 0.048]
    assert c["h3"]["variants_k"] == [4, 6] and c["h1"]["variants_m"] == [1.5, 2.5]


def test_a_changed_constants_file_is_refused(tmp_path):
    raw = json.loads(spec.CONSTANTS_PATH.read_text(encoding="utf-8"))
    raw["h3"]["sigma_star_annual"] = 0.12
    p = tmp_path / "c.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(spec.SpecMismatch):
        spec.constants(p)


def test_the_committed_spec_file_matches_its_pin():
    assert spec.verify_spec() == spec.SPEC_SHA256
