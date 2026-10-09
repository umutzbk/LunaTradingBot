# Luna Research V3: Yeni Alpha Kaynakları · Tasarım Spec'i

- **Tarih:** 2026-10-09
- **Durum:** FROZEN (2026-10-09 04:18 UK; kullanıcının overnight talimatıyla otomatik freeze; tag `research-v3-spec-frozen`). Freeze öncesi bağımsız review bulguları (null uygunluk kümesi, spot fail-closed, kriter 8 için Sharpe) hiçbir V3 sonucu görülmeden işlendi.
  Bu spec yalnızca açık bir spec-change kaydıyla değiştirilebilir. V3 sonuçları görüldükten sonra kriter, grid,
  parametre ya da aile değiştirilmez.
- **Ön koşul:** Research V2 CLOSED — NO CANDIDATE (perp ve spot), `feat/luna-core`'a local merge.
- **Girdiler:** `docs/results/v1-v2-failure-analysis.md`, `docs/results/research-v2.md`, V2 spec + SC-1 + SC-2.
- **Yetki:** Kullanıcı 2026-10-09 overnight talimatında V3 tasarımı ve implementasyonu için açık onay verdi
  ("bu mesaj V3 design + implementation için açık onayımdır").

---

## 0. Neden V3?

V1 ve V2, fiyat tabanlı kural sinyallerinde maliyet sonrası tekrar eden bir edge göstermedi
(failure analysis §3). V3'ün araştırma sorusu:

> Fiyat trendinden **farklı ekonomik mekanizmalara** dayanan, düşük turnover'lı yeni sinyal aileleri, aynı maliyet
> ve risk disiplini altında, development walk-forward OOS'ta kendi null baseline'larını geçen bir edge gösteriyor mu?

V3 momentum parametrelerini yeniden optimize etmez. Momentum'un rejim-koşullu varyasyonları kapsam dışıdır.

## 1. Sabit kararlar

| # | Karar |
|---|---|
| E1 | **Veri kesimi:** `V3_DATA_END = V2_DATA_END = 2026-07-01T00:00:00Z`. V3 yükleyicileri V2'nin `load_v2_inputs` / `assert_before_cutoff` korumasını kullanır. Final generalization seti (2026-07 → 2026-10) hiçbir koşulda okunmaz; `luna holdout` V3'ten çağrılmaz. |
| E2 | **Dataset dürüstlüğü:** V3, V1/V2'de defalarca incelenmiş development setini (OOS 2024-09 → 2026-07) yeniden kullanır. V3 sonuçları **keşif** sonuçlarıdır; final generalization kanıtı olarak sunulmaz. Her V3 raporu bunu ilk paragrafta söyler. |
| E3 | **Kabul ve GO/NO-GO kuralları gevşetilmez:** ranker kabul kuralı (n ≥ 30, shrinkage, bootstrap alt sınırı > 0) ve V2 §3.5 kriter 1–7 aynen. Kriter 8, V3'te **aile null baseline'ını günlük Sharpe'ta geçmek** olarak tanımlanır (§4). V1 naive momentum karşılaştırması bilgi amaçlı raporlanır. |
| E4 | **Motor yeniden kullanımı:** Tüm V3 aileleri V2'nin cross-sectional simülatörünü (`run_xsec`), portföy risk motorunu (V2 §4.2 sabitleri, SC-1 emir limitleri) ve SC-2 ücretlerini **değiştirmeden** kullanır. Tek ekleme, `run_xsec`'e bir skor kancasıdır: varsayılan davranış V2 ile birebir aynıdır (golden test). |
| E5 | **Düşük turnover:** Yalnızca haftalık rebalance. Günlük sensitivity yok (deneme sayısını ve maliyeti büyütmemek için). |
| E6 | **Perp ve spot ayrı karar alır**, sonuçlar toplanmaz (V2 D4). |
| E7 | **Canlı emir yok, gerçek para yok.** Yalnızca research/paper. |
| E8 | V1/V2 sınıfları, parametreleri ve davranışı değişmez. V3 yeni modüllerde yaşar (`luna/v3/`). |

## 2. Aile değerlendirmesi (kullanıcının 8 adayı)

