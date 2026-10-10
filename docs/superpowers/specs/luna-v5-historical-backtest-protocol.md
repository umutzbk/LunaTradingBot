# V5 Adaptive Carry: Historical Backtest Protocol (draft rev 1)

**Durum:**
- Draft rev 1, 2026-10-10. **Frozen değil.**
- Bu protokol için hiçbir tarihsel getiri, P&L veya karşılaştırma hesaplanmadı.
- Kod yok. Mevcut `luna/carry/replay.py` adaptörü kullanılacak; gereken eklemeler §9'da.
- Freeze akışı: kullanıcı review → bağımsız red-team review → kullanıcı onayı → commit → annotated tag `v5-backtest-protocol-frozen`.
- **Freeze'den önce outcome koşusu yok (R1).**

**İlgili belgeler:**
- V5 motor tasarımı: `luna-v5-adaptive-carry.md`.
- V4 spec (frozen): `research-v4-spec-frozen`.
- V4 Stage 1 sonuçları: `docs/results/research-v4.md`.
- V4 veri envanteri: `docs/results/v4-data-availability.md`.
- **Bu protokol V4'ün hiçbir dosyasını, tag'ini veya sonucunu değiştirmez.**

## 0. Değişmez kurallar

| # | Kural |
|---|---|
| R1 | Freeze tag'inden önce hiçbir tarihsel V5 getirisi, P&L'i veya arm karşılaştırması hesaplanmaz. Kod yalnızca sentetik veriyle test edilir. |
| R2 | **Holdout [2026-07-01T00:00Z, collector_start) kapalı.** Bütün yükleyiciler `assert_before_cutoff(2026-07-01)` kullanır. Runner, 2026-07-01'den sonra zaman damgası taşıyan tek bir satır görse durur (test ile). |
| R3 | Strateji parametreleri V5 implementasyon default'larıdır ve **sonuca göre değişmez**: `k=2`, `gross=1.0`, `signal_hours=720`, `switch_horizon_hours=168`, `min_carry_improvement_bps=5`, `switch_cost_multiplier=1.5`, `safety_buffer_bps=5`, `min_hold_hours=24`. Paper bot da bu değerlerle kalır. |
| R4 | Parametre bütçesi kapalıdır (§6). Varyantlar yalnızca sensitivity'dir; **hiçbiri seçilemez** ve paper bot'a taşınamaz. Varyant sonucuna dayanan bir parametre değişikliği ancak ayrı ve önceden kayıtlı bir forward testle yapılabilir. |
| R5 | Random split yok. Tek, kronolojik, point-in-time replay. Karar t anında yalnızca t'de bilinen veriyi kullanır. |
| R6 | Bütün arm'lar aynı veriyi, evreni, sinyali, sizing'i, risk kurallarını, funding semantiğini ve maliyet modelini kullanır. Arm'lar arasında yalnızca **karar kuralı** (`decision_mode`) ve **karar zamanlaması** (`trigger`) farklıdır. |
| R7 | Sonuç etiketi: **"EXPLORATORY — reused development period"** (§1). Bu backtest canlıya geçiş, GO kararı veya parametre değişikliği üretmez. |
| R8 | Tek koşu. Koşudan sonra bulunan bir bug düzeltilebilir; o durumda iki sonuç da (bug'lı ve düzeltilmiş) diff ile birlikte raporlanır. Protokol, kriter ve eşikler sonuç görüldükten sonra değişmez. |
| R9 | `live_enabled=false`. Bu iş hiçbir servisi, paper DB'yi veya V4 forward kanıtını etkilemez. Koşu ayrı bir süreçte ve yalnızca `var/history_v4` ile `var/bars` üzerinde salt-okunur çalışır. |

## 1. Araştırma sorusu ve kontaminasyon beyanı

**Ana soru:** Aynı FL30 sinyali ve aynı risk ve maliyet varsayımları altında, event-driven cost-aware V5 (E), V4 weekly Monday rebalance baseline'ını (W) net olarak yeniyor mu? Yeniyorsa fark nereden geliyor: ekstra funding mi, zamanlama mı, fiyat P&L'i mi, maliyet mi?

**Kontaminasyon (açık beyan):**
- Gerçek funding yalnızca 2025-10-06 → 2026-07-01 arasında var. Bu pencere V1–V4'te kullanıldı.
- W arm'ı esasen V4 `H2-L30-k2`'dir. Onun Stage 1 sonucu biliniyor: funding +10,69, ücret 2,27, spread 1,08, fiyat +9,95 USD.
- V5 default parametreleri bu sonuç görüldükten sonra yazıldı. Parametreler bir backtest'ten seçilmedi, ama bu bilgiden habersiz de yazılmadı.
- Bu yüzden tarihsel sonuç **genelleme kanıtı değildir.** Sorunun cevabı en fazla "tarihsel veride E, W'yi yener mi / yenmez mi" düzeyindedir. Teyit edici kanıt yalnızca forward veriden gelir (§11).

## 2. Veri ve pencere

| Kalem | Kaynak | Not |
|---|---|---|
| Funding | `var/history_v4/funding` (gerçek, saatlik, `funding_abs`, `funding_rel`) | V4 S0-F semantiği (`luna/carry/funding.py`, known-only) |
| Mark | `var/history_v4/mark` 15m | Değerleme, leg stop ve risk kontrolleri |
| Perp trade barları | `var/bars/kraken_futures` 15m | Yalnızca 24 saatlik hacim (Σ volume·close, son 96 bar) |
| Perp spread | **Eksik, Stage 0'da tamamlanır (§2.1)** | Diskteki `history_v4/spreads` yalnızca her günün 23:00–00:00 saatini kapsıyor (V4 yalnızca 00:00'da karar veriyordu) |
| Enstrüman | `config/v4_instruments.json` (sha `d14a82bb…`) | Adım ve minimum emir. V4'teki bilinen anakronizmin aynısı |
| Evren | V4 §2a'nın 20 varlığı (MUBARAK hariç) | V5 paper evreniyle aynı |

