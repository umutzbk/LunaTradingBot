# Luna Research V4 — Stage 1

**EXPLORATORY — reused development period; not generalization evidence.** Stage 1 alone can never give GO; GO evidence comes only from the forward evaluation (Stage 2).

**DEPLOYABLE NOW: NO**
- (a) UK retail access to perpetual products is restricted; eligibility not confirmed.
- (b) No live broker in the code.
- (c) No synchronised two-venue execution or margin management.
- (d) No maker fill model.

Window: {'eval_start': '2025-11-05T00:00:00+00:00', 'end': '2026-07-01T00:00:00+00:00'} (238 days, DQ-valid 238).
- Instrument metadata (min order, step) is the freeze-time snapshot applied to the past: a known anachronism (§7.1).

Stage-1 candidate (information only): none

Hypotheses (summary only; the forward set is per config): H1: killed, H2: advances, H5: killed

## Stage 1 decision (ADVANCE TO FORWARD / KILL)

KILL reasons: (a) mean < 0 and b=28 one-sided 95% upper bound < 0; (b) carry component does not cover costs; (c) no position ever open. C1-C7 / M1-M4 are information only.

| config | decision | mean daily net | UB95 b=28 | LB95 b=28 | LB95 b=14 | carry value (H1/H5) | mean carry_net (H2) |
|---|---|---|---|---|---|---|---|
| H1-Y10 | KILL (a, b) | -0.000136 | -0.000097 | -0.000195 | -0.000205 | -16.14 | n/a |
| H1-Y20 | KILL (a, b) | -0.000046 | -0.000042 | -0.000092 | -0.000091 | -5.91 | n/a |
| H2-L7-k1 | KILL (b) | 0.000141 | 0.002829 | -0.003213 | -0.003679 | n/a | -0.000015 |
| H2-L7-k2 | KILL (b) | 0.000283 | 0.002220 | -0.001419 | -0.001757 | n/a | -0.000004 |
| H2-L30-k1 | ADVANCE TO FORWARD | -0.001120 | 0.002954 | -0.004773 | -0.005364 | n/a | 0.000127 |
| H2-L30-k2 | ADVANCE TO FORWARD | 0.000469 | 0.002065 | -0.001705 | -0.001884 | n/a | 0.000113 |
| H5-HFWB | KILL (a, b) | -0.000169 | -0.000139 | -0.000244 | -0.000243 | -21.19 | n/a |

### C1-C7 / M1-M4 (information only)

| config | Stage 1 decision | C1 | C2 | C3 | C4 | C5 | C6 | C7 | M1 | M2 | M3 | M4 | p b=28 | p b=14 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| H1-Y10 | KILL (a, b) | PASS | FAIL | FAIL | FAIL | PASS | FAIL | FAIL | PASS | PASS | PASS | PASS | 1 | 0.9998 |
| H1-Y20 | KILL (a, b) | FAIL | FAIL | FAIL | PASS | PASS | FAIL | FAIL | PASS | PASS | FAIL | PASS | 0.9998 | 0.999 |
| H2-L7-k1 | KILL (b) | PASS | FAIL | FAIL | PASS | FAIL | FAIL | FAIL | PASS | PASS | PASS | PASS | 0.5138 | 0.5125 |
| H2-L7-k2 | KILL (b) | PASS | FAIL | PASS | FAIL | FAIL | FAIL | FAIL | PASS | PASS | PASS | PASS | 0.355 | 0.4285 |
| H2-L30-k1 | ADVANCE TO FORWARD | PASS | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | PASS | PASS | PASS | PASS | 0.6528 | 0.6716 |
| H2-L30-k2 | ADVANCE TO FORWARD | PASS | FAIL | PASS | PASS | FAIL | PASS | FAIL | PASS | PASS | PASS | PASS | 0.4243 | 0.4444 |
| H5-HFWB | KILL (a, b) | PASS | FAIL | FAIL | FAIL | PASS | FAIL | FAIL | PASS | PASS | PASS | PASS | 1 | 1 |

## Sensitivity rows and P&L decomposition

