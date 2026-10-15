"""Task 26a fix round 2: executed BookRuns persisted without pickle (canonical parquet + JSON), exact round
trips (output sha256, byte-identical re-serialization), the mandatory pinned file hash and ``load_pinned``,
and no unpickling path anywhere in luna/v7. Synthetic runs in tmp_path only."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import os
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from luna.v7 import guard, k9, rawstore, runner, runstore, windows
from luna.v7.data.schema import IntegrityBlocker
from luna.v7.runrecord import run_output_sha256
from luna.v7.runstore import RunStore, decode_run, encode_run, run_file_sha256
from luna.v7.timeutil import D
from tests.v7 import auth_fixtures as af
from tests.v7 import fixtures_v7 as fx
from tests.v7 import run_fixtures as rf


@pytest.fixture
def log(tmp_path):
    lg = k9.K9Log(tmp_path / "k9")
    with k9.using(lg):
        yield lg


@pytest.fixture(scope="module")
def engine_runs():
    """Representative engine books (A0 under E_T and E_M on the synthetic universe, as in the e2e test)."""
    u = fx.build_universe(7)
    prov = runner.DecisionProvider(u, with_features=True)
    return {"A0_T": runner.run_book("A0", u, prov, fx.BOOK, fx.H_STAR, fx.NOW),
            "A0_M": runner.run_book("A0", u, prov, fx.BOOK, fx.H_STAR, fx.NOW, execution="E_M")}


def exotic_run():
    """Every storage kind and awkward cell: numpy scalars, -0.0, NaN, inf, None, pd.NA, nested tuples /
    lists / dicts, str dtype with missing values, nullable Int64, bool, an empty frame with columns, a frame
    without columns and a missing (None) frame."""
    r = rf.funding_run([0.01, -0.02, 0.03], costs=[0.001, 0.0, 0.002])
    dec = pd.DataFrame({
        "t": np.array([1, 2, 3], dtype=np.int64), "killed": [False, True, False],
        "orders": [(), ("a", np.float64(1.5), np.int64(3)), (("x", -0.0), [1, None], {"k": float("nan")})],
        "notes": [("n",), (float("inf"), np.bool_(True)), (pd.NA, np.str_("s"))],
        "hedge_reason": pd.array(["h", None, "z"], dtype="str"),
        "i": pd.array([1, None, 3], dtype="Int64"), "f": np.array([-0.0, np.nan, 1e-300]),
        "u": np.array([1, 2, 3], dtype=np.uint32)})
    empty = pd.DataFrame({"ts": np.array([], dtype=np.int64), "symbol": pd.array([], dtype="str"),
                          "flags": np.array([], dtype=object)})
    return dataclasses.replace(r, decisions=dec, positions=empty, marks=None,
                               daily=pd.DataFrame(), integrity=("i1", "i2"),
                               invalid="INVALID RUN", invalid_detail="d", invalid_ts=7, equity0=0.5,
                               filter_provenance=("p",), research=True)


def _cell_types(df: pd.DataFrame | None) -> object:
    if df is None:
        return None
    return [(c, str(t), [repr(v) for v in df[c].tolist()]) for c, t in df.dtypes.items()]


def assert_round_trip(run) -> dict[str, bytes]:
    files = encode_run(run)
    back = decode_run(files)
    assert run_output_sha256(back) == run_output_sha256(run)
    assert encode_run(back) == files  # canonical: a rebuilt run persists to the same bytes
    for f in dataclasses.fields(run):
        a, b = getattr(run, f.name), getattr(back, f.name)
        if isinstance(a, pd.DataFrame) or a is None and f.name in runstore.FRAMES:
            assert _cell_types(a) == _cell_types(b), f.name
        else:
            assert repr(a) == repr(b) and type(a) is type(b), f.name
    return files


def test_round_trip_is_exact_and_canonical(engine_runs):
    for run in (*engine_runs.values(), rf.funding_run([0.01, 0.02], costs=[0.001, 0.0]), exotic_run()):
        assert_round_trip(run)
    assert len(engine_runs["A0_T"].fills) and len(engine_runs["A0_T"].positions)  # non-trivial books
    files = encode_run(exotic_run())
    assert sorted(files) == ["decisions.parquet", "fills.parquet", "funding_flows.parquet",
                             "positions.parquet", "run.json"]
    rec = json.loads(files["run.json"])
    assert rec["frames"]["marks"] is None and rec["frames"]["daily"] == {
        "columns": [], "file": None, "rows": 0, "sha256": None}
    assert all(b"pandas" not in v and b"ARROW:schema" not in v for v in files.values())  # no pickled metadata


def test_unsupported_values_are_refused():
    r = rf.funding_run([0.01])
    with pytest.raises(TypeError):
        encode_run(dataclasses.replace(r, decisions=pd.DataFrame({"t": pd.to_datetime(["2024-01-01"])})))
    with pytest.raises(TypeError):
        encode_run(dataclasses.replace(r, decisions=pd.DataFrame({"t": [object()]})))


def test_put_load_mandatory_pin(tmp_path, log, engine_runs):
    rs = RunStore(tmp_path / "runs")
    run = engine_runs["A0_M"]
    sha = rs.put(run)
    assert sha == run_output_sha256(run) and rs.file_sha256(sha) == run_file_sha256(run)
    assert rs.put(run) == sha  # idempotent
    back = rs.load(sha, file_sha256=run_file_sha256(run))
    assert run_output_sha256(back) == sha and encode_run(back) == encode_run(run)
    with pytest.raises(TypeError):
        rs.load(sha)  # type: ignore[call-arg]  - the file pin is mandatory
    with pytest.raises(IntegrityBlocker, match="pinned"):
        rs.load(sha, file_sha256="0" * 64)
    d = tmp_path / "runs" / sha
    assert sorted(p.name for p in d.iterdir()) == json.loads((tmp_path / "runs" / "MANIFEST.json").read_text(
        encoding="utf-8"))["runs"][sha]["files"]
    fills = d / "fills.parquet"
    data = fills.read_bytes()
    fills.unlink()
    fills.write_bytes(data[:-4] + b"\0\0\0\0")
    with pytest.raises(IntegrityBlocker, match="not a valid BookRun record"):
        rs.load(sha, file_sha256=run_file_sha256(run))


def test_no_unpickling_path(tmp_path, log, monkeypatch):
    """Review round 2 (I): the reviewer's payload (pandas -> io.pickle.pickle.loads -> os.getpid) placed as
    the run record with matching manifest and pin hashes is only bytes that fail to parse; nothing runs."""
    class R:
        def __reduce__(self):
            return (os.getpid, ())

    inner = pickle.dumps(R(), protocol=2)
    payload = (b"\x80\x05" + pickle.dumps("pandas", 5)[2:-1] + pickle.dumps("io.pickle.pickle.loads", 5)[2:-1]
               + b"\x93" + pickle.dumps(inner, 5)[2:-1] + b"\x85R.")
    rs = RunStore(tmp_path / "runs")
    run = rf.funding_run([0.01], costs=[0.001])
    sha = rs.put(run)
    d = tmp_path / "runs" / sha
    for name in ("run.json", "fills.parquet"):
        (d / name).unlink()
        (d / name).write_bytes(payload)
    man_p = tmp_path / "runs" / "MANIFEST.json"
    man = json.loads(man_p.read_text(encoding="utf-8"))
    man["runs"][sha]["sha256"] = hashlib.sha256(payload).hexdigest()
    man_p.write_text(json.dumps(man), encoding="utf-8")
    called = []
    monkeypatch.setattr(os, "getpid", lambda: called.append(1) or 0)  # the payload's call, if it ever ran
    with pytest.raises(IntegrityBlocker):
        rs.load(sha, file_sha256=hashlib.sha256(payload).hexdigest())
    assert called == []
    assert not hasattr(rawstore, "_RunUnpickler") and not hasattr(runstore, "pickle")


_UNPICKLE = {"load", "loads", "Unpickler", "read_pickle", "allow_pickle"}
_FORBIDDEN_MODULES = ("joblib", "dill", "cloudpickle", "shelve", "marshal")


def test_no_module_unpickles():
    """Static: no luna/v7 module can unpickle. ``pickle`` is imported only by runner.py, for
    ``runner.model_hash`` (``pickle.dumps`` of a fitted fold model, hashed, never loaded); no module calls
    pickle.load / loads / Unpickler, pandas.read_pickle or allow_pickle, or imports joblib / dill /
    cloudpickle / shelve / marshal."""
    root = Path(runstore.__file__).parent
    importers, offenders = [], []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(root)).replace("\\", "/")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(m == "pickle" for m in mods):
                    importers.append(rel)
                if any(m.split(".")[0] in _FORBIDDEN_MODULES for m in mods):
                    offenders.append(f"{rel}: import {mods}")
                if isinstance(node, ast.ImportFrom) and node.module == "pickle":
                    offenders.append(f"{rel}: from pickle import ...")
            elif isinstance(node, ast.Attribute) and node.attr in _UNPICKLE and \
                    isinstance(node.value, ast.Name) and node.value.id in ("pickle", "pd", "pandas", "np"):
                offenders.append(f"{rel}: {node.value.id}.{node.attr}")
            elif isinstance(node, ast.keyword) and node.arg == "allow_pickle":
                offenders.append(f"{rel}: allow_pickle=")
    assert importers == ["runner.py"] and offenders == []


def test_load_pinned_through_the_verified_opening(tmp_path, monkeypatch):
    dev = af.dev_selection()
    o = af.authorize(tmp_path / "o", monkeypatch, dev=dev)
    with k9.using(k9.K9Log(tmp_path / "k9")):
        rs = RunStore(tmp_path / "runs")
        ml = dev.runs[("A2", 72)]
        rs.put(ml)
        got = rs.load_pinned(o.auth)
        assert run_output_sha256(got) == o.auth.record["pin"]["dev_ml_run_sha256"] == run_output_sha256(ml)
        with pytest.raises(guard.HoldoutBlocked):
            rs.load_pinned(object())  # type: ignore[arg-type]  - a forged authorization


def test_window_guard_and_window_mismatch(tmp_path, log):
    rs = RunStore(tmp_path / "runs")
    hold = rf.funding_run([0.01, 0.02], lo=windows.W_HOLD[0])
    sha = rs.put(hold)
    with pytest.raises(guard.HoldoutBlocked):
        rs.load(sha, file_sha256=run_file_sha256(hold))
    run = rf.funding_run([0.01, 0.02])
    sha2 = rs.put(run)
    man_p = tmp_path / "runs" / "MANIFEST.json"
    m = json.loads(man_p.read_text(encoding="utf-8"))
    m["runs"][sha2]["window"] = [run.window[0] - D, run.window[1]]
    man_p.write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(IntegrityBlocker, match="window"):
        rs.load(sha2, file_sha256=run_file_sha256(run))


# ------------------------------------------------------------------------------- Task 26b carried minors


@pytest.mark.parametrize("dtype,kind", [("category", "json"), ("Sparse[float64, nan]", "ext"),
                                        ("datetime64[ns]", "numpy"), ("object", "numpy"), ("float64", "json"),
                                        ("int64", "ext"), ("complex128", "numpy")])
def test_decode_reapplies_the_dtype_allowlist(dtype, kind):
    """A run.json column entry whose (dtype, storage) the writer would refuse never decodes."""
    files = encode_run(rf.funding_run([0.01, 0.02], costs=[0.001, 0.0]))
    rec = json.loads(files["run.json"])
    meta = rec["frames"]["fills"]
    meta["columns"][0][1:] = [dtype, kind]
    files["run.json"] = json.dumps(rec).encode()
    with pytest.raises(IntegrityBlocker, match="persistable"):
        runstore._decode_frame("fills", meta, files[meta["file"]])


@pytest.mark.parametrize("name", ["C:fills.parquet", r"c:\x", "/etc/x", r"\\host\share\x", "..", "a/b"])
def test_manifest_file_names_with_drive_or_absolute_refused(tmp_path, log, name):
    rs = RunStore(tmp_path / "runs")
    run = rf.funding_run([0.01], costs=[0.001])
    sha = rs.put(run)
    man_p = tmp_path / "runs" / "MANIFEST.json"
    m = json.loads(man_p.read_text(encoding="utf-8"))
    m["runs"][sha]["files"] = [*m["runs"][sha]["files"], name]
    man_p.write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(IntegrityBlocker, match="invalid file name"):
        rs.load(sha, file_sha256=run_file_sha256(run))
    assert runstore._plain_name("fills.parquet") and not runstore._plain_name(name)