**Pencere:**
- Lookback 2025-10-06 08:00'de başlar.
- Değerlendirme **[2025-11-05T00:00Z, 2026-07-01T00:00Z)**, 238 gün. V4 Stage 1 ile aynı pencere olduğu için W karşılaştırılabilir.
- Bütün arm'lar 2025-11-05'te flat başlar; ilk değerlendirmede hepsine giriş izni verilir (`Trigger`: `last is None`).
- Pencere sonunda pozisyonlar 2026-06-30 23:45 mark'ıyla değerlenir. Kapatma maliyeti ayrıca "terminal unwind cost" olarak raporlanır, net'e eklenmez.

### 2.1 Stage 0: veri tamamlama (getiri hesaplanmaz)

- `backfill-v5-spreads`: 20 varlık için Kraken analytics `spreads`, 2025-10-06 → 2026-07-01, sürekli. Satır kesimi yükleyicide değil indirme sırasında yapılır.
  - Her dosya için provenance JSON ve sha256 yazılır.
  - Çözünürlük, Stage 0'da API semantiği kontrol edilerek seçilir: değerin aralık ortalaması mı, nokta örneği mi olduğuna bakılır. Bu kontrol outcome'dan bağımsızdır. Tercih 1 dakika; rate limit izin vermezse 5 dakika.
- Kapsam ve boşluk raporu yazılır: spread, mark, bar ve funding için varlık × gün kapsamı.
- Bu adım hiçbir P&L veya sinyal sıralaması hesaplamaz.

## 3. Tarihsel snapshot yapımı (bütün arm'lar için aynı)

