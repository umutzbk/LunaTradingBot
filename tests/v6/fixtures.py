"""Synthetic V6 inputs (no market data)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from luna.v6.data import FundingPanel

T0 = pd.Timestamp("2025-10-06T00:00Z")


def ar_funding(n_assets=12, hours=24 * 200, phi=0.995, seed=1, scale=2e-5, start=T0) -> FundingPanel:
    """Hourly AR(1) funding around asset-specific means (a persistent cross-section)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=hours, freq="h")
    mu = rng.normal(0, scale, n_assets)
    x = np.zeros((hours, n_assets))
    x[0] = mu
    for t in range(1, hours):
        x[t] = mu + phi * (x[t - 1] - mu) + rng.normal(0, scale / 3, n_assets)
    cols = [f"A{i:02d}" for i in range(n_assets)]
    return FundingPanel(pd.DataFrame(x, index=idx, columns=cols))