| # | Aile | Veri | Karar | Gerekçe |
|---|---|---|---|---|
| 1 | Funding-rate / funding dispersion | Kraken funding yalnızca 2025-10-06'dan beri (development'ta ~9 ay) | **Ertelendi** | 6 ay train / 2 ay test ile en fazla 1 OOS penceresi. Kanıt üretemez. Funding forward toplanmaya devam eder; V4 adayı. |
| 2 | Perp–spot basis / cash-and-carry | Perp'te carry geliri = funding (aynı kısıt); iki hesaplı (spot+perp) eşzamanlı yürütme gerekir | **Ertelendi** | Carry getirisi funding geçmişi olmadan ölçülemez. UK perakende perp kısıtı. Bilgi değeri §3'teki prim sinyaliyle kısmen alınır. |
| 3 | Cross-sectional funding relative value | Funding kısa; **perp–spot primi** 15m barlardan 2023'ten beri hesaplanabilir | **Seçildi (A), prim proxy'si ile** | Prim, kaldıraçlı long talebini (funding baskısını) anlık olarak yansıtır. Uzun geçmişle test edilebilir. |
| 4 | Volatility / realized-vol anomalileri | OHLCV yeterli | **Seçildi (B: low-vol cross-section; C: vol-managed exposure)** | Hisse ve kripto literatüründe belgelenmiş, momentum'dan farklı mekanizma. |
| 5 | Likidite / spread / maker edge | Tarihsel spread ve kuyruk verisi yok | **Reddedildi** | Maker edge'i kuyruk pozisyonu modeli olmadan güvenilir simüle edilemez. |
| 6 | Order-book imbalance / mikro-yapı | L2 geçmişi yok | **Reddedildi** | Veri yok; ayrıca 15m maliyet tabanı kısa vadeli sinyalleri V1'de zaten eledi. |
| 7 | Rejim-koşullu momentum | — | **Reddedildi** | Momentum'un yeniden optimizasyonu riski; V1 rejim filtresi zaten ablate edildi. |
| 8 | Yapısal / event anomalileri (ör. hafta sonu, funding saati) | Bar verisi var; event takvimi (unlock vb.) yok | **Ertelendi** | Etki büyüklüğü literatürde birkaç bps; haftalık maliyet tabanını geçmesi düşük olasılık. Event verisi yok. |

Seçilen aile sayısı: **3** (A, B, C).

## 3. Hipotezler ve ekonomik gerekçe

### A. Perp primi (crowding) cross-sectional relative value
- **Sinyal:** Her varlık için günlük prim `p_d = close_perp(d) / close_spot(d) − 1` (her ikisi de aynı karar damgasındaki
  causal günlük kapanış, yalnızca gerçek işlem içeren günler). Skor: son L günün ortalama primi.
  **Düşük prim → long, yüksek prim → short.**
