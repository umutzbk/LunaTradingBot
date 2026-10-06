from dataclasses import FrozenInstanceError

import pandas as pd
import pytest

from luna.types import Candidate, ExitSpec, Leg, StopSpec


def test_candidate_is_frozen():
    c = Candidate(
        ts=pd.Timestamp("2024-01-01", tz="UTC"),
        strategy="s",
        key="k",
        legs=(Leg("BTC", 1.0),),
        direction=1,
        entry_ref=100.0,
        stop=StopSpec("price", 95.0, 0.05),
        exit=ExitSpec(10),
        regime="r",
        reason="x",
        features={},
    )
    with pytest.raises(FrozenInstanceError):
        c.score = 1.0  # type: ignore[misc]
    assert c.score == 0.0 and c.exit.take_profit_z is None