| config | row | mean daily | Sharpe | max DD | spot price | perp price | funding | fees | spread | liq. penalty | entries (carry) / fills (H2) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| H1-Y10 | PRIMARY | -0.000136 | -4.181 | 0.033 | -32.81 | 32.04 | 2.02 | 15.87 | 2.29 | 0.00 | 15 entries |
| H1-Y10 | S-SPREAD10 | -0.000128 | -4.046 | 0.031 | -32.80 | 32.06 | 2.02 | 15.90 | 1.36 | 0.00 | 15 entries |
| H1-Y10 | S-COST2X | -0.000263 | -4.710 | 0.062 | -33.01 | 33.95 | 1.98 | 30.68 | 4.44 | 0.00 | 15 entries |
| H1-Y10 | S-MAKER | -0.000042 | -2.111 | 0.011 | -32.72 | 35.58 | 2.11 | 7.97 | 2.34 | 0.00 | 15 entries |
| H1-Y20 | PRIMARY | -0.000046 | -2.296 | 0.012 | -35.72 | 35.89 | 0.95 | 5.99 | 0.88 | 0.00 | 6 entries |
| H1-Y20 | S-SPREAD10 | -0.000043 | -2.227 | 0.011 | -35.73 | 35.91 | 0.95 | 5.99 | 0.53 | 0.00 | 6 entries |
| H1-Y20 | S-COST2X | -0.000100 | -2.894 | 0.024 | -35.62 | 35.74 | 0.96 | 11.85 | 1.74 | 0.00 | 6 entries |
| H1-Y20 | S-MAKER | -0.000021 | -1.487 | 0.006 | -35.77 | 35.98 | 0.95 | 2.97 | 0.88 | 0.00 | 6 entries |
| H2-L7-k1 | PRIMARY | 0.000141 | 0.097 | 0.301 | 0.00 | -13.56 | 9.35 | 6.52 | 3.96 | 0.00 | 104 fills |
| H2-L7-k1 | S-SPREAD10 | 0.000141 | 0.097 | 0.301 | 0.00 | -13.56 | 9.35 | 6.52 | 3.96 | 0.00 | 104 fills |
| H2-L7-k1 | S-COST2X | -0.000062 | -0.042 | 0.309 | 0.00 | -15.30 | 9.08 | 12.75 | 7.77 | 0.00 | 104 fills |
| H2-L7-k1 | S-MAKER | 0.000157 | 0.108 | 0.300 | 0.00 | -16.54 | 9.40 | 2.62 | 3.97 | 0.00 | 104 fills |
| H2-L7-k2 | PRIMARY | 0.000283 | 0.338 | 0.223 | 0.00 | 10.52 | 7.76 | 5.26 | 3.00 | 0.00 | 185 fills |
| H2-L7-k2 | S-SPREAD10 | 0.000283 | 0.338 | 0.223 | 0.00 | 10.52 | 7.76 | 5.26 | 3.00 | 0.00 | 185 fills |
| H2-L7-k2 | S-COST2X | 0.000144 | 0.173 | 0.231 | 0.00 | 9.70 | 7.73 | 10.38 | 5.92 | 0.00 | 185 fills |
| H2-L7-k2 | S-MAKER | 0.000337 | 0.406 | 0.220 | 0.00 | 11.03 | 7.80 | 2.12 | 3.01 | 0.00 | 186 fills |
| H2-L30-k1 | PRIMARY | -0.001120 | -0.681 | 0.436 | 0.00 | -91.82 | 10.86 | 2.56 | 1.24 | 0.00 | 73 fills |
| H2-L30-k1 | S-SPREAD10 | -0.001120 | -0.681 | 0.436 | 0.00 | -91.82 | 10.86 | 2.56 | 1.24 | 0.00 | 73 fills |
| H2-L30-k1 | S-COST2X | -0.001169 | -0.709 | 0.442 | 0.00 | -90.19 | 10.79 | 5.09 | 2.47 | 0.00 | 72 fills |
| H2-L30-k1 | S-MAKER | -0.001139 | -0.686 | 0.443 | 0.00 | -94.63 | 10.92 | 1.03 | 1.24 | 0.00 | 74 fills |
| H2-L30-k2 | PRIMARY | 0.000469 | 0.447 | 0.174 | 0.00 | 9.95 | 10.69 | 2.27 | 1.08 | 0.00 | 124 fills |
| H2-L30-k2 | S-SPREAD10 | 0.000469 | 0.447 | 0.174 | 0.00 | 9.95 | 10.69 | 2.27 | 1.08 | 0.00 | 124 fills |
| H2-L30-k2 | S-COST2X | 0.000416 | 0.396 | 0.173 | 0.00 | 9.75 | 10.62 | 4.49 | 2.15 | 0.00 | 125 fills |
| H2-L30-k2 | S-MAKER | 0.000486 | 0.465 | 0.173 | 0.00 | 9.81 | 10.74 | 0.91 | 1.09 | 0.00 | 125 fills |
| H5-HFWB | PRIMARY | -0.000169 | -5.219 | 0.040 | -5.13 | 5.44 | 1.90 | 20.06 | 3.03 | 0.00 | 17 entries |
| H5-HFWB | S-SPREAD10 | -0.000154 | -5.090 | 0.037 | -5.10 | 6.04 | 1.90 | 20.11 | 1.86 | 0.00 | 17 entries |
| H5-HFWB | S-COST2X | -0.000348 | -6.090 | 0.081 | -5.60 | 6.02 | 1.81 | 38.57 | 5.85 | 0.00 | 17 entries |
| H5-HFWB | S-MAKER | -0.000088 | -3.868 | 0.022 | -4.91 | 5.14 | 1.89 | 10.09 | 3.09 | 0.00 | 17 entries |

