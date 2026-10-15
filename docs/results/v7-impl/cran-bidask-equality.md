# CRAN `bidask` 2.1.5 AR equality test (DK-09, red-team m11, S-4.5 / DEC-38)

| | |
|---|---|
| Date | 2026-10-15 |
| Environment | R 4.5.3 (conda-forge, temporary env `v7-bidask-r`, removed after the run), data.table 1.18.6.1, jsonlite 2.0.0 |
| Package | `bidask` 2.1.5 from the sha256-pinned CRAN tarball `e0bf415c7e9f9859b044bf059979f811fb9dbaf68a2b41780f4b76faf5aeae9f`; `packageVersion` asserted |
| Call | `bidask::spread(x, width, method = "AR", sign = TRUE, na.rm = TRUE)` |
| Kit | `scripts/v7/cran_bidask/` (vectors, R script, comparator, README, R output, comparator outputs) |
| Vectors | `vectors.json` sha256 `382b879ae88e68386b0dc46068e1a50b0f95cd39ab6970a1df15c96e6f346f61` |
| R output | `r_output.json` sha256 `c612195a72ffb461fa8bdde82ae1b4c83ad4e89b26b419a2e874850eb2086e10` |

## Result

- **Transcription (`tests/v7/ar_reference.py`) vs R: PASS, 23 / 23 cases.**
  - Coverage: 36 windows, 3,859 rolling rows and 32 mapping cross-checks.
  - The maximum relative difference is 5.7e-16 (849 values are not bit-identical).
  - **Case 11a:** the window mean is exactly 0 in exact arithmetic.
    - The truncated S-4.5 value is exactly 0 on both sides.
    - The signed value differs by 5e-7 relative. sqrt magnifies a 5.6e-24 rounding residue from data.table's summation.
    - The case passes under the stated rule: the means agree within 1e-12 · mean|π|. With `--strict` the result is 22 / 23.
- **Mapping (from the R source):** row r (0-based) of `spread(width = w)` equals `ar_window(r − w + 2, r)` before `max(·, 0)`. `rmean(…, shift = 1)` is a data.table `frollmean` over w − 1 pair products. Width 96 is exactly the S-4.5 95-pair window. **There is no discrepancy in the transcription.**
- **`luna.v7.spread.half_spread` vs R:** every window and rolling row on whole bars matches (31 / 31 and 3,810 / 3,810).
  - One case differs: case 16, a bar that has high and low but a NaN close. bidask and the transcription keep the pair (k − 1, k). `luna.v7.spread` treats the partially missing bar as a missing bar.
  - Binance klines never carry a partial OHLC row, and the V7 ingest refuses a non-finite price field. The divergence is therefore unreachable on V7 inputs. It is recorded in AMB-06 as an implementation clarification (fail closed).

DK-09 is closed by this run.
