"""Luna V4 forward data collector: raw, append-only, timestamped Kraken public snapshots.

Spec: docs/superpowers/specs/luna-v4-forward-collector-design.md. The collector never computes
basis, mids, spreads, returns or signals and never fills or resamples data. It must not import the
trading/research stack (engine, execution, store, strategy, portfolio, risk, v2, v3, data.backfill).
"""
