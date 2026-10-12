"""V6 guards: the holdout stays sealed, forward needs the code freeze and the holdout opening."""

import pandas as pd
import pytest

from luna.v6 import guard


def test_dev_scope_refuses_any_holdout_timestamp():
    guard.require_window(pd.Timestamp("2025-11-10T00:00Z"), pd.Timestamp("2026-06-30T23:45Z"), "dev")
    with pytest.raises(guard.HoldoutBlocked):
        guard.require_window(pd.Timestamp("2025-11-10T00:00Z"), pd.Timestamp("2026-07-01T00:00Z"), "dev")


def test_holdout_scope_needs_the_authorization_token():
    with pytest.raises(guard.HoldoutBlocked):
        guard.require_window(guard.HOLDOUT_START, guard.HOLDOUT_START + pd.Timedelta("1D"), "holdout")
    with pytest.raises(guard.HoldoutBlocked):
        guard.HoldoutAuthorization(object(), {})


def test_forward_scope_refused_before_the_code_freeze(tmp_path):
    with pytest.raises(guard.HoldoutBlocked, match="code freeze"):
        guard.require_window(pd.Timestamp("2026-11-01T00:00Z"), pd.Timestamp("2026-12-01T00:00Z"), "forward",
                             repo=tmp_path)


def test_forward_scope_refused_before_the_holdout_opening(tmp_path, monkeypatch):
    rec = tmp_path / guard.FREEZE_RECORD_REL
    rec.parent.mkdir(parents=True)
    rec.write_text('{"T_F": "2026-11-20T00:00:00Z"}', encoding="utf-8")
    monkeypatch.setattr(guard, "marker_paths", lambda repo=None: [tmp_path / "m1", tmp_path / "m2"])
    with pytest.raises(guard.HoldoutBlocked, match="holdout opening"):
        guard.require_window(pd.Timestamp("2026-11-20T00:00Z"), pd.Timestamp("2026-12-01T00:00Z"), "forward",
                             repo=tmp_path)
    (tmp_path / "m1").write_text("{}")
    (tmp_path / "m2").write_text("{}")
    guard.require_window(pd.Timestamp("2026-11-20T00:00Z"), pd.Timestamp("2026-12-01T00:00Z"), "forward",
                         repo=tmp_path)


def test_authorize_refuses_without_the_exact_phrase_and_on_this_unfrozen_code():
    with pytest.raises(guard.HoldoutBlocked, match="approval"):
        guard.authorize_holdout(None)
    with pytest.raises(guard.HoldoutBlocked, match="approval"):
        guard.authorize_holdout("yes")
    with pytest.raises(guard.HoldoutBlocked, match="v6-code-frozen"):
        guard.authorize_holdout(guard.approval_phrase())  # no code tag yet -> refused, no marker written
    assert not any(m.exists() for m in guard.marker_paths())


def test_markers_are_exclusive_and_written_once(tmp_path, monkeypatch):
    ms = [tmp_path / "a" / guard.MARKER, tmp_path / "b" / guard.MARKER]
    monkeypatch.setattr(guard, "marker_paths", lambda repo=None: ms)
    monkeypatch.setattr(guard, "preflight", lambda repo=None: {"head": "x"})
    tok = guard.authorize_holdout(guard.approval_phrase())
    assert isinstance(tok, guard.HoldoutAuthorization) and all(m.exists() for m in ms)
    with pytest.raises(guard.HoldoutBlocked, match="already opened"):
        guard.authorize_holdout(guard.approval_phrase())


def test_holdout_scope_refuses_data_beyond_the_holdout_end(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "marker_paths", lambda repo=None: [tmp_path / "x" / guard.MARKER, tmp_path / "y" / guard.MARKER])
    monkeypatch.setattr(guard, "preflight", lambda repo=None: {"head": "x"})
    tok = guard.authorize_holdout(guard.approval_phrase())
    guard.require_window(guard.HOLDOUT_START, guard.HOLDOUT_END - pd.Timedelta("1min"), "holdout", tok)
    with pytest.raises(guard.HoldoutBlocked):
        guard.require_window(guard.HOLDOUT_START, guard.HOLDOUT_END, "holdout", tok)
