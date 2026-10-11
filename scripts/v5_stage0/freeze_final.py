"""Protocol §2.2 (rev 3): build the final frozen input copy ``var/history_v5/frozen`` (V4 layout +
5-minute spread snapshots with provenance). Never overwrites; prints the manifest sha256.

Usage: freeze_final.py <var_dir> <v4_instruments.json> <dst> <v5_spreads_dir>
"""
import json
import sys
from pathlib import Path

from luna.carry import frozen
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backfill_spreads import ASSETS  # noqa: E402

var, inst, dst, sp = (Path(a) for a in sys.argv[1:5])
man = frozen.freeze_inputs(var, inst, dst, assets=ASSETS, history_v5_spreads=sp)
frozen.verify_frozen(dst)
print(json.dumps({"files": len(man["files"]), "missing_sources": man["missing_sources"],
                  "manifest_sha256": frozen.sha256_file(dst / "manifest.json")}))