- **Kadans:** 15 dakika. t, 15m bar kapanış anlarıdır. Canlı bot 60 saniye kullanıyor; bu fark beyan edilir.
- **t anındaki quote (varlık a):**
  - `mid` = `open_ts = t − 15m` mark barının kapanışı. Mark yoksa trade bar kapanışı kullanılır ve sayılır. İkisi de yoksa quote geçersizdir.
  - `spread_full` = [t−1h, t) içindeki spread değerlerinin ortalaması (V4 konvansiyonu). En az 30 dakikalık veri yoksa 10 bps fallback kullanılır ve sayılır.
  - `bid/ask = mid × (1 ∓ spread_full/2)`, `markPrice = mid`.
  - `volumeQuote` = son 96 trade barının Σ volume·close değeri.
- **Derinlik:** Tarihsel order book yok. Derinlik gate'i bütün arm'larda geçmiş sayılır ve beyan edilir. Leg notional ≈ $66 olduğu için etkisi küçük.
- **Funding:** `FundingStore` geçmişin tamamını tutar. Sinyaller yalnızca `T + 1h ≤ t` dönemlerini okur. Settlement, bir dönemin rate'ini dönem bittikten sonra okur. Eksik dönem 0 nakit akışı olarak sayılır (known-only).
- **Yürütme gecikmesi:** Karar t'deki snapshot'la verilir. Fill, **bir sonraki snapshot'ın (t + 15m)** bid/ask'ında olur: alış ask × (1 + slip), satış bid × (1 − slip), taker ücreti. Bu, V4'ün "sonraki barın kapanışı" kuralının karşılığıdır. IOC limiti `mid_t × (1 ± order_band)`'dır; dışarıda kalan emir dolmaz ve sayılır.
- **Gate'ler:** V5 default'ları (spread ≤ 15 bps, hacim ≥ $1M, FL30 kapsamı ≥ %90, funding tazeliği ≤ 3 saat, quote yaşı). Tarihsel quote yaşı her zaman 0'dır.

## 4. Arm'lar

| Arm | `decision_mode` | `trigger` | Ne ölçer |
|---|---|---|---|
| **W** weekly Monday baseline | `rebalance_to_target` | `weekly`, Pazartesi 00:00 UTC | V4 H2-L30-k2 semantiği (V5 çekirdeğiyle) |
| **D** daily rebalance | `rebalance_to_target` | `daily`, 00:00 UTC | Daha sık hedef takibi, maliyet farkındalığı yok |
| **C** fixed-interval adaptive | `cost_aware` | `daily`, 00:00 UTC (24 saatlik sabit aralık) | Cost-aware kural, sabit kadans |
| **E** event-driven cost-aware | `cost_aware` | `event` (her 15m snapshot) | V5'in kendisi |
| *I* frictionless ideal (yalnızca referans) | Her snapshot'ta ideal FL30 kitabı, sıfır maliyet | — | Ulaşılabilir funding tavanı. Strateji değil, karşılaştırmaya girmez. |

**Ortak (R6):** Evren, FL30, k = 2, gross = 1,0, leg = 0,25 × equity, `carry.risk` profili (R1–R19, leg stop %15, stop cooldown 24 saat, R3 günlük kayıp %2, R4 drawdown %15), `max_net_frac` 0,10, funding semantiği, ücret %0,05 taker, §3 spread modeli, 2 bps slippage.

**Bilinen W ≠ V4 farkları (raporda tablo halinde beyan edilir):**
- Tutulan leg'ler yeniden boyutlandırılmaz (R10).
- Leg stop ve risk motoru var.
- 2 bps slippage var.
- Spread V4 gibi analytics'ten alınır, ama sürekli seriden.

C arm'ının D ile aynı kadansta olması bilinçli bir seçim: W→D→C→E zinciri her adımda tek bir şeyi değiştirir. §7'deki attribution bunun üzerine kurulu.

## 5. Metrikler (her arm, PRIMARY maliyet satırı)

