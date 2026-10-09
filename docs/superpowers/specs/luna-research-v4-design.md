# Luna Research V4: Funding / Basis / Structural Carry (spec rev 2)

**Durum:**
- rev 2, 2026-10-09. **Frozen değil.** Kod yok. Stage 1 getiri koşusu **yapılmadı**.
- Kullanıcının 15 maddelik pre-freeze kararlarını uygular. Independent red-team review'un 25 bulgusu (F1–F25) bu metne işlendi; özet `docs/results/v4-prefreeze-review.md` dosyasında.
- Freeze akışı: kullanıcı onayı → commit → annotated tag `research-v4-spec-frozen`.
- **Kural freeze'i ile evidence başlangıcı ayrıdır (§7.5):** `V4_EVIDENCE_START_TS` freeze'de önceden kaydedilen bir tarihtir, tag zamanı değildir.

**İlgili belgeler:**
- Veri envanteri: `docs/results/v4-data-availability.md`.
- Collector: `luna-v4-forward-collector-design.md` (rev 2).
- Önceki sonuçlar: `research-v2-closure.md`, `research-v3.md`.

## 0. Değişmez kurallar

| # | Kural |
|---|---|
| R1 | **Holdout = [2026-07-01T00:00Z, `collector_start_ts`) — sealed.** V4 bu aralığı sinyal, lookback, eşik, medyan, evren veya config seçimi için okumaz. Tarihsel yükleyiciler `assert_before_cutoff(2026-07-01)` kullanır. Forward tarafı yalnızca `recv_ts ≥ collector_start_ts` collector verisini ve yalnızca dönem başlangıcı `T ≥ collector_start_ts` olan funding satırlarını okur; `T < collector_start_ts` satırları atılır (test ile). **Kullanıcı kararı (2026-10-09): holdout tamamen kapalı.** Okunmaz, kill-test yapılmaz, tuning için kullanılmaz. Değişiklik ancak kullanıcının ayrı ve açık onayıyla olur. |
| R2 | Yeni fiyat-sinyali optimizasyonu yok. V4'te sinyal, carry nakit akışı (funding) ve basis'tir. |
| R3 | **`premium_proxy` asla nakit akışı üretmez.** Yalnızca rank/sign persistence, rejim, sinyal sayısı ve veri kalitesi için kullanılır. Proxy'den funding seviyesine kalibrasyon yasak. **Eksik funding saati** asla proxy, ortalama veya tahmin (`fundingRatePrediction`) ile doldurulmaz (§2). Net carry getirisi yalnızca gerçek settle edilmiş funding'in olduğu dönemde (2025-10-06 → 2026-07-01) ve forward veride hesaplanır. |
| R4 | Canlı emir yok, gerçek para yok. Rapor iki ayrı sonuç verir: **RESEARCH EDGE** (GO / NO-GO / INSUFFICIENT DATA) ve **DEPLOYABLE NOW** (YES / NO). Bkz. §9. |
| R5 | §6'daki kabul kriterleri frozen'dır. Sonuç görüldükten sonra gevşetilmez. Kanıt yetersizse sonuç INSUFFICIENT DATA olur. |
| R6 | **Deneme sayısı kapalı: 7 executable config** (§4). Bütçe yok, yedek yok. Yeni config veya hipotez = V5 ya da spec değişikliği. Sensitivity satırları (§5.4) ve null'lar config değildir, hepsi zorunlu raporlanır. **Null'lar hiçbir zaman GO alamaz ve stratejiye terfi ettirilemez.** |
| R7 | GO kanıtı yalnızca forward evidence'tan gelir: `recv_ts ≥ V4_EVIDENCE_START_TS` (§7.5). Bu tarihten önceki collector verisi warmup'tır: veri kalitesi ve lookback girdisi olarak kullanılabilir, kanıt olarak kullanılamaz. |
| R8 | Kümülatif deneme kaydı: V1 208; V2 perp 184 + spot 88; V3 10 config / 124 grid; **V4 7 config**. |
| R9 | **Freeze tag'inden önce hiçbir tarihsel veya forward getiri/P&L hesaplanmaz.** V4 kodu o zamana kadar yalnızca sentetik veriyle test edilir. |

## 1. Gerekçe

V1–V3'ün bulgusu: fiyat sinyallerinde, maliyetler düşüldükten sonra ayırt edilebilir bir alpha yok.

Funding farklı bir getiri kaynağı:
- Kaldıraçlı long talebinin ödediği yapısal bir primdir ve yön tahmini gerektirmez.
- Hedge'li pozisyonla toplanabilir.
- Kripto literatüründe funding ve basis, kalıcı ama zamanla değişen bir carry primidir.

Riskler: funding'in tersine dönmesi, spot ücreti (taker %0,80), spread, basis şoku, hedge hatası, margin.

## 2. Funding semantiği (S0-F: DOĞRULANDI, 2026-10-09)

**Kaynaklar:**
- Kraken "Linear Multi-Collateral Derivatives Contract Specifications" (PF_ kontratlar).
- Kraken Futures API referansı "Historical funding rates".
- Development penceresinde (< 2026-07-01) 21 varlık üzerinde sonuçtan bağımsız ampirik kontrol.
- Ayrıntı: `docs/results/v4-funding-semantics.md`. Kilitleyen testler: `tests/v4/test_funding.py`.
- **Kullanıcı kararı (2026-10-09):** S0-F PASS kabul edildi.

**Kurallar:**
- **Dönem:** 1 saat. API `timestamp` = rate'in uygulandığı dönemin **başlangıcı** T. Rate, [T−1h, T) primine göre hesaplanır.
- **İşaret:** Rate > 0 ise long öder, short alır; rate < 0 ise short öder, long alır.
- **Birim:**
  - `fundingRate` (abs) = dönemin tamamı boyunca tutulan 1 birimlik short'un aldığı USD.
  - `relativeFundingRate` (rel) = abs / referans fiyat; saatlik ondalık kesir, yüzde değil.
  - Yıllıklandırma ×8760.
