# V5 W parity raporu (2026-10-11): **PARITY PASS** (A, B1, B2, B3)

> Yalnızca zaten yayımlanmış V4 H2-L30-k2 sonucunun yeniden üretimi (protokol R1 istisnası).
> - Yeni V5 getirisi yok, W/D/C/E karşılaştırması yok.
> - Holdout okunmadı, V4 dosyaları değişmedi, paper config değişmedi, `live_enabled=false`.
> - Ham çıktılar: `var/ops/v5_parity/{ref,layer_a,layer_b,stress_A,stress_B}/`.

**Girdi:** `var/history_v5/frozen_parity/`, manifest sha256 `88633d83…abc94`. Her koşu manifest'i doğrulayarak açtı.

## 1. Referans üretimi (§5.1)

- **Kurulum:** Detached worktree `LunaTradingBot-v4ref` @ `b074b69`, V4 venv. Harness `scripts/v5_parity/v4_reference_harness.py` V4 kodunu import ediyor, değiştirmiyor. Fill ve değerleme kaydını salt-okunur sarmalayıcılarla tutuyor.
- **Sonuç:** Commit edilmiş `summary.json` ile karşılaştırma **PASS**: 71 kontrol, max |Δ| = 0. Kontrol edilenler:
  - `mean_daily`, `sharpe`, `max_drawdown`, `worst_day`, `es975`, `cost2x_mean_daily`;
  - stage1 `ub95_b28`, `lb95_b28`, `lb95_b14`, `carry_net_mean` ve karar;
  - PRIMARY ve S-COST2X için final equity, P&L kalemleri (funding 10,6928, ücret 2,2689, spread 1,0818, fiyat 9,9492), bütün sayaçlar ve giriş sayıları (124 / 125).
- **Tam fill günlüğü:** PRIMARY 124 rebalance fill'i; margin, likidasyon ve delisting fill'i 0. S-COST2X 125 fill.

## 2. Katman A: W-V4 (`luna/carry/parity.py`)

V4 H2 kuralları V5 `Portfolio` + `FundingLedger` + `FundingStore` üzerinde. `CarryCore` kullanılmıyor.

| Kalem (§5.4) | PRIMARY | S-COST2X | Tolerans |
|---|---|---|---|
| Fill sayısı; ts, varlık ve kaynak birebir | 124/124 | 125/125 | birebir |
| dq, px, half spread, ceza, q_before / q_after (göreli) | 0 | 0 | 1e-9 |
| Günlük funding / ücret / spread / fiyat P&L / ceza (USD) | ≤ 4,6e-13 | ≤ 4,6e-13 | 1e-9 |
| Günlük getiri, carry_net | ≤ 1,8e-15 | ≤ 1,8e-15 | 1e-10 |
| Günlük equity | 2,4e-12 | 3,6e-12 | 1e-6 |
| Gross, active, n_positions, günlük tutulan set | birebir | birebir | birebir |
| Final equity / mean / Sharpe / MDD | 1,2e-12 / 1,6e-17 / 1,7e-14 / 7,8e-16 | 2,3e-13 / 1e-18 / 2,6e-15 / 5,4e-15 | 1e-6 / 1e-9 |
| 12 sayaç (decision, flat, skip, min-emir, fallback, margin, likidasyon, funding saatleri, …) | birebir | birebir | birebir |

## 3. Katman B

| Bileşen | Sonuç |
|---|---|
| **B1:** V4 fill günlüğü ve V4 değerleme fiyatları → V5 Portfolio + FundingLedger | PASS (iki satır); farklar Katman A ile aynı büyüklükte |
| **B2:** parity-mode quote (`history.parity_quote`) ↔ V4 `HistoricalMarket.quote`, 34 Pazartesi × 20 varlık | 680/680 birebir (mid, half, ts, fallback) |
| **B3:** V5 `FundingStore.trailing` FL30 + kapsam ↔ V4 `fl(…, 30)`; `elig_h2_30`; long/short seçimi | 680/680 birebir (FL30 göreli Δ = 0); 34/34 seçim birebir |

## 4. Gerçek veride tetiklenmeyen yollar: sentetik stres çapraz kontrolü (review M1)

- **Yöntem:** `scripts/v5_parity/make_stress.py`, V4 dizin düzeninde iki sentetik kopya üretir. Bunlar fiyat şoku, likidasyon spike'ı, eksik 00:00 barı, AVAX'ta sıfır gerçek bar ve min_cost 5 içerir. V4 harness ve W-V4 aynı kopyada koşar.
- **stress_A:** margin küçültme (4 fill), 00:30 fill'i, ADA'da işaret değişimi, atlanan aksiyon (1), min-emir atlaması (29). Katman A, B1, B2 ve B3 **PASS**.
- **stress_B:** 31 likidasyon (124 fill, cezayla), margin küçültme. Katman A, B1, B2 ve B3 **PASS**.

## 5. Bağımsız review (2026-10-11) ve düzeltmeler

**Reviewer hükmü:** "PASS gerçek, döngüsel değil." Bulgular ve karşılıkları:

| ID | Bulgu | Durum |
|---|---|---|
| M1 | Margin, likidasyon, işaret değişimi ve atlanan aksiyon yolları V4'e karşı hiç koşmadı | Kapandı: §4 stres çapraz kontrolü |
| M2 | Sıra dışı fill zamanı FundingLedger'da genel bir hatayla çöküyor (V4 sessizce örtüşmeyi çift sayar) | Kapandı: açık `ParityUnsupported` + test. V4'ün çift sayımı taklit edilmez. Gerçek veride bu durum oluşmuyor; stres B'de eksik bar kapatılarak sınandı. |
| M3 | Karşılaştırıcılar NaN'ı sessizce geçiriyor | Kapandı: NaN ↔ sayı uyumsuzdur; eksik kolon FAIL (3 test, önce kırmızıydı) |
| M4 | NaN funding satırı V4 ve V5'te farklı işleniyor | Kapandı: `funding_store` NaN satırı reddeder (test). Frozen kopyada 0 NaN. |
| m1 | Gross, active, kaynak ve ceza karşılaştırılmıyordu | Kapandı: Katman A'ya eklendi, yeniden PASS |
| m2 / m3 | Değerleme fallback'i ve sıfıra yuvarlama toleransı farkı | Açık (Minor). Erişilemez veya etkisiz. Raporlandı. |
| m4 | Paper engine'in parity'yi reddettiğine dair test yok; v4ref worktree silinmedi; kopya adı `frozen_parity` | Açık (Minor). Aşağıdaki listede. |
| m5 | `history_v4` dosyaları kesim kontrolünden önce bütünüyle okunuyor | Açık (Minor). Sızan satır hata verir; ama önce belleğe alınır. |

**Testler:** `tests/carry` 166 passed (düzeltmelerden önce). `test_parity.py` 22 ve `test_frozen.py` 14 testten oluşuyor.

## 6. A-priori MDE (§5.4)

- **Kaynak:** Referansın 233 günlük (2025-11-10 → 2026-07-01) `carry_net` serisi.
- **Yöntem:** Merkezlenmiş moving block bootstrap, B = 10.000, seed 20261010.

| Blok | SE(ortalama) | MDE (%80 güç, tek taraflı %5) |
|---|---|---|
| b = 28 | 2,65e-5 / gün | **6,6e-5 / gün** (≈ 0,66 bps/gün, 265 USD'de ≈ 0,017 USD/gün) |
| b = 14 | 2,53e-5 / gün | 6,3e-5 / gün |

Bu bir **alt sınırdır**: yalnızca W tarafının dağılımına dayanır.
