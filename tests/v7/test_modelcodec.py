"""Canonical non-executable fold-model serialisation (``luna.v7.modelcodec``; Task 28 round 2, R54): bit-equal
predictions after a round trip for every family (en, logit_en, lgb_reg, lgb_clf) and calibrator kind (linear,
isotonic) and the Preprocessor, a canonical round trip, and refusal of malformed / foreign documents."""

from __future__ import annotations

import json

import pytest

from luna.v7 import modelcodec
from luna.v7.modelcodec import CodecError, decode, encode, model_sha256
from tests.v7 import leak_fixtures as lf

H = 72


@pytest.fixture(scope="module", params=["en", "lgb_reg"])
def model(request):
    return lf.final(H, request.param)  # en / lgb_reg regressor + logit_en and lgb_clf classifiers


def _hex(df):
    return {c: [float(x).hex() for x in df[c].to_numpy()] for c in sorted(df.columns)}


def test_round_trip_predictions_bit_equal(model):
    rows = lf.panel(H).iloc[:200]
    m2 = decode(encode(model))
    p1, p2 = model.predict(rows), m2.predict(rows)
    fam = next(iter(model.reg))
    cols = {f"yhat_{fam}", f"yhat_cal_{fam}", "p_logit_en", "p_logit_en_cal", "p_lgb_clf", "p_lgb_clf_cal"}
    assert cols <= set(p1.columns)
    assert _hex(p1) == _hex(p2)  # every regressor / classifier / calibrator output, bit for bit
    assert _hex(model.pre.transform(rows)) == _hex(m2.pre.transform(rows))
    assert m2.selection[fam].cfg == model.selection[fam].cfg
    assert m2.reg[fam].best_iteration == model.reg[fam].best_iteration
    assert m2.clf["lgb_clf"].fitted.best_iteration == model.clf["lgb_clf"].fitted.best_iteration


def test_round_trip_is_canonical(model):
    b = encode(model)
    assert encode(decode(b)) == b and model_sha256(decode(b)) == model_sha256(model)


def test_malformed_or_foreign_documents_refused(model):
    b = encode(model)
    with pytest.raises(CodecError, match="canonical"):
        decode(json.dumps(json.loads(b), indent=1).encode())  # same content, non-canonical bytes
    d = json.loads(b)
    fam = next(iter(d["reg"]))
    if "cls" in d["reg"][fam]["model"]:
        d["reg"][fam]["model"]["cls"] = "subprocess.Popen"
    else:
        d["clf"]["logit_en"]["fitted"]["model"]["cls"] = "subprocess.Popen"
    with pytest.raises(CodecError, match="whitelist"):
        decode(json.dumps(d, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(CodecError):
        decode(b"\x80not json")
    with pytest.raises(CodecError):
        decode(json.dumps({"format": "other"}).encode())
    assert modelcodec.FORMAT in b.decode()