- **Nakit akışı:** Varlık birimi cinsinden q (long > 0) için, [T, T+1h) dönemi içinde tutulan süre oranıyla:
  `cf = −q · fundingRate_T · (tutulan süre / 1h)`
  - Settle zamanı: dönem sonu ya da pozisyon değişikliği, hangisi önce gelirse.
  - Funding, perp collateral nakdine settle anında yazılır; E'de ayrı bir terim değildir.
  - Mark fiyatı gerekmez, çünkü abs rate doğrudan USD/birimdir.
- **Bilinen rate:** t anındaki karar yalnızca `T + 1h ≤ t` olan, yani bitmiş dönemlerin rate'lerini kullanır (`known_rates`).
- **Eksik dönem:** Nakit akışı 0 (known_only); `funding_unknown_hours` sayılır. Tutulan saatlerin > %1'i bilinmiyorsa raporda işaretlenir.
- Varlık bazında semantik tutarlılık, §2a'daki genel uygunluk kurallarının (U3, U4) parçasıdır.

## 2a. V4 araştırma evreni: genel uygunluk kuralları (frozen)

**Kapsam:**
- Aday küme: V2/V3 ile aynı 21 varlık.
- Aynı kurallar her varlığa uygulanır. Varlık adı ya da geçmiş strateji performansı kurallara girmez.
- Kurallar yalnızca development penceresi [2025-10-06T08:00Z, 2026-07-01T00:00Z) verisiyle ve getiri hesaplanmadan değerlendirilir.
- Sonuç freeze commit'inde `docs/results/v4-universe.md` olarak sabitlenir.
- §2a evreni, development penceresinin tamamından bir kez hesaplanan statik bir veri-bütünlüğü filtresidir ve Stage 1'e de uygulanır. Bu, metadata snapshot'ı gibi bilinen ve raporlanan bir anakronizmdir. §8'deki "bugünün evren listesi geçmişe uygulanmaz" kuralı, §3'teki dinamik uygunluk (hacim, listeleme yaşı, funding kapsamı) için geçerlidir.
- **Şeffaflık notu:** U3/U4 eşikleri, S0-F kontrolü bir varlığın başarısız olduğunu gösterdikten sonra yazıldı. Kurallar geneldir ve sonuçtan bağımsızdır, ama bu bilgiden habersiz yazılmadı.
- Bir kuralı geçemeyen varlık V4 araştırma evreninin dışında kalır. Bu, paper bot evrenini **değiştirmez**; ikisi ayrı kavramlardır.