| Metrik | Tanım |
|---|---|
| Net return | `E_end / E_0 − 1`; günlük `r_d = E_d / E_{d−1} − 1`. E_d, gün sonundaki son snapshot'ta değerlenir. |
| Funding P&L | Σ settle edilmiş funding nakit akışı (`cf = −q · fundingRate_T · frac`) |
| Price P&L | Net − funding + ücretler + spread/slippage (mark-to-market) |
| Fees | Σ taker ücreti |
| Spread / slippage | Σ (fill fiyatı − karar anındaki mid) × q, yön işaretli; yarım spread ve slippage ayrı ayrı gösterilir |
| Turnover | Σ |fill notional|; ayrıca ortalama equity'ye bölünmüş yıllık turnover |
| Switch sayısı | SWITCH aksiyonları; ayrıca ENTER, EXIT, forced exit, stop ve trim ayrı sayılır |
| Holding duration | Leg başına giriş → çıkış saati: medyan, ortalama, dağılım |
| Sharpe | Günlük ortalama / std × √365 |
| Max drawdown | Günlük equity üzerinden; ayrıca en kötü gün ve ES(97,5%) |
| Carry per turnover | `funding / turnover` ve `(funding − fees − spread_slip) / turnover` (bps) |
| Capture ratio | Arm'ın funding'i / I referansının funding'i |
| Exposure | Pozisyonlu gün sayısı, ortalama gross / E, net / E, residual BTC beta (diagnostic) |
| Sayaçlar | Atlanan veya dolmayan emir, spread fallback, bilinmeyen funding saati, R3/R4 olayları, leg stop'lar, kill-switch tarihi |

## 6. Parametre bütçesi (kapalı, önceden kayıtlı)

Büyük grid yok. Default'lar primary'dir. Toplam **3 varyant koşusu**; hepsi raporlanır, **hiçbiri seçilmez** (R4).

| Varyant | Arm | `switch_horizon_hours` | `min_carry_improvement_bps` | `switch_cost_multiplier` | `safety_buffer_bps` | `min_hold_hours` |
|---|---|---|---|---|---|---|
| **DEFAULT** (primary) | C, E | 168 | 5 | 1,5 | 5 | 24 |
| P-TIGHT (daha az işlem) | E | 72 | 10 | 2,0 | 10 | 48 |
| P-LOOSE (daha çok işlem) | E | 336 | 2,5 | 1,0 | 2,5 | 12 |
| P-C8 (aralık) | C | default | default | default | default | default; `trigger: interval`, 8 saat |

**Gerekçe:**
- Profiller, beş parametreyi default'un yaklaşık ×2 / ÷2 etrafında **aynı yönde** hareket ettirir. Böylece "sonuç işlem sıklığına ne kadar duyarlı?" sorusu 2 koşuyla cevaplanır.
- `m = 1,0`, maliyetin tam başa baş olduğu noktadır, bu yüzden altına inilmez.
- Değerler bu dokümanda, sonuç görülmeden sabitlenir.

**Deneme kaydı:** V5 tarihsel = 4 arm + 3 varyant = 7 koşu. Sensitivity satırları (§8) ve I referansı deneme sayılmaz.

## 7. Attribution: E − W farkı nereden geliyor?

**Teleskopik ayrışım (tam):**

```
E − W = (D − W)      frekans etkisi: hedefi haftalık yerine günlük takip
      + (C − D)      maliyet-kapısı etkisi: aynı kadansta cost-aware kural
      + (E − C)      sürekli zamanlama etkisi: aynı kural, günlük yerine her snapshot
```

Her terim dört bileşene bölünür: **Δfunding, Δprice, Δfees, Δspread_slip**. Tablo 3 × 4, toplamlar E − W'ye birebir eşit (test ile).

**Kullanıcının dört sorusuna eşleme:**

| Soru | Ölçü |
|---|---|
| Ekstra funding capture | Δfunding(E − W) toplam; ayrıca capture ratio farkı (E/I − W/I) |
| Daha iyi timing | Δfunding(D − W) + Δfunding(E − C): kadans değişince gelen funding |
| Price P&L | Δprice(E − W) ve üç terime dağılımı. Seçilen varlık farkından gelen fiyat gürültüsü; strateji iddiası olarak sayılmaz |
| Ekstra / azalan cost | Δfees + Δspread_slip; ayrıca turnover farkı |

