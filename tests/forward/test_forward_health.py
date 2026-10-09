"""§8 collector-health logic (pure) and §9/§10.11 static import isolation."""

import ast
from pathlib import Path

import luna.forward
from luna.forward.envelope import iso_ns
from luna.forward.status import check_health

S = 1_000_000_000
NOW = 1_791_547_200 * S


def _status(**ages_s):
    """ages_s: source -> seconds since last ok (None: never)."""
    src = {}
    for name in ("fut_tickers", "spot_ticker", "fut_funding", "fut_instruments", "spot_assetpairs",
                 "fut_funding_full"):
        age = ages_s.get(name, 60)
        src[name] = {"last_ok_recv_ts": None if age is None else iso_ns(NOW - age * S)}
    return {"started_ts": iso_ns(NOW - ages_s.get("uptime", 10 * 3600) * S), "sources": src,
            "backup_ok": True, "backup_reason": None, "backup_warnings": []}


def test_healthy():
    code, lines = check_health(_status(), NOW)
    assert code == 0 and any("ok" in x for x in lines)
    assert not any(x.startswith("WARNING") for x in lines)


def test_ticker_staleness_fails():
    assert check_health(_status(fut_tickers=179), NOW)[0] == 0
    assert check_health(_status(fut_tickers=180), NOW)[0] == 1
    assert check_health(_status(spot_ticker=200), NOW)[0] == 1
    assert check_health(_status(spot_ticker=None), NOW)[0] == 1


def test_funding_staleness_fails_after_2h():
    assert check_health(_status(fut_funding=7199), NOW)[0] == 0
    code, lines = check_health(_status(fut_funding=7200), NOW)
    assert code == 1 and any("fut_funding" in x for x in lines)


def test_funding_never_ok_is_tolerated_only_in_the_first_2h():
    assert check_health(_status(fut_funding=None, uptime=3000), NOW)[0] == 0
    assert check_health(_status(fut_funding=None, uptime=7300), NOW)[0] == 1


def test_missing_status_fails():
    code, lines = check_health(None, NOW)
    assert code == 1 and "status.json" in lines[0]


def test_reference_sources_older_than_26h_warn_only():
    code, lines = check_health(_status(fut_instruments=26 * 3600 + 1, spot_assetpairs=None), NOW)
    assert code == 0
    warn = [x for x in lines if x.startswith("WARNING")]
    assert any("fut_instruments" in x for x in warn) and any("spot_assetpairs" in x for x in warn)
    assert check_health(_status(fut_instruments=26 * 3600 + 1), NOW, strict=True)[0] == 0


def test_backup_warning_printed_and_strict_exit_2():
    st = _status()
    st.update(backup_ok=False, backup_reason="LUNA_FORWARD_BACKUP_DIR not set", backup_warnings=["overdue x"])
    code, lines = check_health(st, NOW)
    assert code == 0
    assert "WARNING backup: LUNA_FORWARD_BACKUP_DIR not set" in lines and "WARNING backup: overdue x" in lines
    assert check_health(st, NOW, strict=True)[0] == 2
    st["sources"]["fut_tickers"]["last_ok_recv_ts"] = None
    assert check_health(st, NOW, strict=True)[0] == 1  # unhealthy wins


FORBIDDEN = ("luna.engine", "luna.execution", "luna.store", "luna.strategy", "luna.portfolio",
             "luna.risk", "luna.v2", "luna.v3", "luna.data.backfill")


def test_forward_package_imports_nothing_forbidden():
    pkg = Path(luna.forward.__file__).parent
    files = sorted(pkg.glob("*.py"))
    assert len(files) >= 8
    bad = []
    for f in files:
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for n in names:
                if any(n == m or n.startswith(m + ".") for m in FORBIDDEN):
                    bad.append((f.name, n))
    assert bad == []


def test_unclosed_days_warn_and_fail_strict():
    st = _status()
    st["unclosed_days"] = ["2026-10-07"]
    code, lines = check_health(st, NOW)
    assert code == 0 and any(x.startswith("WARNING") and "2026-10-07" in x for x in lines)
    assert check_health(st, NOW, strict=True)[0] == 2


def test_daily_full_funding_staleness_warns_only():
    st = _status()
    st["sources"]["fut_funding_full"] = {"last_ok_recv_ts": iso_ns(NOW - 27 * 3600 * S)}
    code, lines = check_health(st, NOW)
    assert code == 0 and any(x.startswith("WARNING fut_funding_full") for x in lines)


def test_importing_every_forward_module_loads_nothing_forbidden():
    import subprocess
    import sys

    pkg = Path(luna.forward.__file__).parent
    mods = sorted(f"luna.forward.{f.stem}" for f in pkg.glob("*.py") if f.stem != "__init__")
    code = (f"import sys\nfor m in {mods!r}:\n    __import__(m)\nforbidden = {FORBIDDEN!r}\n"
            "bad = sorted(n for n in sys.modules\n"
            "             if any(n == f or n.startswith(f + '.') for f in forbidden))\n"
            "print(','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
    assert len(mods) >= 10