| # | Kural | Eşik ve gerekçe |
|---|---|---|
| U1 | Zorunlu funding alanları | Her satırda `timestamp`, `fundingRate`, `relativeFundingRate` mevcut ve sonlu sayı (%100). Muhasebe abs rate'i, sinyal rel rate'i kullanır. |
| U2 | Tekrar bütünlüğü | Aynı dönem başlangıcı için birden fazla satır varsa değerler aynı olmalı. Çelişkili tekrar sayısı 0. |
| U3 | İşaret ve birim geçerliliği | `sign(abs) = sign(rel)` satırların ≥ %99,9'unda. `median |abs/rel − P_perp(T)| / P_perp(T) ≤ %0,5`, burada P_perp(T) = `open_ts = T − 15m` barının kapanışı. Birim (rel = abs/fiyat) varlıkta gerçekten geçerli olmalı; aksi halde funding cash flow'u ile sinyal tutarsızlaşır. |
| U4 | Zaman damgası semantiği | 1h mark/index priminden: `corr(rel_T, prim[T−1h, T)) > 0` **ve** `> corr(rel_T, prim[T, T+1h))`. Kraken'in "rate, önceki saatten hesaplanır" kuralı o varlıkta gözlenmeli. |
| U5 | Piyasa erişimi | PF_ perp listeli ve Kraken spot USD çifti mevcut (H1/H5 iki bacak gerektirir). |
| U6 | Asgari kapsam | Funding: beklenen saatlik dönemlerin ≥ %99'u. Perp 15m bar: beklenen slotların ≥ %95'i. Spot 15m bar: ≥ %90. |
| U7 | Azami boşluk | Funding ≤ 6 saat (API'nin tüm varlıklarda ortak 2–3 saatlik boşluklarına tolerans). Perp ve spot bar ≤ 24 saat. |

## 3. Hipotezler

**Ortak kurallar (H1, H5, H2):**
- **Bar etiketi:** Barlar `open_ts` ile etiketlenir; `close_ts = open_ts + 15m`.
- **Sinyal:** t anındaki sinyal yalnızca `open_ts ≤ t − 15m` barları ve bitmiş funding dönemlerini (`T + 1h ≤ t`) kullanır.
- **Tarihsel yürütme:** `open_ts = t` olan barın kapanışında. Bu bar yoksa 1 saat içindeki ilk bar kullanılır; o da yoksa işlem o gün atlanır ve sayılır.
- **Forward yürütme:** t'den sonraki ilk skew-geçerli `pair_seq` snapshot'ının mid'i (§7.2). 15 dakika içinde geçerli snapshot yoksa o günün kararları atlanır ve sayılır.
- **Eşitlik:** alfabetik sıra.
- **Çok bacaklı işlemler hep-ya-hiç kuralına tabidir.** Bu, H2 rebalance'ı ve her H1/H5 giriş/çıkışı için geçerlidir. Herhangi bir bacak atlanırsa (eksik fiyat, minimum emrin altı), bütün işlem atlanır, önceki portföy korunur ve sayılır. Atlanan H2 Pazartesi'si bir sonraki Pazartesi'ye kadar yeniden denenmez.
- **Uygunluk, yalnızca girişte kontrol edilir.** Pozisyon açıkken uygunluk kaybolursa pozisyon kapanmaz.
  - Delisting veya `suspended` olursa pozisyon son geçerli fiyattan kapatılır, ücret + spread + %0,5 ceza uygulanır.
- **Günlük hacim:**
  - Tarihsel: UTC günü içindeki 15m barların Σ(volume·close) değeri.
  - Forward: 00:00 sonrası ilk geçerli snapshot'ta spot için `v[1]·p[1]`, perp için `volumeQuote`.
  - 30 günlük medyan için 30 günlük değerin ≥ 27'si gerekir.
- **Listeleme yaşı:** Tarihsel için ilk perp bar'ı; forward için `fut_instruments` kaydındaki `openingDate`, yoksa collector'da ilk görülme.
- **Minimum emir:** Miktarlar freeze anında kaydedilen enstrüman metadata'sına göre yuvarlanır: `qty_step`, `min_qty` / spot için `ordermin` ve `costmin`. Minimumun altındaki işlem ve ayarlamalar atlanır, sayılır; oluşan hedge hatası raporlanır.

### H1: Spot/perp cash-and-carry (delta-nötr, yalnızca pozitif funding)

- **Gerekçe:** Spot long + perp short pozitif funding'i toplar. Spot short açılamadığı için ters carry kapsam dışıdır.
- **Sinyal:** `F7_i(t)` = bitmiş son 168 dönemin rel ortalaması × 8760. Yalnızca gerçek funding kullanılır.
- **Uygunluk:**
  - Perp ve spot listeli.
  - 30 günlük medyan günlük spot hacmi ≥ $1M ve perp hacmi ≥ $1M.
  - Son 168 saatin ≥ 160'ında funding kaydı var.
- **Kurallar:**
  - Karar her gün 00:00 UTC'de verilir. Önce çıkışlar işlenir, sonra girişler.
  - Çıkış: `F7 ≤ 0` ya da tutma süresi 90 gün.
  - Giriş: `F7 ≥ Y`. Uygun adaylar F7'ye göre azalan sıralanır; boş slot kadar alınır, en fazla 3 eşzamanlı pozisyon.
  - Aynı varlığa yeniden giriş, `exit_day + 7` karar gününden itibaren mümkün.
- **Configler:** `H1-Y10` (Y = %10), `H1-Y20` (Y = %20).

### H2: Perp-only funding carry (cross-sectional, dollar-neutral)

- **Sinyal:** `FL_i(t)` = son L×24 ödeme saatinin r ortalaması, L ∈ {7, 30}.
- **Uygunluk:**
  - Perp ≥ 30 gün listeli.
  - 30 günlük medyan günlük perp hacmi ≥ $1M.
  - Son L×24 saatin ≥ %90'ında funding kaydı var.
- **Kurallar:**
  - Rebalance her Pazartesi 00:00 UTC.
  - En düşük FL'li k varlık long, en yüksek FL'li k varlık short.
  - Her long ve her short notional'ı `0,5/k × E`. Gross 1,0 × E, net 0.
  - Ara günlerde ağırlık kayması serbest. Beta-hedge yok.
  - Uygun varlık sayısı 2k'dan azsa o hafta flat kalınır ve bu sayılır.
- **Configler:** `H2-L7-k1`, `H2-L7-k2`, `H2-L30-k1`, `H2-L30-k2`.

### H5: Funding × basis (canonical kural önceden kayıtlı)

- **Venue basis:** `B_i = (P_perp − P_spot) / P_spot`.
  - Tarihsel: aynı `open_ts`'li 15m bar kapanışlarından. Bacaklardan biri `filled=True` ise o bar dışlanır.
  - Forward: skew-geçerli `pair_seq` mid'lerinden.
- **`B24_i(t)`:** son 24 saatin medyanı. Tarihsel için ≥ 48 geçerli bar, forward için ≥ 720 geçerli snapshot gerekir.
- **`B30med_i(t)`:** son 30 günün her biri için 00:00 karar anındaki B24 değerlerinin medyanı. ≥ 27 gün gerekir.
- **Kural `H5-HFWB`:** H1 mekaniği. Giriş koşulu: `F7 ≥ %10` **VE** `B24 ≥ B30med`.
- **Diğer üç 2×2 hücresi:** yalnızca diagnostic (D-H5CELLS). Seçilemez.

### Diagnostic'ler (config değil, GO/NO-GO'ya girmez, deneme sayılmaz)

- **D-PERSIST (H1/H2 ön koşulu):**
  - Ölçülen: haftalık örtüşmeyen pencerelerde F7(t) ile sonraki 7 günün ortalama funding'i arasındaki varlıklar-arası Spearman korelasyonu, ayrıca yarılanma süresi.
  - Gerçek-funding penceresinde ölçülür. Proxy döneminde yalnızca proxy-rank versiyonu ölçülür.
  - Persistence ≤ 0 ise uyarı bayrağı yazılır. Config setini değiştirmez.
- **D-DISP (eski H3):** H2 günlük getirisi, varlıklar-arası funding std'sinin (trailing 30 gün) genişleyen medyanının üstünde ve altında ayrı ayrı raporlanır. Bundan strateji türetmek yasaktır (V5).
- **D-H5CELLS:** 2×2 tablonun dört hücresi.
- **Kaldırılan:** Eski H4(b) (aşırı funding sonrası fiyat getirisi) V4'te yok, raporlanmaz.

## 4. Executable configler (kapalı, toplam 7) ve null'lar

| # | Config | Hesap | Null'lar |
|---|---|---|---|
| 1 | H1-Y10 | Carry (§5.1) | N-RANDENTRY ×5, N-ALWAYS |
| 2 | H1-Y20 | Carry | N-RANDENTRY ×5, N-ALWAYS |
| 3 | H2-L7-k1 | H2 (§5.2) | N-SHUFFLE ×5 |
| 4 | H2-L7-k2 | H2 | N-SHUFFLE ×5 |
| 5 | H2-L30-k1 | H2 | N-SHUFFLE ×5 |
| 6 | H2-L30-k2 | H2 | N-SHUFFLE ×5 |
| 7 | H5-HFWB | Carry | N-RANDENTRY ×5, N-ALWAYS; H1-Y10 karşılaştırması (bilgi) |

**N-RANDENTRY:**
- Config'in gerçekleşen her girişi için tutma süresi korunur. Giriş günü ve varlık, o günün uygun kümesinden düzgün dağılımla çekilir: `rng = numpy.default_rng([seed, crc32(config_id)])`, seed 1–5.
- En fazla 3 eşzamanlı pozisyon ve 7 günlük cooldown korunur. Kısıta uymayan çekim 100 kez yeniden denenir; yine olmazsa atılır ve sayılır.
- Config'in hiç girişi yoksa C4 = FAIL.

**N-ALWAYS:**
- Yalnızca §5.1 muhasebesi kullanılır. F7 çıkışı, 90 gün sınırı veya cooldown **yoktur**.
- D0'da (Stage 1'de değerlendirme başında) ve her ayın 1'inde 00:00 UTC'de uygun kümeden alfabetik ilk 3 varlık hedeflenir. Yalnızca değişen varlıklar işlem görür.