## Counters (PRIMARY)

| config | skipped_actions | min_order_skips | funding_unknown_hours | liquidations | perp_spread_fallbacks | hedge_rebalances | margin_reductions | flat_weeks | outage_days | funding_unknown_flag |
|---|---|---|---|---|---|---|---|---|---|---|
| H1-Y10 | 0 | 57 | 5 | 0 | 0 | 1 | 0 | 0 | 0 | ok |
| H1-Y20 | 0 | 0 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | ok |
| H2-L7-k1 | 0 | 6 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | ok |
| H2-L7-k2 | 0 | 20 | 16 | 0 | 0 | 0 | 0 | 0 | 0 | ok |
| H2-L30-k1 | 0 | 13 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | ok |
| H2-L30-k2 | 0 | 41 | 16 | 0 | 0 | 0 | 0 | 0 | 0 | ok |
| H5-HFWB | 0 | 66 | 4 | 0 | 0 | 1 | 0 | 0 | 0 | ok |

## Nulls (PRIMARY; never GO, never promoted)

| config | null | Sharpe | mean daily | entries (carry) / fills (H2) |
|---|---|---|---|---|
| H1-Y10 | N-RANDENTRY:1:H1-Y10 | -6.618 | -0.000192 | 15 entries |
| H1-Y10 | N-RANDENTRY:2:H1-Y10 | -6.030 | -0.000155 | 15 entries |
| H1-Y10 | N-RANDENTRY:3:H1-Y10 | -6.709 | -0.000157 | 15 entries |
| H1-Y10 | N-RANDENTRY:4:H1-Y10 | -6.180 | -0.000173 | 15 entries |
| H1-Y10 | N-RANDENTRY:5:H1-Y10 | -5.740 | -0.000151 | 15 entries |
| H1-Y10 | N-ALWAYS:H1-Y10 | -3.752 | -0.000085 | 8 entries |
| H1-Y20 | N-RANDENTRY:1:H1-Y20 | -4.525 | -0.000068 | 6 entries |
| H1-Y20 | N-RANDENTRY:2:H1-Y20 | -3.806 | -0.000071 | 6 entries |
| H1-Y20 | N-RANDENTRY:3:H1-Y20 | -4.660 | -0.000075 | 6 entries |
| H1-Y20 | N-RANDENTRY:4:H1-Y20 | -4.948 | -0.000092 | 6 entries |
| H1-Y20 | N-RANDENTRY:5:H1-Y20 | -3.720 | -0.000071 | 6 entries |
| H1-Y20 | N-ALWAYS:H1-Y20 | -3.752 | -0.000085 | 8 entries |
| H2-L7-k1 | N-SHUFFLE:1:H2-L7-k1 | 0.191 | 0.000205 | 123 fills |
| H2-L7-k1 | N-SHUFFLE:2:H2-L7-k1 | -1.097 | -0.001019 | 123 fills |
| H2-L7-k1 | N-SHUFFLE:3:H2-L7-k1 | 0.786 | 0.000750 | 124 fills |
| H2-L7-k1 | N-SHUFFLE:4:H2-L7-k1 | 0.330 | 0.000355 | 128 fills |
| H2-L7-k1 | N-SHUFFLE:5:H2-L7-k1 | -1.349 | -0.001481 | 125 fills |
| H2-L7-k2 | N-SHUFFLE:1:H2-L7-k2 | -1.163 | -0.000841 | 239 fills |
| H2-L7-k2 | N-SHUFFLE:2:H2-L7-k2 | 1.014 | 0.000832 | 223 fills |
| H2-L7-k2 | N-SHUFFLE:3:H2-L7-k2 | 3.372 | 0.002099 | 241 fills |
| H2-L7-k2 | N-SHUFFLE:4:H2-L7-k2 | 0.306 | 0.000223 | 234 fills |
| H2-L7-k2 | N-SHUFFLE:5:H2-L7-k2 | -0.967 | -0.000848 | 231 fills |
| H2-L30-k1 | N-SHUFFLE:1:H2-L30-k1 | -0.047 | -0.000055 | 124 fills |
| H2-L30-k1 | N-SHUFFLE:2:H2-L30-k1 | -0.089 | -0.000071 | 127 fills |
| H2-L30-k1 | N-SHUFFLE:3:H2-L30-k1 | -0.232 | -0.000184 | 130 fills |
| H2-L30-k1 | N-SHUFFLE:4:H2-L30-k1 | 0.651 | 0.000652 | 125 fills |
| H2-L30-k1 | N-SHUFFLE:5:H2-L30-k1 | 1.448 | 0.001120 | 124 fills |
| H2-L30-k2 | N-SHUFFLE:1:H2-L30-k2 | -1.211 | -0.000795 | 224 fills |
| H2-L30-k2 | N-SHUFFLE:2:H2-L30-k2 | -1.683 | -0.001177 | 229 fills |
| H2-L30-k2 | N-SHUFFLE:3:H2-L30-k2 | 0.281 | 0.000200 | 223 fills |
| H2-L30-k2 | N-SHUFFLE:4:H2-L30-k2 | 1.071 | 0.000666 | 232 fills |
| H2-L30-k2 | N-SHUFFLE:5:H2-L30-k2 | 2.338 | 0.001752 | 230 fills |
| H5-HFWB | N-RANDENTRY:1:H5-HFWB | -6.526 | -0.000174 | 17 entries |
| H5-HFWB | N-RANDENTRY:2:H5-HFWB | -6.765 | -0.000198 | 17 entries |
| H5-HFWB | N-RANDENTRY:3:H5-HFWB | -7.001 | -0.000187 | 17 entries |
| H5-HFWB | N-RANDENTRY:4:H5-HFWB | -6.375 | -0.000193 | 17 entries |
| H5-HFWB | N-RANDENTRY:5:H5-HFWB | -5.969 | -0.000201 | 17 entries |
| H5-HFWB | N-ALWAYS:H5-HFWB | -3.752 | -0.000085 | 8 entries |

