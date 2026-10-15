# V7 AR equality kit: CRAN bidask 2.1.5 vs the Python transcription (DK-09, red-team m11)

S-4.5 / DEC-38 requires "equality on fixed vectors with an independent reference implementation ... CRAN
`bidask` 2.1.5, method "AR" with sign = TRUE, negative window values then set to 0". This kit runs the real R
package on fixed vectors and compares the result with `tests/v7/ar_reference.py` (and optionally `luna.v7.spread`).

## Files

| file | role |
|---|---|
| `vectors.json` | the fixture: 23 cases, prices as exact `float.hex()` strings, `null` = missing (NaN/NA) |
| `gen_vectors.py` | provenance only (regenerates `vectors.json` with numpy; cases 01-11 replay `tests/v7/test_spread.py`) |
| `run_bidask.R` | installs bidask 2.1.5 (sha256-pinned tarball) into `./Rlib`, writes `r_output.json` |
| `compare.py` | stdlib-only comparison; `--luna` additionally checks `luna.v7.spread` (needs numpy + pandas) |
| (`r_source/`) | not committed: read the bidask 2.1.5 sources from the pinned CRAN tarball |
| `r_output.json` | evidence produced on 2026-10-15 (R 4.5.3 ucrt, bidask 2.1.5, data.table 1.18.6.1) |

bidask 2.1.5 tarball sha256: `e0bf415c7e9f9859b044bf059979f811fb9dbaf68a2b41780f4b76faf5aeae9f`
(`https://cran.r-project.org/src/contrib/bidask_2.1.5.tar.gz` while current; `.../Archive/bidask/bidask_2.1.5.tar.gz`
afterwards; the script tries both). `vectors.json` sha256: `382b879ae88e68386b0dc46068e1a50b0f95cd39ab6970a1df15c96e6f346f61`.

## Run (any machine with R >= 4.1; R >= 4.5 has `tools::sha256sum`, otherwise install `digest`)

Conda (Windows/Linux/macOS), isolated env:

```
conda create -n v7-bidask-r --override-channels -c conda-forge r-base r-jsonlite r-data.table r-digest "libblas=*=*openblas"
# Windows: run Rscript with the env activated (conda activate v7-bidask-r) or with its Library\bin dirs on PATH,
# otherwise Rscript fails with "error while loading shared libraries".
cd kit
Rscript run_bidask.R vectors.json r_output.json Rlib
python -I compare.py --repo <path-to-LunaTradingBot-v7>
<path-to-repo>/.venv/Scripts/python -I compare.py --repo <path-to-repo> --luna     # optional
```

`run_bidask.R` needs internet once (tarball, and data.table/jsonlite from CRAN only if not installed). bidask has
`NeedsCompilation: no`, so no compiler is needed. It asserts `packageVersion("bidask") == "2.1.5"` and that the
package was loaded from `./Rlib`, and runs data.table single-threaded.

## Expected output

`compare.py` (transcription), as obtained on 2026-10-15:

```
PASS-COND 11a_mixed_sign_zero_mean: map=1/1 ref=1/1 sign=1/1
...
totals: ref=36/36 sign=36/36 map=32/32 roll_ref=3859/3859 roll_sign=3859/3859
PASS: 23/23 cases (1 of them PASS-COND)
```

exit status 0. `--strict` gives `FAIL: 22/23` (only 11a's signed value, see below), exit status 1.
`--luna` gives `FAIL: 22/23`: case 16 `luna_pi` differs by design (see "Known differences").

## What is compared

Window `[first, last]` = 0-based indices of the later bar of the pair products (the `ar_window` convention).
R value per window = `bidask::spread(x[first:(last+1), ], method = "AR", sign = TRUE, na.rm = TRUE)` (1-based
rows, default width = nrow -> one full-sample value), cross-checked against
`spread(x, width = c(first, last + 1), ...)` (endpoints form, check `map`). Rolling cases also run
`spread(x, width = w, ...)`: output row `r` (0-based) = `ar_window(first = r - w + 2, last = r)`.

- `ref`: `ar_window(...)` vs `max(R, 0)` (the spec quantity). `sign`: the signed value from `ar_s2` vs `R`.
- Tolerance: identical floats, both missing, or `|a-b| <= 1e-12 * max(|a|,|b|)`. Observed max relative
  difference on non-identical values: 5.7e-16 (summation-order rounding in `data.table::frollmean`).
- PASS-COND (signed checks only): case 11a has a window mean that is exactly 0 in exact arithmetic
  (-0.0008 + 0 + 0.0008); the residue (~5e-18) differs between frollmean and a left-to-right sum by 5.6e-24,
  i.e. 1e-8 of the summands' ulp scale, but sqrt(|mean|) magnifies it to 5e-7 relative. It is accepted iff the
  recovered means agree within `1e-12 * mean(|pi|)`. The truncated value (`ref`) is 0 on both sides, exactly.

## Known differences (not transcription errors)

- Case 16: a bar with valid high/low but NaN close. bidask and the transcription keep the pair (k-1, k), which
  uses only eta_k from bar k; `luna.v7.spread` drops the whole bar (any invalid price = missing bar), so that pair
  is absent. All-NaN missing bars (the S-4.5 "missing bar") agree everywhere.
- Non-positive prices: R gives log(0) = -Inf / log(<0) = NaN; the transcription and luna map them to missing.
  Not in the vectors.

## Commit the evidence (the repo owner, on the V7 branch)

```
mkdir -p docs/results/v7-bidask-ar-equality
cp kit/{vectors.json,r_output.json,run_bidask.R,compare.py,gen_vectors.py,README.md} docs/results/v7-bidask-ar-equality/
python -I docs/results/v7-bidask-ar-equality/compare.py --repo . > docs/results/v7-bidask-ar-equality/compare_output.txt
git add docs/results/v7-bidask-ar-equality
git commit -m "test(v7): CRAN bidask 2.1.5 AR equality evidence (DK-09)"
```

Do not regenerate `vectors.json` without rerunning R: `compare.py` checks `vectors_sha256` in `r_output.json`.