**N-SHUFFLE:**
- Her rebalance anında FL sıralaması `crc32(f"{seed}:{config_id}:{t}")` ile karıştırılır, seed 1–5.

## 5. Muhasebe (frozen)

### 5.1 Carry hesabı (H1, H5, N-RANDENTRY, N-ALWAYS)

| Kalem | Değer |
|---|---|
| Başlangıç equity E0 | **$530**: spot sleeve $265 + perp sleeve $265. Sanal; paper bot'lardan ayrı. |
| Slot notional | `N = 0,30 × E_spot` (girişteki değer). En fazla 3 slot, spot sleeve'in ≤ %90'ı. |
| Hedge | USD-eşit notional: `q_s = N / P_spot`, `q_p = −N / P_perp` (giriş fiyatlarıyla). |
| Perp collateral | Perp sleeve nakdi. Funding buraya yazılır. Toplam short notional ≤ ~$239, perp leverage ≤ ~0,9×. |
| Margin buffer | Günlük 00:00: `perp_equity / perp_notional < 0,50` ise iki bacak orantılı küçültülerek oran 1,00'e döndürülür. |
| Likidasyon | Gün içinde `perp_equity ≤ 0,05 × perp_notional` olursa pozisyon kapanır. Tarihsel: short perp bacakları için 15m mark high, long perp bacakları için mark low ile kontrol; o barın kötü fiyatından kapanış. Forward: short için snapshot'ların maks, long için min `markPrice`'ı. İki bacağa ücret + spread + %0,5 ceza. |
| Hedge düzeltmesi | Günlük 00:00: `|spot_notional − perp_notional| / spot_notional > 0,05` ise perp notional düzeltilir. Minimum emrin altındaysa atlanır ve sayılır. |
| Sleeve transferi | Yok. |
| Equity | E = spot nakit + `q_s · P_spot` + perp nakit (collateral + gerçekleşmiş P&L + funding) + perp unrealized. |
| Getiri | `r_d = E_d / E_{d−1} − 1`. E_d, `open_ts = d 23:45` barının kapanışında (forward'da 00:00 sonrası ilk geçerli snapshot'ta) değerlenir. Flat günler 0 getiri ile seriye dahildir. |
| Gross / net | `gross = spot_notional + |perp_notional|`, `net = spot_notional − |perp_notional|`. Günlük raporlanır. |
| Mark fiyatı | Funding nakit akışında **kullanılmaz** (§2: `cf = −q·fundingRate_T·frac`). Yalnızca margin oranı ve likidasyon için kullanılır: tarihsel `open_ts = t − 15m` mark barının kapanışı (yoksa trade kapanışı, sayılır); forward t anında veya öncesindeki son snapshot'ın `markPrice`'ı. |
| Ücret tabanı | `fee = oran × |q| × mid`, `spread_cost = |q| × half_spread × mid`. T4 bu tanımı kullanır. |

**Günlük P&L ayrışımı:**
`spot_price_pnl + perp_price_pnl + funding_cf − fees − spread_slippage − liquidation_penalty`

Ek olarak, slot başına fiyat P&L'i iki parçaya ayrılır:
- `basis_contribution = −min(q_s, |q_p|) · Δ(P_perp − P_spot)`
- `residual_delta_pnl = (q_s − |q_p|) · ΔX`, burada `q_s ≥ |q_p|` ise X = P_spot, değilse X = P_perp.

İkisinin toplamı `spot_price_pnl + perp_price_pnl`'e eşittir. Hedge error = `net/gross` ve `residual_delta_pnl`; hepsi günlük raporlanır. T2 için beklenen: basis +0,44554, residual +0,09901, toplam +0,54455.

**Sentetik el hesabı testleri (TDD, implementasyondan önce):**