- **Ekonomik gerekçe:** Yüksek prim, kaldıraçlı long kalabalığını ve yüksek funding baskısını gösterir. Kalabalık
  pozisyonlar likidasyon ve funding maliyetiyle çözülür; prim yüksek varlıklar sonraki dönemde göreli olarak zayıf
  performans gösterir (funding/basis'in getiriyi negatif öngördüğüne dair kripto literatürü).
- **Hesaplar:** Perp market-neutral (long low-prim k / short high-prim k). Spot long-only (en düşük primli top-k).
  Spot için perp verisi yalnızca sinyal olarak okunur.
- **Grid:** V2 cross-sectional grid'i aynen: L ∈ {14, 30, 90} gün, k ∈ {1, 2}; perp için risk grid'i gross
  {1.0, 1.5} × vol hedefi {%10, %15}. Evren: BTC/ETH ve geniş.

### B. Low-volatility cross-section
- **Sinyal:** Son L günlük günlük log-getirilerin standart sapması. **Düşük vol → long, yüksek vol → short.**
- **Ekonomik gerekçe:** Kaldıraç kısıtlı ve piyango arayan yatırımcılar yüksek volatiliteli varlıklara fazla öder
  (low-volatility / betting-against-beta anomalisi). Kriptoda perakende ağırlığı bu etkiyi güçlendirebilir.
- **Hesaplar:** Perp market-neutral (dolar nötr; V2 net ≤ 0.10x). Beta nötr değildir; `corr_btc` raporlanır.
  Spot long-only (en düşük vol top-k).
- **Grid:** A ile aynı (L, k, perp risk grid'i).

### C. Vol-managed exposure (spot)
- **Sinyal / ağırlık:** Evrendeki uygun varlıkların tamamı eşit ağırlıkla long (k = n); portföy ölçeği V2'nin causal
  EWMA vol hedeflemesi ile `hedef / tahmini vol` (tavan 1.0).
- **Ekonomik gerekçe:** Volatilite kısa vadede kalıcıdır ama beklenen getiriyle orantılı artmaz; yüksek vol dönemlerinde
  pozisyonu küçültmek risk-ayarlı getiriyi artırır (volatility-managed portfolios).
- **Hesap:** Yalnızca spot (long-only, 1x).
- **Grid:** vol hedefi ∈ {%10, %15}. Evren: BTC/ETH ve geniş.
- **Fail-closed:** Kovaryans, tutulan bir varlık için ölçülemiyorsa (eksik ya da NaN), o rebalance'ta pozisyon açılmaz
  (`cov_unavailable`; perp kitabıyla aynı davranış). Vol ölçeği hiçbir zaman 1.0'ın üstüne çıkmaz.

## 4. Null baseline'lar (kriter 8)

| Aile | Null | Tanım |
|---|---|---|
| A, B | **Shuffled-score** | Aynı motor, aynı seçilmiş parametreler ve evren. Her rebalance'ta skor yerine (tohum, karar damgası, varlık adı) ile tohumlanmış standart normal çekilişler kullanılır. Çekilişler yalnızca gerçek sinyalin skorunun tanımlı olduğu varlıklara verilir (`shuffle<seed>:<signal>`), yani null adayla aynı uygun varlık kümesinde işlem yapar. Tohumlar 1–5; işlem yapan tohumların ortalaması alınır ve işlem yapan tohum sayısı raporlanır. |
| C | **Sabit ağırlık** | Aynı evren, eşit ağırlık long, vol hedeflemesi yok (haftalık rebalance ile al-tut). |

**Kriter 8:** Adayın OOS günlük Sharpe'ı (edge varyantı), aynı hesap ve aynı OOS aralığında null baseline'ın
ortalama günlük Sharpe'ından kesin olarak büyük olmalı. Null hiç işlem yapmadıysa aday Sharpe'ı > 0 olmalı.
**Neden Sharpe:** Planlanan risk, equity'nin %1.5'i artı maliyet olarak sabit tanımlı. Bu yüzden vol hedeflemesiyle
küçültülen bir pozisyon, aynı fiyat hareketinde orantılı olarak küçük R üretir. R karşılaştırması C ailesini yapısal
olarak cezalandırırdı. Sharpe ölçekten bağımsızdır. Bu karar V3 sonuçları görülmeden, freeze öncesi review bulgusu
üzerine verildi. R bazlı karşılaştırma bilgi amaçlı raporlanır. V1 naive momentum (spot'ta long-only versiyonu) da
yalnızca bilgi amaçlı raporlanır; GO kararını etkilemez.

## 5. Doğrulama disiplini
- **Walk-forward:** V2 ile aynı pencereler (6 ay train / 2 ay test, OOS 2024-09 → 2026-07), pencereler arası state taşıma.
- **Seçim:** Konfigürasyon ve parametre seçimi yalnızca train'de (V2 cross-sectional train hedefi, V1 worst-case
  bilinmeyen funding kuralı dahil). Aday, V2'deki gibi iç içe seçimle tek bir walk-forward olarak raporlanır.
- **Look-ahead:** Skorlar yalnızca karar damgası `t`'ye kadar olan günlük kapanışları okur. Test: `t` sonrasındaki
  barları bozmak skoru değiştirmez.
- **Holdout izolasyonu:** Spot sinyal paneli dahil tüm girdiler `assert_before_cutoff`'tan geçer. Test: kesim sonrası
  bar → `HoldoutLeak`.
- **Robustness (diagnostic, V2 §5 aynen):** ex-top1/top3, trimmed, medyan, yoğunlaşma, asset LOO, time LOO,
  best-month/quarter. GO/NO-GO'yu değiştirmez.

## 6. Çoklu test dürüstlüğü (önceden hesaplanmış deneme sayısı)

| Aile | Hesap | Konfigürasyon | Grid noktası |
|---|---|---|---|
| A | perp | 2 (btc_eth, geniş) | 2 × 24 = 48 |
| A | spot | 2 | 2 × 6 = 12 |
| B | perp | 2 | 48 |
| B | spot | 2 | 12 |
| C | spot | 2 | 2 × 2 = 4 |
| **Toplam** | | **10** (perp 4, spot 6) | **124** |

V1 (208 kombinasyon) ve V2 (perp 184 + spot 88 grid noktası) deneme sayıları raporda kümülatif olarak yanında verilir.

## 7. Maliyet ve emir modeli
V2 ile aynı: taker birincil; SC-2 spot ücretleri (0.40/0.80); perp 0.02/0.05; 2× maliyet sensitivity'si; perp funding
known_only (bilinmeyen funding uydurulmaz).

## 8. Uygulama planı
`docs/superpowers/plans/luna-research-v3-plan.md`. Her task TDD ile; sonda bağımsız review.

## 9. Çıktılar
- Hesap başına V3 raporu (HTML + `summary.json`), aile bazında sonuç, null karşılaştırması, kümülatif deneme sayısı,
  dataset-reuse uyarısı.
- `docs/results/research-v3.md`.

## 10. Riskler
- **Az işlem:** Haftalık rebalance ve 6 aylık train n ≥ 30'u zorlayabilir (V2'deki gibi). Dürüst sonuç INSUFFICIENT DATA olabilir.
- **Prim ölçümü:** Perp ve spot kapanışları farklı venue'larda; günlük kapanış zaman damgaları aynı (23:45 UTC barı).
  İşlemsiz günler NaN (V2 kuralı).
- **Beta:** B perp'te dolar-nötr ama beta-nötr değil; `corr_btc` raporlanır, kararı etkilemez.
- **Overnight zaman kısıtı:** Implementasyon ve koşu 2026-10-09 10:00 UK'ye sığmayabilir. Sığmazsa güvenli checkpoint
  bırakılır; bu spec değişmez.