## Diagnostics (not configs; never part of GO / NO-GO)

- D-PERSIST: {'weeks': 33, 'mean_spearman': 0.45108225108225103, 'ar1_daily': 0.28951481597040896, 'half_life_days': 0.5591931334136562, 'warning': False}
- D-H5CELLS: {'HF-WB': {'n': 64, 'next7d_funding_ann': 0.000985616710260702}, 'HF-NB': {'n': 29, 'next7d_funding_ann': -0.02118987442962534}, 'LF-WB': {'n': 1587, 'next7d_funding_ann': -0.005709703555518778}, 'LF-NB': {'n': 1482, 'next7d_funding_ann': -0.030097823976430106}}
- D-DISP: {'H2-L7-k1': {'above': {'n': 102, 'mean': 0.005475857308232493}, 'below_or_equal': {'n': 136, 'mean': -0.00385954031799735}}, 'H2-L7-k2': {'above': {'n': 102, 'mean': 0.0031011756666338586}, 'below_or_equal': {'n': 136, 'mean': -0.001831278935563121}}, 'H2-L30-k1': {'above': {'n': 102, 'mean': 0.0013064820874906856}, 'below_or_equal': {'n': 136, 'mean': -0.0029404997290514183}}, 'H2-L30-k2': {'above': {'n': 102, 'mean': 0.0018997846758991386}, 'below_or_equal': {'n': 136, 'mean': -0.0006043318537573348}}}
- H2 exposure / beta: {'H2-L7-k1': {'residual_btc_beta': -0.02542502918983047, 'mean_net_exposure': -0.000866134310255453, 'long_pnl': -54.15206170555561, 'short_pnl': 39.45312281483889, 'mean_max_share': 0.5129750534312798, 'mean_herfindahl': 0.491488911671929, 'asset_pnl': {'HYPE': -50.58678864197896, 'ZEC': -96.22932305407598, 'ENA': -20.438292130927437, 'SUI': 39.20347440029153, 'BNB': -3.5234598859016795, 'AVAX': -16.354663843315905, 'NEAR': 10.063106177219185, 'XLM': -16.207628804505486, 'PEPE': -15.892463825963198, 'TAO': 130.64312718691372, 'BTC': 13.233117738484678, 'SOL': -1.317799083562592, 'ETH': 18.701218217954434, 'ADA': -5.992563341349014}}, 'H2-L7-k2': {'residual_btc_beta': 0.044387171501613705, 'mean_net_exposure': 0.008406934900963586, 'long_pnl': -50.62513700058321, 'short_pnl': 60.64770911550217, 'mean_max_share': 0.26348009294112845, 'mean_herfindahl': 0.2458401002680411, 'asset_pnl': {'HYPE': -9.031288154679716, 'PUMP': -11.141652197801065, 'XLM': 6.163446988799682, 'ZEC': -61.17533709662007, 'AVAX': -7.856708099985511, 'ENA': -9.62478672582136, 'SUI': 18.921838993831404, 'BNB': -0.3239190471174413, 'NEAR': -22.699540460508832, 'BTC': 10.98231753699985, 'TAO': 83.07763919173576, 'PEPE': -11.731636386014223, 'DOGE': 8.01318424877977, 'SOL': -4.917611252376902, 'UNI': -0.13295890784577408, 'ETH': 12.312630199448499, 'ADA': -7.0438287009121545, 'XRP': 1.1585540135527532, 'FET': 10.340059202699905, 'LINK': 4.732168768754403}}, 'H2-L30-k1': {'residual_btc_beta': 0.024485560382051148, 'mean_net_exposure': -0.00997048277602738, 'long_pnl': -55.758253784520264, 'short_pnl': -29.00385322662204, 'mean_max_share': 0.5143846925772634, 'mean_herfindahl': 0.4919870782107881, 'asset_pnl': {'SUI': 35.745067858181656, 'ZEC': -85.66250306837216, 'AVAX': 11.097212677844285, 'HYPE': -79.74403309347919, 'NEAR': -59.695148436106116, 'BTC': 21.96370325399563, 'BNB': -11.114735499597405, 'TAO': 56.731433891389294, 'ETH': 25.91689540500175}}, 'H2-L30-k2': {'residual_btc_beta': 0.08383662800018717, 'mean_net_exposure': 0.006418949186470073, 'long_pnl': -16.58308432584718, 'short_pnl': 33.87448730500868, 'mean_max_share': 0.26377266861227594, 'mean_herfindahl': 0.24607065528015468, 'asset_pnl': {'ENA': -33.00074050061986, 'SUI': 16.511467544779062, 'UNI': 10.295244398901652, 'ZEC': -8.839709260456269, 'AVAX': 4.035940457547761, 'BTC': 11.501377720870538, 'HYPE': -52.71763683654588, 'XLM': 5.018369558904011, 'NEAR': -16.369437960035498, 'DOGE': 4.097753121760215, 'BNB': -1.9190109702797857, 'TAO': 41.9426187637679, 'PEPE': -5.984851847448885, 'ETH': 27.073057258167122, 'SOL': 15.646961529849442}}}
- H5 vs H1-Y10 (information): {'H5-HFWB': -5.219495567157518, 'H1-Y10': -4.180749668171599}
- Venue basis: {'ADA': {'p05': -0.0009257916066258143, 'p50': 8.596338176170004e-05, 'p95': 0.0010662486084416778, 'acf1_daily': 0.4389514027771345, 'n_days': 268}, 'AVAX': {'p05': -0.0014846235418874976, 'p50': 0.0, 'p95': 0.0014743744105311179, 'acf1_daily': 0.5458098648199742, 'n_days': 268}, 'BNB': {'p05': -0.0012222894347689306, 'p50': 2.9388426837417825e-05, 'p95': 0.0011889956033604987, 'acf1_daily': 0.5711663471036854, 'n_days': 268}, 'BTC': {'p05': -0.000357448468039534, 'p50': 8.37770835985241e-05, 'p95': 0.0005719081169417439, 'acf1_daily': 0.8169898634523994, 'n_days': 268}, 'DOGE': {'p05': -0.0007954357323011751, 'p50': 0.00011271061748907574, 'p95': 0.0009773328938443293, 'acf1_daily': 0.49492592086490905, 'n_days': 268}, 'ENA': {'p05': -0.003627752601095024, 'p50': 2.1687269572856493e-05, 'p95': 0.0036506634847655027, 'acf1_daily': 0.49169276695540254, 'n_days': 268}, 'ETH': {'p05': -0.00047141743364020883, 'p50': 4.179684912697379e-05, 'p95': 0.0005272504420363866, 'acf1_daily': 0.7436744183045367, 'n_days': 268}, 'FET': {'p05': -0.005178543364871429, 'p50': 0.00024013932319255638, 'p95': 0.005515886759603416, 'acf1_daily': 0.45211127797290784, 'n_days': 268}, 'HYPE': {'p05': -0.002004961277452399, 'p50': 0.0001953125000000444, 'p95': 0.0021657663226943838, 'acf1_daily': 0.5582714665653384, 'n_days': 154}, 'LINK': {'p05': -0.0010314845173962418, 'p50': 0.00011873275304408182, 'p95': 0.0012554368796581445, 'acf1_daily': 0.47195510378837935, 'n_days': 268}, 'NEAR': {'p05': -0.002085968293657442, 'p50': 0.00016300914949762824, 'p95': 0.0024744733961781415, 'acf1_daily': 0.4557672263572106, 'n_days': 268}, 'PEPE': {'p05': -0.0016101108194131464, 'p50': 6.0560183068658624e-05, 'p95': 0.0016544491334677391, 'acf1_daily': 0.43909257630519544, 'n_days': 268}, 'PUMP': {'p05': -0.00545851528384278, 'p50': 0.0, 'p95': 0.005637267995466463, 'acf1_daily': 0.5316559591468911, 'n_days': 268}, 'SOL': {'p05': -0.0006090041467591845, 'p50': 7.0556692653434e-05, 'p95': 0.0006302207754272926, 'acf1_daily': 0.536457680709161, 'n_days': 268}, 'SUI': {'p05': -0.0010330578512396382, 'p50': 0.00014117314957051708, 'p95': 0.0013984356004285597, 'acf1_daily': 0.44660296149637374, 'n_days': 268}, 'TAO': {'p05': -0.0021783531785609656, 'p50': 9.200263195097147e-05, 'p95': 0.0021387916403314833, 'acf1_daily': 0.5226184182121013, 'n_days': 268}, 'UNI': {'p05': -0.0022443169195924704, 'p50': 0.0, 'p95': 0.0018924613818237285, 'acf1_daily': 0.5174324788857694, 'n_days': 268}, 'XLM': {'p05': -0.0018820219685601602, 'p50': 5.1606979330598435e-05, 'p95': 0.0018526010556886296, 'acf1_daily': 0.5405367391952268, 'n_days': 268}, 'XRP': {'p05': -0.0005061127412629984, 'p50': 6.109725044667336e-05, 'p95': 0.0006331821644490754, 'acf1_daily': 0.6427561588627231, 'n_days': 268}, 'ZEC': {'p05': -0.0031012970435534692, 'p50': -9.53425179959222e-05, 'p95': 0.0018763726945415504, 'acf1_daily': 0.5996493752277445, 'n_days': 268}}

## Market data counters (all runs)

valuation_stale: 0