| Test | Girdi | Beklenen |
|---|---|---|
| T1 "unit hedge" | 1 spot long @100 → 110; 1 perp short @101 → 110,55 | spot +10,00; perp −9,55; fiyat toplamı **+0,45** (basis 1,00 → 0,55 USD) |
| T2 "USD hedge" | N = 100: q_s = 1, q_p = −100/101 = −0,990099; aynı fiyatlar | perp −9,4554; fiyat toplamı **+0,5446** = N·(S1/S0 − P1/P0) |
| T3 funding | 24 saat, fundingRate (abs) = +0,0101 USD/birim/saat (rel 0,0001 × 101), q_p = −1 | short alır **+0,2424**; abs = −0,0101 → **−0,2424** |
| T4 maliyetler (T1 üzerinde; spot taker %0,80, perp taker %0,05, spot 20 bps full, perp 10 bps full) | — | spot ücret 0,80 + 0,88; perp ücret 0,0505 + 0,055275; spot yarım-spread 0,10 + 0,11; perp yarım-spread 0,0505 + 0,055275; toplam maliyet **2,10155** |
| T5 net (T1 + T3 + T4) | — | 0,45 + 0,2424 − 2,10155 = **−1,40915** |

### 5.2 H2 hesabı

| Kalem | Değer |
|---|---|
| Başlangıç equity | **$265** (sanal perp hesabı). |
| Exposure | Dollar-neutral: rebalance anında gross 1,0 × E, net 0. |
| Margin / likidasyon | Carry hesabıyla aynı. |
| Getiri | `E_d / E_{d−1} − 1`, aynı değerleme anı. |
| Ayrışım (zorunlu) | `funding_pnl`, `price_pnl`, `fees`, `spread_slippage`. |
| Carry bileşeni | `carry_net_d = (funding_pnl_d − fees_d − spread_d) / E_{d−1}`. Ayrı seri olarak test edilir (C6). |
| Diagnostic | Net exposure / E, residual BTC beta (H2 günlük getirisinin BTC perp günlük getirisi üzerine OLS eğimi), long ve short bacak P&L'leri ayrı. Konsantrasyon: günlük `max_i |notional_i| / gross` ve Herfindahl `Σ w_i²` (`w_i = |notional_i| / gross`), varlık başına P&L katkısı. Hepsi diagnostic, GO'ya girmez. |

### 5.3 Ücret ve spread

**Ücretler (SC-2):**
- Spot: maker %0,40, taker %0,80.
- Perp: maker %0,02, taker %0,05.
- Taker primary. Maker yalnızca secondary; GO'ya katkı vermez.

**Spread:**
- Tanım: quoted **full** spread = (ask − bid) / mid. Taker her işlemde **yarım** spread öder.
- Tarihsel spot: primary 20 bps full, secondary 10 bps full.
- Tarihsel perp: analytics `spreads`'ten kararın önceki saatindeki [t−1h, t) 1 dakikalık değerlerin ortalaması alınır. Veri yoksa 10 bps full kullanılır ve bu sayılır.
- Forward (iki bacak): yürütme snapshot'ındaki gerçek bid/ask. Emir top-of-book'u aşarsa yürütmeden önceki en yakın derinlik snapshot'ında book walk yapılır.

### 5.4 Zorunlu sensitivity satırları

| Satır | Tarihsel | Forward | GO'ya katkı |
|---|---|---|---|
| **PRIMARY** | taker, spot 20 bps full | taker, gerçek bid/ask | Evet |
| S-SPREAD10 | taker, spot 10 bps full | — | Hayır |
| **S-COST2X** | 2× taker, 2× spread | 2× taker, 2× gerçekleşen yarım-spread ve book-walk | Evet (C3) |
| S-MAKER | maker, spot 20 bps full | maker, gerçek bid/ask | Hayır |

## 6. Kanıt birimi ve kabul kriterleri (frozen)

**Kanıt birimi:**
- V2'nin "n ≥ 30 trade" kuralı V4'e **uygulanmaz**; bacaklar ve pozisyonlar bağımsız gözlem değildir.
- Kanıt birimi **günlük net portföy getirisi**dir.

**Bootstrap ve p-değeri:**
- Merkezlenmiş moving block bootstrap. B = 10.000, seed 20261009 (her config ve her b için aynı).
- Blok başlangıçları {0, …, n−b} üzerinde düzgün dağılımla çekilir; ⌈n/b⌉ blok birleştirilip n'ye kesilir (dairesel değil). LB = tek taraflı percentile alt sınırı.
- `p = (1 + #{b : m*_b − m̂ ≥ m̂}) / (B + 1)`; m = ortalama günlük net getiri.
- **Blok uzunlukları (kullanıcı kararı, final):**
  - **Primary b = 28 gün.**
  - **Zorunlu robustness b = 14 gün.**
  - GO için ikisinin de alt güven sınırı > 0 olmalı.
- **Holm:** Her bakışta 7 configin p-değerleri üzerinde, bakış başına α = 0,05/3. Her blok uzunluğu için ayrı Holm uygulanır; ikisi de reddetmeli.
- Holm-reddi, Holm'un o config'e verdiği α seviyesindeki tek taraflı alt güven sınırının > 0 olmasıyla eşdeğerdir.
- **Başlık sonucu** b = 28 sonucudur; b = 14 zorunlu robustness olarak yanında raporlanır.

**Minimum kanıt.** Yalnızca örnekleme yapısından türetildi, hiçbir V4 getirisi görülmeden:

| Şart | H1 / H5 | H2 | Türetme |
|---|---|---|---|
| M1: Değerlendirme penceresi | ≥ 182 takvim günü | ≥ 182 | Primary b = 28 ile ≥ 6 blok; robustness b = 14 ile ≥ 13 blok |
| M2: Veri-kalite kapsamı | M1 günlerinin ≥ %90'ı DQ-geçerli (§7.2) | aynı | Boşluklu veriyle ortalama yanlı olur |
| M3: Aktif exposure | ≥ 60 pozisyonlu gün | ≥ 150 pozisyonlu gün (sabit) | H1 koşullu (en az ~1/3); H2 sürekli yatırımda. Flat bir hafta (< 2k uygun varlık) M4 için karar sayılır, M3 için aktif gün sayılmaz. |
| M4: Karar sayısı | ≥ 4 ayrı giriş (açık veya kapalı), ≥ 2 farklı varlıkta | ≥ ⌊pencere/7⌋ − 2 rebalance kararı (182 gün → 24). Kararlar örtüşür; bağımsız sayılmaz. | 3 slot, 90 gün maks tutma ve 7 gün cooldown ile 182 günde slot başına yaklaşık 1–2 döngü olur; "6 döngü" yapısal maksimumdu, bu yüzden 4 giriş seçildi |

