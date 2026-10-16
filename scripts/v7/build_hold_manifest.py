"""DEC-19 pinned W_HOLD listing manifest: builder dry run for the controller (ruling R56 / R56b, AMB-44).

Reads ONLY single-page N2 listings (allowed before the opening, S-1.7) through
``luna.v7.fetch.Fetcher.default`` (netguard before I/O, K9 logging; a truncated listing fails closed, DK-01;
M6 keys dropped by the fetch layer), builds the canonical manifest document
(``luna.v7.manifest.build_document``) and reports the key counts per (table, freq), the number of
symbols and the sha256 the operator would pin. No N1 file is fetched.

    python scripts/v7/build_hold_manifest.py --out <dir>            # dry run: document + counts under <dir>
    python scripts/v7/build_hold_manifest.py --out <dir> --write    # also write it to the canonical location

``--write`` is refused once any opening file exists (``manifest.write_document``). Exit code 1 on any failure
(truncated listing, missing hedge, refusal), with nothing written to the canonical location.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from luna.v7 import k9, manifest  # noqa: E402
from luna.v7.fetch import Fetcher  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="output directory (FETCHES.jsonl, k9/, document, counts)")
    ap.add_argument("--write", action="store_true", help="also write the canonical manifest to pin")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with k9.using(k9.K9Log.under(out)), Fetcher.default(out) as f:
        try:
            doc = manifest.build_document(f)
        except Exception as e:  # noqa: BLE001 - reported, fail closed
            print(f"FAILED (nothing pinned): {type(e).__name__}: {e}", file=sys.stderr)
            return 1
    data = manifest.canonical_bytes(doc)
    sha = hashlib.sha256(data).hexdigest()
    (out / "W_HOLD_MANIFEST.dryrun.json").write_bytes(data)
    report = {"sha256": sha, "symbols": len(doc["symbols"]), "keys": len(doc["entries"]),
              "entries": len(doc["entries"]), "file_bytes": len(data), "counts": manifest.counts(doc)}
    (out / "W_HOLD_MANIFEST.counts.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))
    print(f"manifest: {report['entries']} entries, {report['file_bytes']} bytes")
    if a.write:
        p, sha2 = manifest.write_document(doc)
        print(f"written {p} sha256 {sha2} ({len(doc['entries'])} entries, {p.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