**Switch ledger (yalnızca E ve C):** Her SWITCH ve ENTER için şunlar kaydedilir:
- karar anındaki beklenen artımlı carry (`value` ve bileşenleri);
- gerçekleşen artımlı funding: yeni leg'in funding'i eksi eski leg'in aynı süredeki funding'i, leg'in gerçek tutulma süresi boyunca;
- ödenen maliyet.

Raporlananlar:
- **isabet oranı:** gerçekleşen artımlı funding > maliyet olan switch'lerin payı;
- beklenen ile gerçekleşen arasındaki ortalama hata (bias).

Bu, cost-aware kuralın ex-ante tahmininin gerçeğe ne kadar uyduğunu doğrudan gösterir.

## 8. Maliyet ve risk sensitivity satırları (zorunlu, karar vermez)

| Satır | Değişiklik | Neden |
|---|---|---|
| **PRIMARY** | §3 | — |
| S-COST2X | 2× ücret, 2× yarım spread, 2× slippage (karar kuralı aynı default maliyetle hesaplar) | Avantaj maliyete dayanıklı mı? |
| S-SPREAD-FIX | Bütün varlıklarda sabit 10 bps full spread | Spread verisinden bağımsızlık |
| S-NOKILL | R4 drawdown kill-switch devre dışı; leg stop ve R3 aynen | Tek bir kill olayı arm karşılaştırmasını belirlemesin |

Her satır dört arm'ın hepsi için koşulur.

## 9. İstatistik ve önceden kayıtlı hüküm

**Kanıt birimi:** Günlük seriler, eşleştirilmiş fark olarak.
- `Δnet_d = r_E,d − r_W,d`.
- `Δcarry_d = carry_E,d − carry_W,d`, burada `carry_d = (funding_d − fees_d − spread_slip_d) / E_{d−1}`.

**Bootstrap:** V4 ile aynı metod: merkezlenmiş moving block, dairesel değil, B = 10.000, seed **20261010**, primary b = 28, robustness b = 14, tek taraflı %95.

**Primary test (tek):** H1: ortalama `Δcarry` > 0.
- Gerekçe: Δnet, farklı varlık seçiminden gelen fiyat gürültüsünü taşır. V4 H2'nin günlük std'si yaklaşık %2'dir; 238 günde günde birkaç bps'lik bir carry farkını Δnet ile istatistiksel olarak ayırmak beklenmez. Carry bileşeni, stratejinin kontrol ettiği kısımdır.
- Δnet her durumda nokta tahmini ve CI ile raporlanır.

**Hüküm (önceden kayıtlı, yalnızca PRIMARY + DEFAULT):**