Bir şart sağlanmazsa o bakışta sonuç **INSUFFICIENT DATA** olur. (Eski M5, M1 ile özdeş olduğu için kaldırıldı.)

**Kriterler (hepsi gerekli):**

| # | Kriter |
|---|---|
| C1 | M1–M4 sağlanmış. |
| C2 | PRIMARY ortalama günlük net getiri: Holm reddediyor, primary b = 28 **ve** robustness b = 14 (ikisinin de alt sınırı > 0). |
| C3 | S-COST2X ortalama günlük net getiri > 0 (nokta tahmini). |
| C4 | Yıllıklandırılmış günlük Sharpe (ortalama / std × √365) > null'ların ortalama Sharpe'ı. H1/H5 için ayrıca > N-ALWAYS Sharpe'ı. |
| C5 | Maks drawdown ≤ %15; en kötü gün ≥ −%5; günlük ES(97,5%) ≥ −%3 (ES = en kötü %2,5 günlük getirilerin ampirik ortalaması). |
| C6 | Carry bileşeni. H2: Holm, **4 H2** `carry_net` p-değeri üzerinde, α = 0,05/3, b = 28 (primary) ve b = 14 (robustness) için ayrı ayrı; ikisi de reddetmeli. H1/H5: `Σ funding_cf − fees − spread` > 0 (nokta). Bu şart geçmezse toplam P&L pozitif olsa bile **NO-GO**. |
| C7 | Yalnızca tam takvim ayları sayılır, en az 3 ay gerekir. Ayların ≥ %60'ında aylık ortalama getiri > 0. 3 aydan azsa C7 = FAIL. |

## 7. Aşamalar

### 7.1 Stage 0 (getiri hesaplanmaz)

- S0-F (§2): **tamamlandı**.
- `backfill-v4`: mark/index 15m, analytics spreads, gerçek funding; hepsi `< 2026-07-01`.
- Boşluk raporu (FET 2024 dahil).
- Spot gap-fill'in development dönemine kadar tamam olduğu doğrulanır.
- Enstrüman metadata'sı (min emir, adım) freeze commit'inde snapshot olarak alınır ve SHA256'sı commit'e yazılır. Stage 1 de bunu kullanır; bu bilinen bir anakronizmdir ve raporlanır.
- Look-ahead testleri: t sonrası veri bozulduğunda karar değişmemeli.

### 7.2 Forward veri-kalite kapısı (frozen, sonuçtan bağımsız)

**Belirleme yöntemi:** Eşikler collector'ın tasarım frekansından ve mühendislik gereksinimlerinden belirlendi; P&L, getiri, Sharpe ya da strateji sonucu kullanılmadı. İlk saatlerin ölçümleri (paired skew ~80 ms, saat kayması ~80 ms) yalnızca sağlama kontrolü olarak kullanıldı, optimizasyon için değil. Forward dönemde eşikler pratikte fazla katı ya da gevşek görünse bile V4'te **değiştirilmez**; o veri DQ FAIL ya da INSUFFICIENT DATA olur. Eşik değişikliği ancak spec değişikliği veya V5 ile yapılabilir.

