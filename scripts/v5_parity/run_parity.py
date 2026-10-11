"""One-command, reproducible W parity run (protocol §5): verify the frozen copy against its pinned
manifest sha, create a temporary detached worktree of the frozen V4 code, run the V4 reference
harness, gate it against the committed V4 summary, run Layer A and Layer B, remove the worktree.

Usage (from the V5 worktree):
  .venv/Scripts/python scripts/v5_parity/run_parity.py <frozen_root> <manifest_sha256> <out_dir>
Exit code 0 only if every gate passes.
"""
import json, subprocess, sys
from pathlib import Path

from luna.carry import frozen

V4_COMMIT = "b074b69"
HERE = Path(__file__).resolve().parent
V5 = HERE.parents[1]
MAIN = V5.parent / "LunaTradingBot"
V4_TREE = V5.parent / "LunaTradingBot-v4"
SUMMARY = V4_TREE / "docs" / "results" / "v4-stage1" / "summary.json"


def run(cmd, **kw):
    print("+", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(list(map(str, cmd)), check=True, **kw)


def main(root: Path, sha: str, out: Path) -> int:
    frozen.verify_frozen(root, sha)  # refuses before anything runs
    out.mkdir(parents=True, exist_ok=True)
    wt = V5.parent / "LunaTradingBot-v4ref"
    if wt.exists():
        raise SystemExit(f"{wt} exists: remove it first")
    run(["git", "-C", MAIN, "worktree", "add", "--detach", wt, V4_COMMIT])
    try:
        env = {**__import__("os").environ, "PYTHONPATH": str(wt)}
        run([V4_TREE / ".venv" / "Scripts" / "python.exe", HERE / "v4_reference_harness.py", root, out / "ref"], cwd=wt, env=env)
    finally:
        run(["git", "-C", MAIN, "worktree", "remove", "--force", wt])
    py = sys.executable
    run([py, HERE / "compare_reference.py", out / "ref" / "reference_summary.json", SUMMARY, out / "ref" / "reference_gate.json"])
    run([py, HERE / "run_layer_a.py", root, sha, out / "ref", out / "layer_a"])
    run([py, HERE / "run_layer_b.py", root, sha, out / "ref", out / "layer_b"])
    gate = json.loads((out / "ref" / "reference_gate.json").read_text())["gate"] == "PASS"
    a = json.loads((out / "layer_a" / "layer_a_report.json").read_text())
    b = json.loads((out / "layer_b" / "layer_b_report.json").read_text())
    res = {"reference": gate, "A": {k: v["pass"] for k, v in a.items()}, "B": {k: v["pass"] for k, v in b.items()}}
    ok = gate and all(res["A"].values()) and all(res["B"].values())
    res["PARITY"] = "PASS" if ok else "FAIL"
    (out / "parity_verdict.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])))
