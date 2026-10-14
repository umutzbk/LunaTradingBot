"""RT-5 / S0-14 re-run after the final editorial clarifications (EC-1, EC-9). Identical to rt5_v3_r3.py except:

  EC-1  instance_end_known_at = close_time(last volume > 0 15m bar) + 7 * 24 h (canonical; replaces the 00:00 form).
  EC-9  funding reconciliation (DEC-29 / DEC-40 / DEC-47): a record without an explicit interval field whose spacing
        from the previous record of the same instance is within 60 s of a standard interval (1, 2, 4, 8 h) is
        reconciled by DEC-29 inference even when that interval differs from (or there is no determined) previous
        nominal interval -> valid, provenance flag interval_transition = true. Records reconciled as before keep
        their status. Every other record is genuinely unreconciled -> invalid (individually; no propagation).

Outcome-free, W_DEV only, same inputs and constraints as v3. Usage: python rt5_v4_ec.py <stage0 dir> <out dir>
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import rt5_v3_r3 as r3  # noqa: E402

H, MIN = r3.H, r3.MIN
STD = (1, 2, 4, 8)


def end_known(b):
    """EC-1: close of the last volume > 0 15m bar (b) + 7 * 24 h."""
    return None if b is None else b + 7 * r3.D


def reconciled(ts, iv_explicit, iv_inferred, inst):
    ok, ii, nom = _v3_reconciled(ts, iv_explicit, iv_inferred, inst)
    trans = np.zeros(len(ts), bool)
    for i in np.where(~ok)[0]:
        if i == 0 or ii[i] != ii[i - 1] or not np.isnan(iv_explicit[i]):
            continue
        sp = ts[i] - ts[i - 1]
        if min(abs(c * H - sp) for c in STD) <= MIN:
            trans[i] = True
    reconciled.transitions += int(trans.sum())
    return ok | trans, ii, nom


reconciled.transitions = 0
_v3_reconciled = r3.reconciled
r3.end_known = end_known
r3.reconciled = reconciled

if __name__ == "__main__":
    r3.main()
    print("interval_transition records (calls may repeat per symbol):", reconciled.transitions, flush=True)
