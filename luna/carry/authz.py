"""The outcome authorization token (frozen protocol §12, R1). Replay-only; paper never imports it.

Every path that can produce V5 outcome-window numbers from the real frozen inputs (the snapshot
builder, the arm runner, the study, the I reference and the W-V4 bridge steps) calls
``require_authorization``; only ``outcome_guard.authorize`` creates the token. This is a guard
against accidental runs inside this codebase, not a security boundary: Python code that imports the
private sentinel on purpose can still forge a token (declared).
"""

from __future__ import annotations


class OutcomeBlocked(RuntimeError):
    """A run on the real frozen inputs without the outcome authorization."""


class OutcomeAuthorization:
    """Produced only by ``luna.carry.outcome_guard.authorize`` (all §12 checks passed)."""

    _secret = object()

    def __init__(self, key: object, record: dict) -> None:
        if key is not OutcomeAuthorization._secret:
            raise OutcomeBlocked("an OutcomeAuthorization can only come from outcome_guard.authorize")
        self.record = record


def require_authorization(data, auth) -> None:
    if getattr(data, "real", False) and not isinstance(auth, OutcomeAuthorization):
        raise OutcomeBlocked(
            "real frozen inputs: the single historical outcome run needs the user's explicit approval "
            "(outcome_guard.authorize); dry runs use synthetic data"
        )