| Hüküm | Koşul |
|---|---|
| **ADAPTIVE ADVANTAGE (EXPLORATORY)** | Δcarry LB95 > 0 hem b = 28 hem b = 14 için **ve** ortalama Δnet > 0 (nokta) **ve** S-COST2X'te ortalama Δcarry > 0 (nokta) |
| **CARRY ADVANTAGE, NET NOT SHOWN** | Δcarry şartı geçer ama ortalama Δnet ≤ 0 (fiyat P&L'i farkı yedi) |
| **NO ADVANTAGE SHOWN** | Δcarry LB95 ≤ 0 ve UB95 ≥ 0 |
| **ADAPTIVE WORSE** | Δcarry b = 28 UB95 < 0 |

- **Kırılganlık bayrağı:** S-NOKILL'de hüküm değişirse "FRAGILE (risk event driven)" eklenir.
- **İkincil karşılaştırmalar:** D−W, C−W, E−C için Δcarry ve Δnet raporlanır; p-değerleri 3 test üzerinde Holm ile düzeltilir. Yalnızca açıklayıcıdır, hüküm üretmez.
- Varyantlar (§6) yalnızca tablo halinde raporlanır; p-değeri hükme girmez.
- **Gerçekleşen MDE**, fark serisinin std'sinden hesaplanıp "post hoc" etiketiyle raporlanır. Hükmü değiştirmez.

**Hüküm ne değiştirir:**
- Hiçbir hüküm paper bot'un parametrelerini veya `live_enabled`'ı değiştirmez.
- ADVANTAGE çıkarsa → §11'deki forward karşılaştırma önerilir.
- Diğer hükümlerde paper bot paper olarak çalışmaya devam eder. "Event-driven weekly'yi yener" iddiası desteklenmemiş sayılır. Weekly moda geçip geçmeme kararı kullanıcınındır.

## 10. Uygulama (freeze sonrası, TDD, yalnızca sentetik veriyle)

| Bileşen | İş |
|---|---|
| `luna/carry/history.py` | §3 snapshot builder; `assert_before_cutoff`; quote, volume, spread fallback sayaçları |
| `luna/carry/replay.py` | Yürütme gecikmesi (fill bir sonraki snapshot'ta); I referansı; günlük ayrışım serisi; leg ve switch ledger |
| `luna/carry/backtest.py` | Arm ve varyant tablosu, sensitivity satırları, attribution, bootstrap, hüküm, rapor |
| `luna/cli.py` | `luna v5-backtest`. **Runner guard:** `v5-backtest-protocol-frozen` tag'i HEAD'in atası, çalışma ağacı temiz, protokol dosyası tag ile aynı. |

**Zorunlu testler:**
- Look-ahead: t sonrası veri bozulunca t'deki karar değişmez.
- Holdout sızıntısı: 2026-07-01 ve sonrası satır → hata.
- Teleskopik ayrışımın toplamı E − W'ye eşit.
- Sabit funding sıralamasında W, D, C ve E aynı kitabı tutar; maliyet farkı yalnızca giriş kaynaklıdır.
- W arm, elle hesaplanmış haftalık bir senaryoyu birebir üretir.
- Eksik funding saati 0 nakit akışı sayılır.
- Bootstrap V4 `stats` ile aynı sayıları üretir (sabit seed).

**Kalite kapısı:** Code review'da Critical = 0 ve Major = 0 olmadan outcome koşusu yok.

**Koşu:**
- Task Scheduler ile, tek seferlik.
- Host RAM'i dar olduğu için paper servisleriyle aynı anda bellek limiti gözetilerek çalışır.
- Çıktılar `docs/results/v5-backtest/`: `summary.json` (sha256), `manifest.json` (veri sha'ları, commit, tag), `report.md`.

## 11. Teyit yolu (öneri; ayrı karar)

Tarihsel sonuç keşif amaçlıdır. Teyit için önerilen yol forward karşılaştırmadır:
- Dört arm'ın hepsi, collector snapshot'ları (gerçek bid/ask ve derinlik) üzerinde aynı replay yoluyla offline koşulur.
- Evidence başlangıcı, bu protokolün freeze'inden ≥ 31 gün sonraki ilk 00:00 UTC olur. Bu, FL30 lookback'ini doldurmak için gerekir.
- Bakış tarihinden önce simülasyon yapılmaz (V4 R7 benzeri).
- Paper bot'un kendi defteri yalnızca implementasyon kontrolü olarak kullanılır (replay E arm'ı ≈ paper).
- Ayrıntılar ayrı bir spec'te, kullanıcı onayıyla yazılır. Bu protokolün kapsamı dışındadır.

## 12. Açık kararlar (kullanıcı)

1. Stage 0 spread backfill'i (§2.1) onaylanıyor mu? Alternatif: yalnızca sabit spread. Bu durumda S-SPREAD-FIX primary olur ve gate'ler 10 bps ile çalışır.
2. Parametre bütçesi (§6): 3 varyant uygun mu?
3. Primary test olarak `Δcarry` + Δnet nokta şartı (§9) uygun mu? Alternatif: Δnet'in LB > 0 olması. Bu alternatif, düşük güç nedeniyle büyük olasılıkla NO ADVANTAGE SHOWN verir.
4. Yürütme gecikmesi bir snapshot (15 dakika). V4 ile tutarlı; canlıdan daha muhafazakâr.
