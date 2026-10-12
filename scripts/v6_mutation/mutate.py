"""Mutation check: apply one mutation at a time, run the given tests, expect a failure, restore.

Usage: python scripts/v6_mutation/mutate.py <mutations.json>
mutations.json: [{"file": "...", "old": "...", "new": "...", "tests": "tests/v6/..."}]
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main(spec: str) -> int:
    muts = json.loads(Path(spec).read_text(encoding="utf-8"))
    bad = 0
    for m in muts:
        p = ROOT / m["file"]
        src = p.read_text(encoding="utf-8")
        if src.count(m["old"]) != 1:
            print(f"SKIP (not unique/absent): {m['file']}: {m['old'][:60]!r}")
            bad += 1
            continue
        p.write_text(src.replace(m["old"], m["new"]), encoding="utf-8", newline="\n")
        try:
            r = subprocess.run([sys.executable, "-m", "pytest", *m["tests"].split(), "-q", "-x", "-p", "no:cacheprovider"],
                               cwd=ROOT, capture_output=True, text=True)
        finally:
            p.write_text(src, encoding="utf-8", newline="\n")
        killed = r.returncode != 0
        bad += not killed
        print(("KILLED  " if killed else "SURVIVED"), m.get("name", m["old"][:50]))
    return bad


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
