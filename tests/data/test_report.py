import numpy as np
import pandas as pd

from luna.data.report import build_data_report
from luna.data.store import BarStore


def mk(n=200, base=100.0):
    idx = pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC")
    c = base + np.sin(np.arange(n) / 10)
    return pd.DataFrame(
        {"open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 1.0, "trades": 1, "filled": False},
        index=idx,
    )


def test_data_report_contains_sections(tmp_path):
    store = BarStore(tmp_path)
    for asset in ("BTC", "ETH"):
        store.write("kraken_futures", asset, mk())
        store.write("kraken_spot", asset, mk(base=100.5))
        f = pd.DataFrame({"rate": 1e-6}, index=pd.date_range("2025-01-01", periods=5, freq="h", tz="UTC"))
        store.write_funding(asset, f)
    out = build_data_report(store, tmp_path / "reports" / "data_report.md")
    text = out.read_text(encoding="utf-8")
    for section in ("Validation", "Funding coverage", "Spot vs perp basis"):
        assert section in text
    assert "| kraken_futures | BTC | 200 |" in text and "ok" in text
