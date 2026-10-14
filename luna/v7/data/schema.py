"""Raw / derived table schemas R1-R12 and DT1-DT4 (spec S-3, L12).

Columns are exactly those of the S-3 tables (key columns are listed separately). ``known_at`` rules that are a
fixed offset from the row's key time are also available mechanically through ``known_at``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from luna.v7.timeutil import MIN


class IntegrityBlocker(RuntimeError):
    """Data-integrity blocker (S-2.2 DEC-33, S-2.6 DEC-32): conflicting sources are never picked silently."""


@dataclass(frozen=True)
class TableSpec:
    name: str
    key: tuple[str, ...]
    columns: tuple[str, ...]
    known_at_rule: str
    optional: bool = False


_BAR = ("o", "h", "l", "c", "vol_base", "vol_quote", "n_trades", "taker_buy_quote")
_OHLC = ("o", "h", "l", "c")


def _t(num: str, name: str, key, cols, rule, optional=False) -> tuple[str, TableSpec]:
    return num, TableSpec(name, tuple(key), tuple(cols), rule, optional)


TABLES: dict[str, TableSpec] = dict(
    [
        _t("R1", "instruments", ("venue", "symbol", "instance"),
           ("base", "contract_type", "underlying_type", "multiplier", "first_bar_ts", "last_trade_ts",
            "instance_end_known_at", "tick", "step", "min_notional", "source", "retrieved_at"),
           "identity: first_bar_ts via L1 whitelist (DEC-31); time-varying: effective/known ts; "
           "instance end: instance_end_known_at (DEC-44)"),
        _t("R2", "funding", ("venue", "symbol", "funding_ts"),
           ("rate", "interval_h", "reconciled_or_invalid", "interval_transition", "mark_at_settle", "source"),
           "funding_ts + 1 min; forward: max(recv_ts, funding_ts + 1 min)"),
        _t("R3", "bars_last_15m", ("venue", "symbol", "open_ts"), _BAR,
           "open_ts + 15 min; monthly union daily (DEC-33); forward: max(recv_ts, open_ts + 15 min)"),
        _t("R4", "bars_mark_index_premium_15m", ("venue", "symbol", "open_ts"), _OHLC,
           "open_ts + 15 min; forward: max(recv_ts, open_ts + 15 min)"),
        _t("R5", "bars_last_1m", ("venue", "symbol", "open_ts"), _BAR,
           "open_ts + 1 min; monthly union daily (DEC-33); forward: max(recv_ts, open_ts + 1 min)"),
        _t("R6", "book_top", ("venue", "symbol", "ts"), ("bid", "bid_qty", "ask", "ask_qty"),
           "ts; diagnostic only (S-4.5)", True),
        _t("R7", "book_depth", ("venue", "symbol", "ts", "pct_level"), ("cum_notional",), "ts", True),
        _t("R8", "open_interest", ("venue", "symbol", "ts"), ("oi", "oi_value"), "ts", True),
        _t("R9", "fees", ("venue", "product", "tier", "effective_from"),
           ("maker", "taker", "effective_to", "source_url", "evidence_record_sha256"), "constants"),
        _t("R10", "aggtrades_cache", ("venue", "symbol", "trade_id"),
           ("price", "qty", "ts", "is_buyer_maker"),
           "ts; only for (symbol, UTC day) pairs with a simulated E_M order"),
        _t("R11", "notices", ("venue", "notice_id", "version"),
           ("kind", "matched_instances", "releaseDate", "archive_url", "capture_ts", "content_sha256",
            "scheduled_delist_ts"),
           "max(releaseDate, capture timestamp); forward: recv_ts (S-2.5, DEC-36)"),
        _t("R12", "terminal_settlements", ("venue", "contract_instance_id"),
           ("settlement_ts", "settlement_price", "source", "source_timestamp", "known_at", "retrieved_at",
            "sha256"),
           "source_timestamp; optional (EC-7), valid with zero rows", True),
        _t("DT1", "candidates", ("decision_ts", "venue", "symbol", "side"),
           ("U1", "U2", "U3", "U4", "U5", "U6", "U7", "U8", "features", "max_known_at"),
           "max_known_at asserted <= decision_ts - 1 min"),
        _t("DT2", "labels", ("decision_ts", "venue", "symbol", "side", "h"),
           ("components", "net", "y_pos", "label_known_at", "invalid_reason"), "label_known_at"),
        _t("DT3", "folds", ("fold_id",), ("block_boundaries", "test_lo", "test_hi"), "n/a"),
        _t("DT4", "runs", ("run_id",),
           ("code_commit", "config_sha256", "data_manifest_sha256s", "seeds", "library_versions"), "n/a"),
    ]
)

# Tables whose known_at is a fixed offset from the key time (ns).
KNOWN_AT_OFFSET_NS: dict[str, int] = {"R2": MIN, "R3": 15 * MIN, "R4": 15 * MIN, "R5": MIN,
                                      "R6": 0, "R7": 0, "R8": 0, "R10": 0}


def known_at(table: str, key_ts_ns: int) -> int:
    """Archive ``known_at`` of a row whose key time is ``key_ts_ns`` (S-3 offset rules)."""
    if table not in KNOWN_AT_OFFSET_NS:
        raise KeyError(f"{table} has no fixed-offset known_at rule")
    return int(key_ts_ns) + KNOWN_AT_OFFSET_NS[table]


def validate(table: str, df: pd.DataFrame) -> None:
    """Columns present, key unique. An optional table may be empty (EC-7); R12 stays valid with zero rows."""
    spec = TABLES[table]
    need = [*spec.key, *spec.columns]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise IntegrityBlocker(f"{table}: missing columns {missing}")
    if df.empty:
        if spec.optional:
            return
        raise IntegrityBlocker(f"{table}: empty but not optional")
    if df.duplicated(list(spec.key)).any():
        raise IntegrityBlocker(f"{table}: duplicate keys")