| # | Kural | Eşik | Gerekçe |
|---|---|---|---|
| Q1 | Spot/perp eşleştirme | Basis ve mid yalnızca aynı `pair_seq`'teki `fut_tickers` ve `spot_ticker` ok çiftinden hesaplanır; `|recv_spot − recv_fut| ≤ 2.000 ms` | İki istek ardışık; beklenen fark ≈ bir istek gecikmesi (~0,1 s). 2 s, 20× pay bırakır ama dakika ölçeğinde kaymayı keser. |
| Q2 | İstek tazeliği | `recv − req ≤ 5.000 ms`; aşan snapshot geçersiz | 60 s'lik frekansta 5 s'lik gecikme kotasyonun bayatladığını gösterir. |
| Q3 | Saat anomalisi | Futures: `|recv − serverTime| ≤ 5 s` (snapshot başına). Spot: `|recv − Time.unixtime| ≤ 5 s` (5 dk'lık kontrol; aşarsa sonraki 5 dk'nın spot snapshot'ları geçersiz). | Collector saati ile borsa saati arasında 5 s'yi aşan fark, zaman damgalı eşleştirmeyi güvenilmez yapar. |
| Q4 | Kotasyon geçerliliği | Varlık başına: bid > 0, ask > 0, bid < ask, varlık payload'da var ve `suspended` değil | Aksi halde mid ve spread tanımsız. |
| Q5 | Günlük kapsam | Gün geçerliliği için Q1–Q4'ü geçen ticker çifti sayısı ≥ 1.368 / 1.440 (%95) | Dakikalık frekansta %5 kayıp (~72 dk) gün ortalamasını bozmaz. |
| Q6 | Ardışık eksik snapshot | Gün içinde ardışık geçersiz veya eksik ticker çifti ≤ 15 | 00:00 karar anında 15 dakikalık yürütme penceresiyle uyumlu. |
| Q7 | Funding tazeliği | Her uygun varlık için günün 24 döneminin tamamı, dönem başlangıcından sonraki 3 saat içinde en az bir `fut_funding` yanıtında gözlenmiş olmalı | Saatlik toplama ve 48 saatlik süzme ile normal koşulda her dönem 3 kez görülür. API'nin kendi boşlukları nakit akışında 0 sayılır ve günü geçersiz yapar. |
| Q8 | Yürütme penceresi | Karar t'den sonraki 15 dakika içinde Q1–Q4'ü geçen ilk çift kullanılır; yoksa o günün kararları atlanır ve sayılır | §3 ile aynı. |
| Q9 | Derinlik (book walk) | Yürütmeden önceki en yakın `fut_book` / `spot_depth` snapshot'ı ≤ 10 dk eski olmalı; değilse yalnızca top-of-book kullanılır ve sayılır | 5 dakikalık frekansta en fazla bir tur kaçırmaya izin verir. |

**Geçerlilik birimi:**
- Q1–Q3 çift düzeyinde, Q4 varlık düzeyindedir.
- Bir `pair_seq`, varlık i için Q1–Q3 ve Q4(i) geçerse geçerlidir.
- Q8: işlemin bütün bacak varlıkları için geçerli olan ilk çift kullanılır.

**Geçerli gün (config başına):** Gün d, config c için şu durumda DQ-geçerlidir:
- c'nin d 00:00'daki uygun kümesindeki ve d boyunca tutulan her varlık için Q5, Q6 ve Q7 geçer;
- küme boşsa §2a evreninin tamamı için geçer.

M2 config başına hesaplanır.
- Geçersiz günler **getiri serisinde kalır**. Pozisyonlar muhasebelenir ve E_d ilk geçerli snapshot'tan alınır. Yalnızca M2 kapsamına sayılmazlar.
- Eşik ihlali bir DQ olayı olarak kaydedilir; ilgili kanıt geçersiz işaretlenir. Eşik değiştirilmez.

**Kesinti günü:** d+1 günü 23:59'a kadar hiç geçerli snapshot yoksa `E_d = E_{d−1}` alınır (r_d = 0). Değişim sonraki geçerli güne yazılır. O gün margin, hedge ve likidasyon kontrolleri atlanır ve sayılır.

### 7.3 Stage 1: tarihsel keşif (yalnızca gerçek funding)

- **Pencere:** 2025-10-06 → 2026-07-01. İlk 30 gün lookback; değerlendirme 2025-11-05 → 2026-07-01 (~238 gün ≥ M1).
- Fit yok, walk-forward yok: kurallar sabit.
- **Stage 1 M2:** Bir gün, funding ve bar kapsamı ayrı ayrı ≥ %90 ise geçerlidir. Günlerin ≥ %90'ı geçerli olmalı.
- **Stage 1 çoklu test:** Holm α = 0,05 (tek bakış), primary b = 28 ve robustness b = 14.
- **Etiket:** "EXPLORATORY — reused development period; not generalization evidence." Stage 1 tek başına GO veremez.
- **Stage 1 kararı (önceden kayıtlı, config başına):** `ADVANCE TO FORWARD` ya da `KILL`. Stage 1 GO veremez ve config optimize etmez.
  - **KILL**, aşağıdakilerden biri olursa:
    - (a) PRIMARY ortalama günlük net getiri < 0 **ve** primary b = 28 tek taraflı %95 üst güven sınırı < 0 (istatistiksel olarak negatif);
    - (b) carry bileşeni maliyeti karşılamıyor: H2 için `carry_net` ortalaması ≤ 0, H1/H5 için `Σ funding_cf − fees − spread ≤ 0`;
    - (c) config değerlendirme penceresinde hiç pozisyon açmadı (M3 = 0).
  - Diğer durumların hepsinde **ADVANCE TO FORWARD**. C1–C7 ve M1–M4 bilgi amaçlı raporlanır.
  - **Hipotez düzeyi (yalnızca özet):** H1 (2 config), H2 (4 config), H5 (1 config). En az bir config'i ADVANCE olan hipotez "forward'a geçti" olarak raporlanır. Forward seti config düzeyindedir: yalnızca ADVANCE edilen configler simüle edilir.
  - **Forward Holm ailesi:** Her bakışta aile = 7 config − önceki bakışlarda GO almış configler. KILL edilenler p = 1 ile ailede kalır. Böylece Stage 1 elemesi çoklu test düzeltmesini gevşetmez.
  - Tarihsel sonuç görüldükten sonra spec, kriter veya config değişikliği **yasaktır**.
- **Stage-1 adayı (yalnızca bilgi; holdout kill-test kullanıcı kararıyla kapalı):**
  - Stage 1'de C1–C7'yi geçen configler arasından PRIMARY m̂'nin b = 28 tek taraflı %95 alt sınırı en yüksek olan.
  - Eşitlikte daha küçük L, k ve Y.
  - Hiçbiri geçmezse aday yoktur.
- **Forward seti:** Stage 1'de ADVANCE edilen configler Stage 2'de izlenir; KILL edilenler raporda kalır ve tekrar açılamaz (yeni config = V5).
- **Proxy dönemi (2023 → 2025-10-06):** yalnızca diagnostic.
  - D-PERSIST'in proxy-rank versiyonu.
  - Venue basis dağılımı ve otokorelasyonu.
  - Rank/sign tabanlı sinyal sayıları.
  - **P&L yok.**

### 7.4 Stage 2: forward doğrulama (tek GO kanıtı)

- **Pencere:** [D0, bakış). M1, bu aralıktaki tam UTC günlerini sayar.
- **Lookback:** Yalnızca collector verisi kullanılır (warmup dahil). §7.5 sayesinde lookback'ler evidence başında dolu olur.
- **Bakışlar:** D0 + 182, D0 + 273 ve D0 + 365 gün, 00:00 UTC. **En fazla 3.**
- **Bakış semantiği:**
  - Her bakışta bir config, C1–C7'nin hepsi tutarsa **GO** olur. GO **kesindir** ve config sonraki Holm ailelerinden çıkar.
  - 1. ve 2. bakışta NO-GO veya INSUFFICIENT geçicidir.
  - 3. bakıştaki sonuç **kesindir**: yeterli kanıt yoksa INSUFFICIENT DATA.
  - Her bakışın α/3'ü, M şartları sağlanmasa da harcanmış sayılır.
- **Ara dönem:** Evidence simülasyonu bakış tarihinden önce **hiç çalıştırılmaz**. Forward diagnostic'ler (D-DISP, D-H5CELLS, D-PERSIST) de yalnızca bakış tarihinde hesaplanır. Aylık raporlar yalnızca collector sağlığını, kapsamı, boşlukları, geçerli gün sayısını ve M2 ilerlemesini gösterir.
  - Gösterilen tek sinyal benzeri bilgi, **uygun evren büyüklüğüdür** (yalnızca hacim ve listeleme filtreleri).
  - F7 eşik sayıları ve giriş/rebalance sayıları gösterilmez. F7 sayıları funding rejimini, yani H1'in brüt sonucunu ele verir.
  - Kural ihlal edilirse olay ledger'a "forward peek" olarak yazılır.

### 7.5 Freeze ve evidence başlangıcı (ayrık)

- **Freeze (kurallar):** S0-F, §2a evren kuralları ve §7.2 DQ eşikleri sabitlendikten ve final red-team review'da Critical = 0, Major = 0 çıktıktan sonra hemen tag'lenir. 72 saatlik bekleme kullanıcı kararıyla iptal edildi (2026-10-09). R9 gereği freeze'e kadar getiri hesaplanmaz.
- **Runner hard guard:** `luna research-v4` yalnızca şu durumda çalışır: `research-v4-spec-frozen` tag'i HEAD'in atası, çalışma ağacı temiz ve spec dosyası tag'deki haliyle birebir aynı (`git diff <tag> -- <spec>` boş).
- **`V4_EVIDENCE_START_TS = 2026-11-10T00:00:00Z`** (kullanıcı kararı, sabit).
  - Kuralı: `collector_start_ts` (2026-10-09T11:52:48Z) + 31 gün sonrasındaki ilk 00:00 UTC.
  - Collector başlangıcı ile bu an arasındaki veri yalnızca warmup ve DQ içindir.
  - Böylece 30 günlük hacim medyanı, L30, B30med ve F7 lookback'leri evidence başında dolu olur.
- **D0 = `V4_EVIDENCE_START_TS`** (her zaman bir 00:00 UTC).
- D0 = **2026-11-10 00:00 UTC**; bakışlar 2027-05-11, 2027-08-10, 2027-11-10.

## 8. Look-ahead ve sızıntı kuralları (özet)

- Sinyal: `open_ts ≤ t − 15m` barları ve yalnızca `T + 1h ≤ t` olan funding dönemleri (`known_rates`, §2).
- Yürütme: `open_ts = t` barının kapanışı (tarihsel) ya da t sonrasındaki ilk geçerli snapshot (forward).
- Funding: [T, T+1h) içinde tutulan süreyle orantılı yazılır (§2). Karar yalnızca bitmiş dönemlerin rate'ini görür.
- §3'teki dinamik uygunluk (hacim, listeleme yaşı, funding kapsamı), medyanlar ve D-DISP genişleyen medyanı yalnızca t öncesi veriyle hesaplanır; bugünün dinamik evren listesi geçmişe uygulanmaz. §2a statik veri-bütünlüğü filtresi bu kuralın istisnasıdır (bilinen anakronizm, §2a).
- **Testler:**
  - Her sinyal için "t sonrası veri bozulunca çıktı değişmez".
  - `HoldoutLeak`.
  - Forward yükleyici dönem başlangıcı `< collector_start_ts` olan funding satırlarını atar.
  - `premium_proxy` modülünün P&L kodundan import edilemediğini doğrulayan statik test.
  - Eksik funding saati 0 nakit akışı olarak sayılır.

## 9. Sonuç formatı

Her config için:
- **RESEARCH EDGE:** GO / NO-GO / INSUFFICIENT DATA;
- C1–C7 ve M1–M4 tabloları;
- P&L ayrışımı;
- sensitivity satırları;
- null'lar;
- diagnostic'ler;
- sayaçlar: atlanan işlem, minimum emir, bilinmeyen funding saati, likidasyon.

**DEPLOYABLE NOW: NO.** Sebepler:
- (a) UK'de perp ürünlerine perakende erişim kısıtlı; uygunluk teyit edilmedi.
- (b) Kodda canlı broker yok.
- (c) İki venue'yu senkron yürütme ve margin yönetimi yok.
- (d) Maker fill modeli yok.

Bu sonuç RESEARCH EDGE'i değiştirmez; RESEARCH EDGE: GO da canlıya geçişi otomatik açmaz.

## 10. Kullanıcı kararları (2026-10-09, kapalı)

1. **rev 2:** Onaylı. Freeze, §7.5'teki şartlar temiz çıkınca aynı gün yapılır; 72 saatlik DQ beklemesi iptal.
2. **Evidence başlangıcı:** `V4_EVIDENCE_START_TS = 2026-11-10T00:00:00Z`.
3. **Bootstrap:** Primary b = 28, zorunlu robustness b = 14. Holm ve 7 config sabit.
4. **Holdout:** Tamamen kapalı (R1).
5. **Backup:** Yalnızca kişisel olduğu doğrulanmış bir hedefe kopyalanır. Doğrulanamazsa backup uyarısı açık kalır ve kullanıcıya "personal backup target needed" denir.
