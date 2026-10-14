import pandas as pd
import pytest

from luna.v7.data import schema
from luna.v7.data.schema import TABLES, IntegrityBlocker
from luna.v7.timeutil import MIN


def test_all_tables_present():
    assert set(TABLES) == {f"R{i}" for i in range(1, 13)} | {f"DT{i}" for i in range(1, 5)}


def test_columns_match_spec():
    cols = ("o", "h", "l", "c", "vol_base", "vol_quote", "n_trades", "taker_buy_quote")
    assert TABLES["R3"].columns == cols
    assert TABLES["R3"].key == ("venue", "symbol", "open_ts")
    assert TABLES["R4"].columns == ("o", "h", "l", "c")
    assert TABLES["R9"].key == ("venue", "product", "tier", "effective_from")
    assert TABLES["R12"].key == ("venue", "contract_instance_id")


def test_r12_valid_empty():
    spec = TABLES["R12"]
    assert spec.optional
    df = pd.DataFrame({c: [] for c in (*spec.key, *spec.columns)})
    schema.validate("R12", df)  # zero rows is valid (EC-7)
    with pytest.raises(IntegrityBlocker):
        schema.validate("R3", pd.DataFrame({c: [] for c in (*TABLES["R3"].key, *TABLES["R3"].columns)}))


def test_validate_duplicate_key_and_missing_column():
    spec = TABLES["R8"]
    row = {c: [1, 1] for c in (*spec.key, *spec.columns)}
    with pytest.raises(IntegrityBlocker):
        schema.validate("R8", pd.DataFrame(row))
    with pytest.raises(IntegrityBlocker):
        schema.validate("R8", pd.DataFrame({"venue": ["x"]}))


def test_known_at_rules():
    t = 1_700_000_000 * 10**9
    assert schema.known_at("R3", t) == t + 15 * MIN
    assert schema.known_at("R5", t) == t + MIN
    assert schema.known_at("R2", t) == t + MIN
    assert schema.known_at("R6", t) == t
