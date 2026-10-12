# V6 PROFITABILITY RESEARCH PLAN — REV 2

**Durum:** TASLAK rev 2 (2026-10-12). **Freeze edilmedi.** Kod yok, V6 backtest yok, holdout kapalı.

**Hedef:** Maliyetler sonrası, risk kontrollü ve tekrar edilebilir **mutlak** net pozitif getiri. "W'den iyi" yeterli değil.

**V5:** Olduğu gibi korunur.
- Tek koşu sonucu commit `fabd419` ile `feat/v5-adaptive-carry`'de, bayt-bayt aynı (summary `c0b7adc6…`, 195 tablo manifest ile eşleşiyor).
- Yeniden koşulmaz, tune edilmez.
- P-TIGHT / P-LOOSE / P-C8 arasından kazanan seçilmez.

**Kaynak:** §1'deki sayılar development dönemi (2025-11-10 → 2026-07-01) ve V5 tek koşu tablolarından gelir. Bunlar teşhistir; V6 hipotezlerinin motivasyonudur, kanıtı değil.
- Teşhis script'i: `scripts/v6_postmortem/v5_postmortem.py` (holdout'a erişmez, yeni sinyal test etmez).
- Çıktı: `docs/results/v6-postmortem/v5_postmortem.json`.

## 0. Rev 1 → rev 2 değişiklik kaydı

| # | Değişiklik |
|---|---|
| 1 | Öncelik **H2 → H3 → H1**. H2 ve H3 ana yol; H1 feasibility candidate (§3) |
| 2 | Conservative primary fee modeli: Kraken Tier 1, snapshot kuralıyla (§2). H1 fee-only round trip ve break-even matematiği (§3.3) |
| 3 | H1 tarihsel FINAL GO alamaz: holdout'ta yalnız ADVANCE / INCONCLUSIVE / NO-GO. Tarihsel execution taker; maker-first yalnız forward'da (§3.3) |
| 4 | H2 contamination kuralı düzeltildi. H2a ve H2b'nin bütün öğeleri tam tanımlandı (§3.1) |
| 5 | H2 validity gate artık **hard gate**: MODEL INVALID / MODEL INCONCLUSIVE. P12 = gate VALID, tek aralık [0.8, 1.25] (§3.1, §8) |
| 6 | H3 optimizer yerine deterministik risk-budgeting; bütün sabitler pre-register (§3.2) |
| 7 | Minimum kanıt gate'i: E1 aktif gün, E2 bağımsız episode, E3 farklı underlying. Sağlanmazsa INSUFFICIENT (§8.2) |
| 8 | Multiplicity: `p_GO = max(p_net, p_carry)` (IUT) ve 4 config üzerinde Holm (§6.4) |
| 9 | P8 konsantrasyonu: varlık düzeyinde ana kontroller; episode düzeyi yalnız ≥ 10 kapanmış episode varsa (§8.3) |
| 10 | P9 alt pencerelerine aktiflik şartı (§8.4) |
| 11 | Süreç sırası ve holdout'un rolü: aday seçimi, confirmation değil (§6.1, §9) |
| 12 | Forward coverage kuralı; collector boşlukları (§5) |
| 13 | Bağımsız red-team: 2 tur ve bir kapanış kontrolü (§11) |
| 14 | H1 forward feasibility ölçümü; terminal kural (§3.3, §6.4) |

## 1. V5 failure decomposition

### 1.1 Tek denklem

`Net = Funding − (Fee + Spread/slip) + Price`

| (USD, 265$ ile) | Funding | Maliyet | Carry alpha | Price | Net |
|---|---|---|---|---|---|
| E PRIMARY | +2.07 | 1.61 | **+0.46** | −0.66 | −0.20 |
| E S-NOKILL | +2.77 | 1.73 | **+1.05** | +11.32 | +12.37 |
| C S-NOKILL | +1.78 | 1.65 | +0.14 | −25.23 | −25.09 |
| W S-NOKILL | +6.23 | 10.58 | **−4.35** | −22.12 | −26.47 |
| D S-NOKILL | +8.89 | 27.75 | **−18.85** | −10.45 | −29.31 |

Mekanik özet:
1. **Toplanabilir carry küçüktü.** İdeal FL30 kitabı (k=2, maliyetsiz, hep tam yatırımlı) bütün dönemde yalnız **11.14$** funding toplardı (equity'nin %4.2'si).
2. **Maliyet bu küçük carry'yi yiyor.** E'de maliyet funding'in %78'i. W'de %170, D'de %311 (haftalık/günlük rotasyon carry'den pahalı).
3. **Price P&L'in gürültüsü carry'den 1–2 kat büyüklük fazla.** Fiyat beklentisi sıfıra yakın olsa bile varyansı sonucu belirliyor ve %15 kill kuralı bu varyansı kalıcı düz pozisyona çeviriyor.

### 1.2 E neden yalnız 6 giriş yaptı, hiç SWITCH yapmadı?

E PRIMARY'nin 37,179 karar kaydı:

| Karar | Adet |
|---|---|
| HOLD: entry_carry_below_cost | 14,169 |
| HOLD: in_target_band | 13,292 |
| HOLD: cost_exceeds_advantage (switch) | 5,543 |
| HOLD: no_better_candidate | 1,528 |
| HOLD: min_hold | 836 |
| HOLD: below_min_improvement | 793 |
| ENTER pair | **6** |
| SWITCH | **0** |

- **Giriş eşiği yüksek.** Pair değeri `N(c_s+c_l)·168h − 1.5·N·2(cost_s+cost_l) − 2B > 0` olmalı. Medyan maliyetle (0.316$) bu, `FL_short − FL_long ≥ ~%25/yıl` demek. Evrendeki **en iyi** pair'in medyan farkı %28.8/yıl; günlerin yalnız %58'inde en iyi pair eşiğin üstündeydi (gate'lerden önce). Reddedilen girişlerde medyan beklenen/maliyet oranı 0.68.
- **Ufuk uyumsuzluğu.** Maliyet yalnız 168 saatlik beklenen carry'ye karşı tartılıyor; E'nin bacakları ise ortalama 458 saat tutuldu. Kural tasarımı gereği gerçek tutma süresine göre ~2.7 kat sıkı. (Bu bir bug değil, bir tasarım özelliği; V5'te değiştirilmez.)
- **SWITCH neredeyse hiç kârlı görünmedi.** Aday varken 5,543 kez artımlı carry < switch maliyeti (medyan oran 0.69). Kalan engeller min_hold ve min_improvement.
- **Çıkışlar ekonomik değil, zorunlu.** 12 bacağın çıkış nedenleri: stop 3, ortağın stop'u sonrası neutrality_unwind 5, low_volume ineligibility 2, kill 2. Pair yapısı bir bacağın stop'unu iki bacaklık maliyete ve carry kaybına çeviriyor.
- **Düz kalınan süre uzun.** 2026-02-03 → 03-16 arasında 41 gün eşiği geçen pair yoktu. 2026-04-14 kill'inden sonra 78 gün kalıcı düz.
- 6 girişin 3'ünde **ZEC long** vardı (en negatif FL30). Üçü de %15 leg stop ile kapandı.

### 1.3 Funding P&L neden yalnız +2.07$?

`Funding ≈ ortalama dağıtılmış gross × gerçekleşen carry getirisi × süre`

- **Dağıtım:** dönem ortalaması %22.9 equity. Yatırımlı günlerde gross ~0.47, yatırımlı gün 112/233.
- **Gerçekleşen carry getirisi:** dağıtılmış notional üzerinde ~%5/yıl. FL30'un vaat ettiğinin yaklaşık yarısı (§1.8).
- **Süre:** 0.64 yıl; kill sonrası 78 gün sıfır.
- Sonuç: 275$ × 0.23 × %5 × 0.64 ≈ 2$. İdeal referansın %18.6'sı.

### 1.4 Neden price P&L, funding'den çok daha belirleyiciydi?

İdeal FL30 kitabı için (gross başına, eşit 4 bacak):

| Ufuk | Ortalama gerçekleşen funding | Price getirisi std | Gürültü / carry |
|---|---|---|---|
| 24 saat | +0.022% | 1.86% | ~86× |
| 168 saat | +0.117% | 5.44% | **~46×** |

Yıllıklaştırılmış hali: funding ~%6.1/yıl, fiyat oynaklığı ~%35/yıl. Yani maliyetten **önce** carry Sharpe tavanı **~0.17**. Dollar-neutral olsa da k=2 ile her bacak equity'nin %25'i ve idiosyncratic hareketler (ZEC −%31, SUI, HYPE) her şeyi belirliyor. E PRIMARY'nin −0.66$ price sonucu şanstır: aynı stratejinin S-NOKILL'de +11.32$, C'nin −25$ olması aynı gürültünün farklı çekilişleridir.

### 1.5 2026-04-14 kill'ine giden drawdown

R4, **tüm zamanların zirvesinden** ölçer: 310.12$ (2025-12-29). 0.85 × 310.12 = 263.6$.

1. **Zirve → 2026-02-03 (310 → 272.8$).** ZEC long'un (2025-12-02 girişli) Aralık rallisi equity'yi 265 → 310'a taşıdı. Sonra ZEC zirveden **−%30.8** düştü: pencerede **−42.98$**. BTC short +7.55$ ile kısmen dengeledi (BTC −%15.2). ZEC, 02-03'te girişe göre %15 stop'a takıldı; BTC neutrality_unwind ile kapandı.
2. **41 gün düz** (272.8$).
3. **2026-03-16 → 04-14: TAO long + ETH short.** Equity önce 295.6$'a çıktı (03-26), sonra özellikle 04-09 → 04-10'da tek günde −13.5$ olmak üzere 263.6$'ın altına indi. Bacakların girişten çıkışa toplamı: TAO −5.20$, ETH −3.45$.

Mekanizma: **kill, bir fiyat rüzgârının (ZEC rallisi) geri verilmesiyle tetiklendi**, carry'nin bozulmasıyla değil. Pencere boyunca funding +1.48$ idi.

### 1.6 Long ve short bacakların price katkısı

| | Long price | Long funding | Short price | Short funding |
|---|---|---|---|---|
| E PRIMARY | −28.34 | +1.31 | +27.68 | +0.76 |
| E S-NOKILL | −31.49 | +1.68 | +42.82 | +1.10 |
| C S-NOKILL | −55.34 | +0.94 | +30.11 | +0.85 |
| W S-NOKILL | −12.69 | +5.20 | −9.44 | +1.03 |
| D S-NOKILL | −12.00 | +7.24 | +1.55 | +1.65 |

- İki taraf ortalamada birbirini kısmen dengeliyor; ama her tarafın ±30–55$'lık yolu, toplam funding'in (2–9$) çok üstünde.
- Dönemde long taraf (negatif funding'li varlıklar) daha fazla funding kazandırdı ve daha çok fiyat kaybettirdi.

### 1.7 Dollar-neutral olmasına rağmen residual exposure nereden geldi?

Saatlik pozisyonlardan, PIT 30 günlük beta ve vol ile. Yaklaşık değerler: fill'ler saate yuvarlandı. Satır başına ortalamalar, yalnız yatırımlı günler:

| | \|net $\| / equity | Net beta / equity | Long vol | Short vol | Long beta | Short beta | BTC R² (günlük price P&L) |
|---|---|---|---|---|---|---|---|
| E PRIMARY | 4.6% (max 11.8%) | +0.10 | 1.50 | 0.59 | 1.34 | 1.17 | **0.0%** |
| W S-NOKILL | 3.4% (max 11.7%) | +0.10 | 1.07 | 0.65 | 1.35 | 1.14 | 0.7% |
| D S-NOKILL | 3.5% (max 11.8%) | +0.12 | 1.09 | 0.70 | 1.37 | 1.17 | 0.1% |
| C S-NOKILL | 3.9% (max 10.9%) | +0.09 | 1.53 | 0.61 | 1.39 | 1.18 | 0.1% |

- **Net dolar** küçük: %10 trim kuralı çalışıyor. ZEC'te 497 küçük trim, min qty altında kaldığı için reddedildi; etkisi küçük.
- **BTC beta** hafif pozitif (+0.1), ama günlük price P&L'in BTC ile açıklanan varyansı **%1'in altında**. Yalnız BTC-beta-neutral yapmak bu sorunu çözmezdi.
- **Asıl residual: vol uyumsuzluğu ve konsantrasyon.**
  - En düşük funding'li (long) varlıklar yüksek vol'lü küçük coin'lerdi (ZEC, TAO, ENA, NEAR, AVAX), en yüksek funding'liler (short) daha düşük vol'lü büyük coin'ler (BTC, ETH, SUI, BNB).
  - Long tarafı short'un **~2–2.5 katı** oynaklık taşıyor.
  - Kitap fiilen "long yüksek-vol küçük coin, short büyük coin" faktör pozisyonu ve 2+2 bacakla idiosyncratic riskin hepsi kitapta kalıyor.

### 1.8 Hangi varlıklar funding kazandırıp fiyat kaybettirdi?

| Satır | Varlık / yön | Funding | Price |
|---|---|---|---|
| E PRIMARY | ZEC long | +0.39 | −14.38 |
| E PRIMARY | TAO long | +0.90 | −5.20 |
| E PRIMARY | ENA long | +0.03 | −8.76 |
| W S-NOKILL | HYPE short | +0.34 | −31.91 |
| W S-NOKILL | AVAX long | +0.44 | −19.53 |
| W S-NOKILL | NEAR long | +0.61 | −14.84 |
| D S-NOKILL | HYPE short | +0.78 | −40.62 |
| D S-NOKILL | ENA long | +0.43 | −23.62 |
| D S-NOKILL | ZEC long | +1.59 | −14.18 |
| C S-NOKILL | ZEC long | +0.19 | −41.16 |

**Tek varlık baskınlığı:** W ve D'de TAO long hem funding'in en büyük kaynağı (W +2.45$, D +2.62$) hem de en büyük fiyat kazancı (+34.8$ / +32.2$). Sonuç birkaç varlığın idiosyncratic yoluna bağlı.

### 1.9 Gerçekleşen funding neden FL30'un sistematik olarak altında kaldı?

| Seçim | Beklenen (FL30) | Gerçekleşen 24 sa | 72 sa | 168 sa | Tutma süresi |
|---|---|---|---|---|---|
| İdeal top-2 / bottom-2 kitap (226 gün) | %13.0/yıl | %7.9 (0.61) | %7.2 (0.55) | %6.1 (0.47) | — |
| V5 bacakları (332 benzersiz giriş, tüm arm'lar) | %8.7/yıl | %6.4 (0.73) | %5.5 (0.64) | %4.7 (0.54) | %4.4 (0.50) |

- **Uç değerlere regresyon.** Ranking ucu seçiyor ve FL30'un geçici bileşeni hemen sönüyor: beklentinin ~%40'ı ilk 24 saatte gidiyor. 30 günlük ortalama, çoktan bitmiş funding patlamalarını içeriyor.
- **Sıralama kalıcı, büyüklük değil.** Rank IC (FL30 → gelecekteki funding) 24/72/168 saatte 0.34 / 0.39 / 0.43. Yani "hangisi" doğru, "ne kadar" yanlış. V5'in maliyet kararları ise büyüklüğü kullanıyor; bu yüzden V5 sistematik olarak yarısı buharlaşacak carry için maliyet ödedi.
- **Short taraf (pozitif funding) neredeyse yok oldu:** beklenen %4.8, 168 saatte gerçekleşen %1.5. Long taraf (negatif funding) daha kalıcı: %12.7 → %8.1.
- Bacakların **%29.5'inde** funding işareti 168 saat içinde döndü.

### 1.10 Tutma süresi uzadıkça funding edge sönüyor mu?

V5 bacakları, girişten itibaren yaş kovalarına göre:

| Yaş | Bacak | Gerçekleşen | Beklenen (girişte) | Oran |
|---|---|---|---|---|
| 0–24 sa | 330 | %6.1 | %8.9 | 0.69 |
| 24–72 sa | 226 | %5.6 | %9.1 | 0.62 |
| 72–168 sa | 142 | %5.0 | %9.4 | 0.53 |
| 168–336 sa | 60 | %7.0 | %10.8 | 0.65 |
| 336–720 sa | 32 | %5.3 | %14.8 | 0.35 |
| 720 sa+ | 6 | %4.7 | %16.8 | 0.28 |

Evet. Uzun tutulan bacaklar girişte en yüksek vaadi taşıyanlar, ama 2 haftadan sonra vaadin ~1/3'ünü gerçekleştiriyorlar. V5'in çıkış mantığı (maliyet > avantaj olduğu için SWITCH/EXIT nadiren tetikleniyor) onu bu sönmüş pozisyonlarda tutuyor. Çıkışı ekonomik değil, stop belirliyor. (Kova bileşimi değişiyor; küçük örneklem, dev dönemi.)

### 1.11 Giriş anındaki edge ile 24 / 72 / 168 saatlik gerçekleşen funding

§1.9'daki tablo bunu veriyor: gerçekleşen/beklenen oranı kitapta 0.61 / 0.55 / 0.47, bacaklarda 0.73 / 0.64 / 0.54; rank IC 0.34 / 0.39 / 0.43.

### 1.12 Sonuç: V5 neden net kârlı değildi?

1. Funding-carry evreninde dönem boyunca toplanabilir carry az: ideal kitapta maliyetsiz %6/yıl.
2. V5 bunun da yarısını FL30 büyüklüğünü fazla tahmin ederek, maliyet ödediği pozisyonlarda kaçırdı.
3. Eşik yüksek olduğu için kapitalin çoğu zamanını düz geçirdi.
4. Taşıdığı birkaç bacak, dollar-neutral olsa da yüksek idiosyncratic ve vol-uyumsuz fiyat riski taşıdı. Carry Sharpe tavanı ~0.2 olan bir yapıda bu varyans önce net sonucu, sonra kill üzerinden dağıtım süresini belirledi.

**Bu, eşik tune edilerek düzeltilebilecek bir sorun değil:** carry / fiyat riski oranı yapısal olarak düşük.

---

## 2. Fee modeli (conservative primary)

**Kaynak:** Kullanıcının verdiği güncel public Kraken Tier 1 tarifesi, snapshot tarihi 2026-10-12. Hesaba özel tier doğrulanana kadar primary varsayım budur.

| Piyasa | Maker | Taker |
|---|---|---|
| Spot | 0.40% | 0.80% |
| Futures (perp) | 0.02% | 0.05% |

**Değişiklik kuralı:**
- Hesaba özel tier doğrulanırsa, **spec freeze'den önce** ve tam bir snapshot ile bu tablo değiştirilebilir: tarih, kaynak (hesap ekranının ekran görüntüsü ve sha256'sı, ya da API yanıtı), değerler.
- Freeze'den sonra ve özellikle outcome görüldükten sonra değiştirilemez.
- Daha düşük gerçek fee'ler yalnız bilgi amaçlı bir sensitivity satırında gösterilebilir.
- **Freeze öncesi zorunlu:** Public tarifenin kaynağı da (URL ve erişim tarihi ya da ekran görüntüsü hash'i) spec'e yazılır.

**Fee dışı maliyetler (her bacak, her yön):**
- **Perp half spread:** V5'teki PIT 5 dakikalık seri.
- **Spot half spread:** Tarihsel veri yok. Primary varsayım `max(2 × aynı andaki perp half spread, 5 bps)`.
- **Slippage:** 2 bps.
- **Fill zamanı:** t + 15 dakika (V5 DelayedExecutor).
- **Karar anı:** Kararlarda kullanılan maliyet tahminleri (ör. H1'in `RT_i`'si) yalnız `t`'de bilinen spread'i kullanır. Fill maliyeti ise fill anındaki spread'dir.

---

## 3. Hipotez aileleri (öncelik: H2 → H3 → H1)

H2 ve H3 profitability için ana araştırma yoludur. H1 ekonomik olarak mantıklı, ama Tier 1 maliyetleri nedeniyle **feasibility candidate** olarak ele alınır.

### 3.0 Ortak tanımlar

- **Funding:** `f_i(p)`, Kraken PF saatlik göreli funding'i. `p` dönemin başlangıcıdır, değer `p + 1h`'de bilinir; pozitif değerde longlar short'lara öder.
- **Karar ızgarası:** Ekonomik kararlar ve model güncellemesi her gün 00:00 UTC'de. İzleme (stop, R3/R4, ineligibility) V5 replay'indeki gibi 15 dakikada bir.
- **Evren:** V4 U1–U7 kuralının 20 varlığı (MUBARAK dışarıda). Uygunluk gate'leri V5'teki gibi: karar penceresi spread ≤ 15 bps, 24 saatlik hacim ≥ 1M$, funding kapsamı ≥ %90, en yeni funding ≤ 3 saat.
- **Risk profili:** V5 `carry.risk` değişmeden; R4 %15.
  - **H3:** Bacak bazlı stop %15, cooldown 24 saat.
  - **H1:** Stop, spot+perp **birleşik** P&L üzerinden işler: hedge'li notional'ın −%15'i. Her çıkış iki bacağı birlikte kapatır. Çıkış veya hedge reset sonrası o varlıkta 24 saat cooldown.
- **Pencere sonu:** Değerlendirme penceresinin sonunda açık pozisyonlar mark-to-market edilir ve PRIMARY sonuçlara **terminal unwind maliyeti** dahil edilir: o anki spread, fee ve slip ile çıkış. Mutlak kâr iddiası, çıkış maliyetini ödenmiş sayar. Forward ara bakışlarında (gün 90, 180) bu unwind **varsayımsaldır**: yalnız değerlendirmede düşülür, pozisyonlar devam eder.

### 3.1 H2 — Kalibre carry tahmini (hard gate)

**Contamination kuralı:**
1. Estimator formu, feature'lar, hyperparameter'lar, pencereleme, regularisation, başlatma, minimum geçmiş, eksik-veri kuralı ve ufuklar **outcome görülmeden freeze edilir.**
2. Her karar anı `t`'de estimator yalnız `t`'den önce mevcut veriyle (PIT) fit ya da update edilir.
3. Holdout ve forward koşularında bu fit'e development dönemi gözlemleri de girebilir.
4. Hiçbir hyperparameter ya da yöntem, dev, holdout veya forward outcome'una bakılarak seçilemez ya da değiştirilemez.

#### H2a — Pooled PIT shrinkage regresyonu (H1 ve H3'ün kararlarında kullanılan tek model)

| Öğe | Freeze edilen tanım |
|---|---|
| Feature | `X_i(t) = FL30_i(t)` = `p ∈ [t − 720h, t − 1h]` dönemlerinin ortalaması; en az 648 bilinen dönem yoksa eksik |
| Etiket | `Y_i(s, h)` = `p ∈ [s, s + h − 1h]` dönemlerinin ortalama funding'i; dönemlerin ≥ %90'ı bilinmiyorsa eksik; `s + h`'de bilinir |
| Örnek tarihleri | Günlük 00:00 UTC ızgarası `s` |
| Model | `Y = a(h) + b(h)·X + e`, varlıklar ve tarihler üzerinden havuzlanmış, eşit ağırlıklı OLS |
| Ufuklar | `h = 168h` (H3) ve `h = 720h` (H1); ayrı regresyonlar |
| Pencere | **Expanding**: `S0`'dan `t`'ye kadar, etiketi `t`'de tamamen bilinen (`s + h ≤ t`) bütün örnekler |
| Başlatma (`S0`) | `X`'in en az 10 varlık için mevcut olduğu ilk günlük tarih (~2025-11-03) |
| Minimum geçmiş | En az 60 farklı örnek tarihi ve tarih başına ortalama ≥ 10 varlık. Yoksa model yok: `MODEL WARMUP`, yeni pozisyon açılmaz |
| Aykırı değer | Her fit'te `X` ve `Y`, o fit örnekleminin havuzlanmış %1 ve %99 yüzdeliklerine kırpılır. Tahmin anında `X_i(t)` **aynı fit'in kesme değerlerine** kırpılır |
| AR tahmini | Ayrı bir AR(1) yok. `b(h)`, FL30 seviyesinin `h` ufkundaki havuzlanmış kalıcılık katsayısıdır ve OLS ile tahmin edilir |
| Yenileme | Günlük 00:00 UTC |
| Tahmin | `F̂_i(t, h) = â(t, h) + b̂(t, h)·clip(X_i(t))` |
| Eksik veri | `X_i(t)` yoksa tahmin yok, giriş yok. Tutulan pozisyonun tahmini 24 saatten uzun süre yoksa çıkılır |

#### H2b — Çok değişkenli ridge (yalnız diagnostic; hiçbir karar ve gate'de kullanılmaz)

| Öğe | Freeze edilen tanım |
|---|---|
| Feature'lar ve minimum kapsam | FL30 (≥ 648/720 dönem), FL7 (≥ 151/168), FL1d (≥ 22/24), PREM24 = son 96 kapanmış 15 dakikalık bar'da `mark/index − 1` ortalaması (≥ 86/96 bar) |
| Sıra | Önce winsorize (%1/%99, H2a gibi), sonra z-skor (o fit örnekleminin ortalaması ve std'si; PIT) |
| Amaç | `(1/n)·Σ(y − a − Σ b_j z_j)² + λ·Σ b_j²`, **λ = 0.01**, kesişim cezasız |
| Diğer | Etiket, örnek tarihleri, ufuklar, expanding pencere, başlatma ve minimum geçmiş H2a ile aynı. Eksik feature'lı örnek atılır |

#### H2 validity gate (hard gate; her config için kendi ufku ve kendi `k`'sı ile)

1. **Tahminler:**
   - Her değerlendirme tarihi `s` için `F̂(s)`, `s` anında yapılmış PIT tahmindir; asla sonradan refit değildir.
   - Değerlendirme tarihleri `s + h ≤ değerlendirme penceresinin sonu` koşulunu sağlar. Hiçbir etiket pencere sonrasından veri kullanmaz.
2. **Seçim ve oran:**
   - **H3 config'i (`k` = 4 ya da 6, h = 168 saat):** Her `s`'de uygun varlıklarda `F̂` ile top-k ve bottom-k seçilir.
     - Tahmin spread'i `FS(s)` = top-k `F̂` ortalaması − bottom-k `F̂` ortalaması.
     - Gerçekleşen spread `RS(s)`, aynı seçimle `Y` ile hesaplanır.
     - `R = Σ_s RS / Σ_s FS`.
   - **H1 config'i (h = 720 saat):** Her `s`'de `F̂ > 0` olan `n⁺` varlıktan top-min(3, n⁺) seçilir; `n⁺ = 0` olan tarih atlanır. `R = Σ gerçekleşen seviye / Σ tahmin seviyesi`.
   - `Σ FS ≤ 1e−9` (ya da H1'de toplam tahmin ≤ 1e−9) ise sonuç INCONCLUSIVE.
3. **Gelecekteki eksik etiket** (seçimden sonra delist ya da uygunluk kaybı): Seçilen varlık atılmaz.
   - Dönemlerin ≥ %50'si biliniyorsa gerçekleşen değer, bilinen dönemlerin ortalamasıdır.
   - Aksi halde o varlığın `RS`'e (ve H1'de seviyeye) işaretli katkısı = `min(0, tahminin işaretli katkısı)` sayılır: kazanç asla varsayılmaz, öngörülen kayıp korunur. Bu durumların sayısı raporlanır.
   - Gate'in `Y` değerleri kırpılmaz.
4. **Eğim:** Değerlendirme örneğinde `Y`'nin `F̂` üzerine havuzlanmış OLS eğimi `c`. Kesitsel olarak demean edilmiş eğim de raporlanır (gate değil).
5. **CI'lar:** Değerlendirme tarihleri üzerinde moving block bootstrap; B = 10,000, seed 20261010. Blok uzunluğu 168 saat için 14 gün, 720 saat için 30 gün. İki taraflı yüzdelik aralık (%2.5 ve %97.5).
6. **Minimum kanıt:** 168 saat için ≥ 60 değerlendirme tarihi; 720 saat için ≥ 8 örtüşmeyen 720 saatlik pencere (~240 gün).

| Sınıf | Koşul |
|---|---|
| **INVALID** | Minimum kanıt sağlanmış **ve** (`R` CI95 tamamen < 0.8 ya da tamamen > 1.25, **veya** eğim CI95 üst sınırı ≤ 0) |
| **VALID** | Minimum kanıt sağlanmış **ve** INVALID değil **ve** `R ∈ [0.8, 1.25]` (nokta) **ve** eğim CI95 alt sınırı > 0 |
| **INCONCLUSIVE** | Diğer her durum (minimum kanıt eksikliği dahil) |

- VALID ve INVALID koşulları aynı anda sağlanırsa **INVALID kazanır.**
- **Sonucun stratejilere etkisi:**
  - Gate INVALID ise config P&L ne olursa olsun **MODEL INVALID / NO-GO** alır.
  - INCONCLUSIVE ise sonuç en fazla **MODEL INCONCLUSIVE** olur.
  - Şanslı P&L ile kötü tahmin modeli GO alamaz.
- **H1 holdout'ta:** 720 saatlik minimum kanıt ~100 günde sağlanamaz, bu yüzden en fazla MODEL INCONCLUSIVE olur. INVALID de olamaz, çünkü INVALID minimum kanıt ister.
- **P12** bu gate'in VALID olmasıdır. Tek kalibrasyon aralığı **[0.8, 1.25]**.
- **H2b** için aynı metrikler raporlanır, gate değildir.

### 3.2 H3 — Risk bütçeli perp-perp kitap (deterministik; serbest ceza katsayısı yok)

Optimizer kullanılmaz. Ağırlıklar kapalı formdan gelir, bu yüzden solver fallback'i de gerekmez.

| Adım | Freeze edilen tanım |
|---|---|
| Uygunluk | §3.0 gate'leri + H2a tahmini mevcut + **≥ 60 gün** mark geçmişi. `N_e` = uygun varlık sayısı (tek havuz; taraflar sıralamadan çıkar) |
| Sinyal | `s_i = F̂_i(t, 168h)` |
| Göreli carry | `m_t` = uygun varlıklarda `s`'nin medyanı. Bacak beklenen carry'si `c_i = −side·(s_i − m_t)` (USD/saat/USD) |
| Boyut ve buffer | `k' = min(k, ⌊N_e/2⌋)`; `k' < 3` ise kitap uygulanamaz. Rank buffer `r = min(2, ⌊N_e/2⌋ − k')` (bölgeler örtüşmez) |
| Seçim | Uygunlar `s`'ye göre azalan sırada. Short bölgesi = ilk `k'`, long bölgesi = son `k'` |
| Tutma | Tutulan bacak, kendi tarafında ilk `k' + r` sıra içinde kaldığı ve `c_i·168 ≥ −cost_i` olduğu sürece kalır (beklenen kayıp çıkış maliyetini aşmadıkça). Aksi halde çıkılır |
| Giriş | Boş slotlar sıra sırasıyla doldurulur. Aday `x` yalnız `c_x·168 > 2·cost_x` ise girer. Girmezse sıradaki aday denenir; ilk `k'` sıra biterse slot boş kalır. `cost` = `t`'de bilinen fee + half spread + slip, tek yön |
| Vol | `σ_i`: saatlik log mark getirilerinin EWMA'sı, yarı ömür **14 gün** (336 saat), ilk 30 gün başlatma. Yıllıklaştırma ×√8760 |
| Kovaryans | Son **60 gün** saatlik log getirilerinin eşit ağırlıklı örnek kovaryansı. Ledoit–Wolf (2004) **constant-correlation** hedefiyle analitik yoğunlukla küçültülür (δ*, [0, 1]'e kırpılır). Sonra `Σ_ij = ρ̃_ij·σ_i·σ_j` |
| Beta | `β_i = Σ_i,BTC / Σ_BTC,BTC`. BTC her zaman kovaryans hesabına girer (kitapta olmasa da) |
| Taraf içi ağırlık | `u_i = (1/σ_i) / Σ_{j∈taraf}(1/σ_j)` (inverse-vol, carry ölçeklemesi **yok**) |
| Taraflar arası bölüş | `B_L = Σ_L u_i β_i`, `B_S = Σ_S u_i β_i`. Beta-nötr: `g_L = G·B_S/(B_L + B_S)`, `g_S = G·B_L/(B_L + B_S)`. `\|g_L − g_S\| > 0.2·G` ise sınıra kırpılır (`g_L + g_S = G` korunur). `B_L ≤ 0` ya da `B_S ≤ 0` ise `g_L = g_S = G/2` |
| Kitap ölçeği | `G = min(1.0, σ* / σ̂_book)`. `σ̂_book = √(wᵀΣw)` birim-G kitap için. **σ\* = %8/yıl** (1 günlük normal ES97.5 = 2.34·σ_d ≤ %1 ⇒ ~%8.2/yıl; P6 ES sınırının yarısı) |
| Notional | `n_i = w_i·Equity`, qty step'e yuvarlanır. `max(min notional, 10$)` altındaki bacak düşer ve taraf içinde yeniden normalize edilir (bir kez) |
| Boyut bandı | Tutulan bacak yalnız `\|n_hedef − n_tutulan\| > 0.25·n_hedef` ise yeniden boyutlanır. Büyütme, V5 core'una eklenecek **açık ve testli bir R10 istisnasıyla** yapılır (yalnız V6 H3 için; V5 paper semantiği değişmez). Küçültme reduce-only |
| Uygulanamaz kitap | `k' < 3`, model yok ya da `B_L + B_S ≤ 0`. 24 saate kadar: mevcut bacaklar tutulur, yeni giriş yok, stop ve zorunlu çıkışlar aktif. 24 saatten uzun: bütün bacaklardan çıkılır |
| Varyantlar | **k ∈ {4, 6}**; başka hiçbir fark yok |

**Sabitler (hepsi freeze'de; hiçbiri dev P&L ile seçilmedi):**
- rank buffer 2;
- ufuk 168 saat;
- σ\* %8/yıl;
- kovaryans penceresi 60 gün, EWMA yarı ömrü 14 gün;
- LW constant-correlation;
- dolar sınırı 0.2·G;
- boyut bandı %25;
- minimum bacak 10$;
- minimum geçmiş 60 gün;
- infeasible grace 24 saat;
- rebalance 00:00 UTC;
- V5 risk profili.

**H3 ex-ante fizibilite (dürüst önsel):**
- Dev döneminde ideal FL30 kitabının maliyet öncesi carry'si ~%6/yıl idi (gross başına). σ\* = %8 ile G ≈ 0.35–0.4 olur, yani equity'de ~%2–2.5/yıl carry ve %8 vol; **maliyet öncesi Sharpe ≈ 0.3.**
- P4 (Sharpe ≥ 1.0) için kalibre tahmin ve çeşitlendirmenin carry/risk oranını **~3 kat** artırması gerekir.
- Holdout'ta Holm düzeyinde ADVANCE için Sharpe ≈ 5 gerekir.
- **Beklenti:** H3 holdout'ta büyük olasılıkla INCONCLUSIVE ya da NO-GO alır. Forward'da FINAL GO olasılığı düşük.
- Bu bir gerekçe değil, önseldir: kriterler buna göre gevşetilmez.

### 3.3 H1 — Hedge'li carry (spot long + perp short): feasibility candidate

#### Fee-only round trip (spot giriş+çıkış ve perp giriş+çıkış; hedge birimi başına notional)

| Yol | Hesap | Fee-only | All-in örnek* |
|---|---|---|---|
| Taker / taker | 0.80 + 0.80 + 0.05 + 0.05 | **1.70%** | ~1.94% |
| Maker / maker | 0.40 + 0.40 + 0.02 + 0.02 | **0.84%** | ~0.84% |
| Spot maker + perp taker | 0.40 + 0.40 + 0.05 + 0.05 | **0.90%** | ~1.00% |

\*Örnek: spot half spread 5 bps, perp half spread 3 bps, slip 2 bps/yön. Maker yönleri spread ve slip ödemez; taker yönleri öder. Gerçek değerler PIT spread'den gelir.

#### Break-even funding (yıllık; hedge'li notional üzerinde)

`f* = RT × 365 / H_gün`

| Tutma süresi | Taker all-in (~1.94%) | Maker/maker (0.84%) | Spot maker + perp taker (~1.00%) |
|---|---|---|---|
| 30 gün | %23.6 | %10.2 | %12.2 |
| 60 gün | %11.8 | %5.1 | %6.1 |
| 90 gün | %7.9 | %3.4 | %4.1 |
| 180 gün | %3.9 | %1.7 | %2.0 |

**Sermaye verimi:**
- Spot tam fonlanır, perp short 1× teminatla açılır; hedge'li notional ≤ equity'nin **0.5**'i.
- Equity getirisi ≈ `0.5 × (f − RT × 365/H)`.
- Örnek: taker yolunda, 90 gün tutmada, equity'de %5/yıl net için `f ≈ %17.9/yıl` gerekir.
- Dev döneminde pozitif funding tarafı ~%1.5–2.7/yıl gerçekleşti (§1.9).

#### Kurallar (freeze)

- **Evren:** 20 varlıktan Kraken spot USD paritesi olanlar. Spot 24 saatlik hacim ≥ 1M$ (tarihsel: spot bar'ları; forward: spot_ticker).
- **Giriş:** Her gün 00:00 UTC. Varlık tutulmuyor ve cooldown'da değil, `F̂_i(t, 720h) > 0` ve `F̂_i·720 ≥ m × RT_i`. `RT_i`, `t`'de bilinen PRIMARY maliyetlerle all-in round trip. **m ∈ {1.5, 2.5}** iki varyant. En yüksek `F̂` önce; en fazla 3 varlık.
- **Boyut:** Varlık başına hedge'li notional ≤ Equity/6 (toplam ≤ 0.5). Spot ve perp qty eşit; perp teminatı = notional (1×).
- **Çıkış** (iki bacak birlikte), şunlardan biri olunca:
  - her gün 00:00 UTC'de `F̂_i(t, 720h)·720 < RT_i/2`;
  - birleşik stop (§3.0);
  - fiyatın girişten ±%40 hareketi (hedge reset);
  - R4 kill;
  - ineligibility.
- **Tarihsel PRIMARY execution:** İki bacak taker, §2 maliyetleri, t + 15 dakika fill.

#### Sensitivity satırları (her satırın rolü freeze'de sabit)

| Satır | Tanım | Rol |
|---|---|---|
| S-MAKER-BOUND | PRIMARY ile **aynı kararlar**; fill fiyatı t + 15 dakikadaki mid; fee'ler maker/maker (0.84%); spread ve slip 0; %100 fill | **Yalnız NO-GO zorlar** (iyimser üst sınır); asla ilerletmez ya da sınıf yükseltmez |
| S-COST2X | Bütün fee'ler, spot spread varsayımı, perp spread ve slip, iki bacakta ×2 | Karar taşır (P7) |
| S-PIT10 | Perp spread snapshot'ının bilinme zamanı T+5 dakika yerine T+10 dakika (V5 anlamı). Funding known-time zaten kesin | Karar taşır (P11) |
| S-FB-CONS | Perp spread fallback'leri, değerlendirme başlangıcından önceki 35 günün varlık başına p90'ı ile | Karar taşır (P11) |
| S-MIXED | Spot maker + perp taker | Bilgi |
| S-SPOTSPREAD2X | Spot spread varsayımı ×2 | Bilgi |
| S-FEE-ACTUAL | Hesaba özel fee'ler (doğrulanırsa) | Bilgi |

H3 için de S-COST2X, S-PIT10 ve S-FB-CONS aynı tanımlarla karar taşır.

#### Sınıflar

| Ortam | Mümkün sonuçlar |
|---|---|
| Holdout | **ADVANCE / INCONCLUSIVE / NO-GO** (MODEL INVALID, INSUFFICIENT dahil). **FINAL GO yok:** tarihsel spot spread/orderbook ve maker-fill geçmişi yok |
| Forward | FINAL GO ancak taze forward dönemde, gözlenmiş spot bid/ask ve depth ile, aşağıdaki execution modeliyle |

- **H1'in bütün kontrolleri ve `p_GO`, PRIMARY (taker) üzerinden hesaplanır.**
- **S-MAKER-BOUND'da P1 ya da P3 ≤ 0 ise → NO-GO (kapanır).** Maker'la bile kârlı olamıyor demektir.
- **PRIMARY'de P1 ya da P3 ≤ 0 ama S-MAKER-BOUND'da pozitifse → NO-GO-PRIMARY.**
  - H1 forward'a **confirmation adayı olarak geçemez.**
  - Yalnız **forward feasibility ölçümü** olarak çalışabilir (aşağıdaki tablo). Ölçümün tek sonucu FEASIBILITY ESTABLISHED ya da NOT ESTABLISHED'dır; bu bir kâr iddiası değildir.

#### H1 forward feasibility ölçümü (yalnız NO-GO-PRIMARY config'leri için; freeze)

| Öğe | Tanım |
|---|---|
| Pencere | Forward'ın ilk **90 günü** (gün 1–90); tek değerlendirme gün 90'da |
| Execution | §3.3 forward modeli (maker-first, 60 dk taker fallback, gözlenmiş spot ve perp book) |
| M-a maker fill oranı | Spot notional'ının maker olarak dolan payı |
| M-b gerçekleşen all-in round trip | Ölçülen fee + spread + slip + bacaklar arası gecikme P&L'i; bütün episode'lar üzerinden notional ağırlıklı ortalama. Gün 90'da açık episode'ların çıkış bacağı, gün 90'daki **varsayımsal taker unwind**: zaman damgası ≤ gün 90 olan en son spot_depth ve fut_book snapshot'larında seviyeler yürünür, taker fee'lerle |
| M-c ekonomik nokta | H1 PRIMARY kurallarının (aynı giriş/çıkış) ölçülen execution altında P1 ve P3 nokta değerleri |
| **ESTABLISHED** | M-a ≥ %50 **ve** M-b ≤ %1.25 (karışık yol ~%1.00 ile taker all-in ~%1.94 arasında; pratikte ~%70+ maker payı gerektirir, M-a'dan sıkı) **ve** M-c'de P1 > 0 ve P3 > 0 **ve** ≥ 3 kapanmış ya da açık episode |
| Aksi halde | NOT ESTABLISHED → H1 kapanır |
| Takip | ESTABLISHED ise config, **gün 91–365** verisi üzerinde (ölçüm penceresiyle örtüşmez) forward confirmation testine girer. Tek bakış gün 365; ara bakış yok; bütün P-kriterleri ve kanıt gate'i yalnız bu pencerede |
| Aile büyüklüğü | Forward Holm ailesi **forward başlangıcında** sabitlenir: holdout'tan gelen confirmation adayları + bütün NO-GO-PRIMARY H1 config'leri. Hiç test edilmeyen slot (NOT ESTABLISHED) `p = 1` sayılır (muhafazakâr) |

Gün 1–90 verisi H1 için asla confirmation'da kullanılmaz.
- PRIMARY'de P1 ve P3 > 0 ise H1, merdivenin (§6.3) normal sınıfını alır.

#### Forward execution modeli (H1; freeze)

| Durum | Kural |
|---|---|
| Spot giriş (alış) | Karar anındaki son spot_ticker'ın (≤ t) best bid'ine limit emir, hedef qty |
| Maker fill | `[t, t + 60 dk]` içindeki bir sonraki spot_ticker snapshot'ında best ask ≤ emir fiyatı **ve** zaman damgası o spot_ticker zamanına eşit ya da ondan önceki en son spot_depth snapshot'ında o fiyat ve altındaki ask hacmi ≥ qty ise tamamı emir fiyatından dolar. Ask ≤ fiyat ama hacim yetersizse görünen hacim kadar kısmi fill |
| Taker fallback | `t + 60 dk`'da kalan qty için, o anki (≤ t + 60 dk) son spot_depth snapshot'ında ask seviyeleri yürünür; VWAP + taker fee. Derinlik yetmezse kalan qty iptal edilir ve perp bacağı dolan qty'ye göre açılır |
| Perp hedge | Spot fill tamamlandıktan sonraki **ilk** fut_tickers/fut_book snapshot'ında taker short; fut_book bid seviyeleri yürünür; qty = dolan spot qty |
| Çıkış | Simetrik: spot satış best ask'e maker (fill koşulu best bid ≥ fiyat), 60 dk sonra taker fallback; perp kapanışı spot fill'inden sonraki ilk snapshot'ta |
| Bacaklar arası gecikme | Spot ve perp fill'leri arasındaki (≤ ~60 dk) hedge'siz süre P&L'i episode'a yazılır ve birleşik stop'a sayılır |
| Collector boşluğu | 60 dakikalık pencerede snapshot yoksa maker dolmamış sayılır; taker fallback ilk mevcut snapshot'ta. Karar anında 6 saatten uzun boşluk varsa giriş kararı atlanır. Çıkışlar ilk mevcut snapshot'ta taker |

H3 forward execution'ı V5 paper broker semantiğidir: taker, karar sonrası ilk snapshot'ta fut_book yürünerek.

---

## 4. Mimari değişiklikler (V5 koduna dokunmadan, yeni modüller)

| Öncelik | Modül | İçerik |
|---|---|---|
| 1 | `forecast` | H2a / H2b, PIT fit, günlük yenileme, gate metrikleri; known-time ve etiket/feature ayrımı testleri |
| 2 | `riskmodel` | EWMA vol, 60 günlük LW constant-correlation kovaryans, beta |
| 2 | `book` | §3.2'deki deterministik kitap |
| 2 | Core yolu | Yalnız V6 H3 için açık ve testli R10 istisnası (boyut bandı büyütmesi). V5 paper semantiği değişmez |
| 3 | Spot bacak | Spot enstrüman, fee, spread varsayımı, envanter muhasebesi, birleşik stop, hedge reset, 1× teminat |
| 3 | Forward replay | Collector verisi üzerinde frozen kodla forward değerlendirme; §3.3 execution modeli; coverage |
| Her yer | Muhasebe | Carry alpha ve total alpha ayrı; varlık, episode ve cluster bazlı attribution; terminal unwind |
| Her yer | Değerlendirme | §6.3 merdiveni, §6.4 IUT + Holm, §8 kuralları, kör dev koşusu, forward maskesi, V6 outcome guard |

---

## 5. Veri gereksinimleri

| Veri | Durum | Not |
|---|---|---|
| Perp funding (saatlik) | 2025-10-06'dan beri; API daha eskisini vermiyor | Dev öncesi temiz tarihsel dönem yok |
| Perp mark / index (15 dk) | 2023'ten beri | PREM24, vol, kovaryans |
| Perp spread (5 dk) | Dev dönemi var | Holdout penceresi için indirilmesi holdout açılışının parçası |
| Spot bar'ları | 2023'ten beri | H1 tarihsel fiyat ve hacim |
| Spot spread / book geçmişi | **Yok** | H1 tarihsel FINAL GO alamaz |
| Forward: fut_tickers, fut_book, fut_funding, spot_ticker (60 sn), spot_depth (5 dk, 10 seviye) | Collector 2026-10-09'dan beri | **Boşluklar var** (ör. 2026-10-10 ~03–09 UTC, 2026-10-11 00–03 UTC) |
| Hesaba özel fee tier | Doğrulanmadı | §2 |

**Forward kuralları (freeze):**
1. **Başlangıç:** Forward değerlendirmesi, frozen V6 kodunun collector verisi üzerinde `recv_ts ≥ V6 freeze anı` ile replay'idir.
2. **Maske:** Forward P&L, holdout kararı verilene ve ardından önceden takvimlenmiş bakış gelene kadar maskelidir.
3. **Coverage:** Bir gün, 15 dakikalık snapshot'ların ≥ %90'ı mevcutsa değerlendirilebilir sayılır. Eksik zamanlarda strateji emir göndermez.
   - Pozisyonlar veri geri geldiğinde mark edilir ve o P&L verinin geri geldiği güne yazılır.
   - Değerlendirilemeyen günler günlük seride bu P&L ile (çoğu zaman 0) yer alır ve sayıları raporlanır.
   - Bir bakışta değerlendirilebilir gün oranı < %80 ise o bakış INCONCLUSIVE olur.

---

## 6. Deney tasarımı

### 6.1 Süreç sırası (sapma yok)

1. Plan rev 2.
2. Bağımsız metodolojik red-team.
3. Critical ve Major düzeltmeler.
   3b. Exact spec üzerinde ikinci bağımsız red-team ve düzeltmeler.
4. Exact V6 spec.
5. Freeze, tag ve hash.
6. Implementasyon (TDD).
7. Kör dev invariant koşuları.
8. Bağımsız implementasyon review'u.
9. Ancak bundan sonra, kullanıcının açık onayıyla, **tek** holdout açılışı.
10. Forward (freeze anından itibaren biriken veri; maskeli; takvimli bakışlar).

### 6.2 Config'ler

- **Strateji:** H3-k4, H3-k6, H1-m1.5, H1-m2.5 (4 config).
- **Tahmin:** H2a (gate), H2b (diagnostic).
- **Referanslar (hükümsüz, freeze'de sabit):**
  - "cash" (0);
  - **V5 W ve E**, V5 kodu ve frozen config'i değişmeden holdout'ta koşulur. V5'in dev'den bağımsız ilk temiz okumasıdır; V5 veya V6 için hiçbir karara girmez.

### 6.3 Karar merdiveni (her config; **bütün basamaklar değerlendirilir**)

**Nihai sınıf, tetiklenen en ağır sınıftır:**

`INVALID RUN > MODEL INVALID > NO-GO (NO-GO-PRIMARY dahil) > {MODEL INCONCLUSIVE, INSUFFICIENT, INCONCLUSIVE} > ADVANCE / FINAL GO`

Tetiklenen bütün nedenler raporlanır.

| Kontrol | Tetiklenirse |
|---|---|
| Veri bütünlüğü ya da guard hatası | INVALID RUN (R9) |
| H2a gate INVALID (config'in ufku ve `k`'sı) | MODEL INVALID |
| R4 kill > 0 ya da likidasyon > 0 | NO-GO |
| P1 ya da P3 ≤ 0 (PRIMARY) | NO-GO. H1'de: S-MAKER-BOUND da ≤ 0 ise NO-GO; değilse NO-GO-PRIMARY |
| P4, P5, P6, P7 ya da P11 başarısız | NO-GO |
| P8 / P9 değerlendirilebilir ve başarısız | NO-GO |
| P8 / P9 değerlendirilemez | INCONCLUSIVE |
| Kanıt gate'i (§8.2) sağlanmıyor | INSUFFICIENT |
| H2a gate INCONCLUSIVE | MODEL INCONCLUSIVE |
| P2 (§6.4) sağlanmıyor | INCONCLUSIVE |
| Hiçbiri tetiklenmedi | Holdout'ta **ADVANCE** (FINAL GO değil); forward'da **FINAL GO** (H1 yalnız §3.3 forward modeliyle) |

- **Holdout bir confirmation değildir;** oradan geçen config yalnız forward adayıdır.
- **Forward confirmation adayları:** Holdout'ta ADVANCE, ya da yalnız INCONCLUSIVE, INSUFFICIENT veya MODEL INCONCLUSIVE alan config'ler. Hiçbir NO-GO veya MODEL INVALID nedeni olmamalı (en fazla 4).
- **Ayrıca:** H1 NO-GO-PRIMARY yalnız feasibility ölçümü olarak forward'a geçebilir (§3.3); bu, aile dışıdır.

### 6.4 Multiplicity (exact)

1. **Günlük seriler:** Değerlendirme penceresinin bütün takvim günleri (düz günler dahil), PRIMARY:
   - `net_t / E_{t−1}`
   - `carry_t / E_{t−1}`; carry = funding − fee − spread/slip
2. `p_net = max_{b ∈ {14, 28}} p_mbb(net, b)` ve `p_carry = max_{b ∈ {14, 28}} p_mbb(carry, b)`.
   - `p_mbb`: V5 `stats.mbb` ile tek taraflı (H0: ortalama ≤ 0) moving block bootstrap p-değeri; B = 10,000, seed 20261010.
   - Bloklar V5 ile aynı (V5 `stats.BLOCKS = (28, 14)`). Funding haftalarca kalıcı olduğu için daha kısa bloklar bağımlılığı küçümser.
3. **Intersection–union:** `p_GO = max(p_net, p_carry)`. H0 = {net ortalaması ≤ 0 **veya** carry ortalaması ≤ 0} için α düzeyinde geçerli test; iki bileşen için ek düzeltme gerekmez.
4. **Holm:**
   - 4 strateji config'i üzerinde aile düzeyinde α = 0.05.
   - `p_GO`'lar küçükten büyüğe sıralanır; `j`'inci adımda `p_(j) ≤ 0.05/(4 − j + 1)` olduğu sürece reddedilir.
   - H1 config'leri holdout'ta FINAL GO alamasa da aileden çıkarılmaz (muhafazakâr).
   - Holm keyfi bağımlılık altında geçerli; blok uzunlukları üzerinden max almak ve nokta/gate kriterleriyle kesişim yalnız muhafazakârlaştırır.
5. **P2 sağlanır ancak:** (i) config Holm ile reddedilmiş **ve** (ii) düzeltmesiz LB95 > 0 hem net hem carry ortalaması için, **her iki** blok uzunluğunda.
6. **Forward:**
   - Ayrı aile, forward başlangıcında sabit: forward confirmation adayları + NO-GO-PRIMARY H1 config'leri (test edilmeyen slot `p = 1`); Holm bunlar üzerinde.
   - Bakışlar forward başlangıcından 90 / 180 / 365 gün sonra; Haybittle–Peto (ara bakışlarda α = 0.001, son bakışta α = 0.05).
   - Bakışlar arasında spec değişmez.
7. **Terminal kural:** 365. gün bakışında FINAL GO yoksa **hipotez ailesi kapanır.** H1 feasibility takibi aynı ailenin parçasıdır: tek bakışına gün 365'te ulaşır ya da aileyle birlikte kapanır; uzatma yok. H1, H2 ve H3 aynı veri üzerinde yeniden spec'lenmez. Yeni bir fikir, yeni bir plan ve yeni ve taze veri gerektirir.

### 6.5 Güç

LB95 > 0 için gereken yıllık Sharpe (blok şişmesi ~×1.2 varsayımıyla):

| Örneklem | Gereken Sharpe |
|---|---|
| 100 gün | ~3.8 |
| 180 gün | ~2.8 |
| 1 yıl | ~2.0 |
| 2 yıl | ~1.4 |

Holm ile ilk config için eşik α = 0.0125 olur ve gereksinim daha da yükselir (100 günde ~5). Holdout'ta çoğu sonuç doğal olarak INCONCLUSIVE çıkabilir; **kriterler gevşetilmez.** Gerçek karar forward'dadır ve terminal kural (§6.4.7) sonsuz döngüyü engeller.

---

## 7. Contamination ve development dönemi protokolü

| Risk | Önlem |
|---|---|
| Hipotezler dev verisinden ilham aldı | Kanıt yalnız görülmemiş veride (holdout = aday seçimi, forward = confirmation) |
| Hyperparameter ya da yöntem seçimi | §3.1: form, feature, hyperparameter, pencere, regularisation freeze'de; PIT fit serbest, seçim yasak |
| H3 / H1 sabitleri ve sensitivity rolleri | §3.2, §3.3 tabloları freeze'de; gerekçeleri risk limitleri ya da a-priori |
| Merdivenin sırası | En-ağır-sınıf kuralı; sıralamadan bağımsız |
| Dev'de P&L ile varyant seçimi | Kör dev koşuları: P&L, Sharpe, carry ve kalibrasyon sütunları maskeli; yalnız invariant'lar |
| Holdout'u veri indirirken görmek | V5 guard'ları; holdout spread indirme açılışın parçası ve onaya bağlı |
| Görülmüş forward veri (V5 paper dashboard'u) | V6 forward'ı freeze anından başlar; P&L maskeli |
| Forward execution modelinin veri görülerek seçilmesi | §3.3 forward modeli freeze'de tam tanımlı |
| Sonuç sonrası "bug fix" | R9 aynen |
| Fee tablosunun sonradan değişmesi | §2: yalnız freeze öncesi, snapshot ile |
| Forward veri boşlukları | §5 coverage kuralı |
| Sonsuz "INCONCLUSIVE → yeniden spec" döngüsü | §6.4.7 terminal kural |

**Dev'de izin verilenler:** mekanik fizibilite, veri kapsamı, PIT invariant'ları, muhasebe kimlikleri, inşa kısıtlarının sağlanması.

**Dev'de yasak olanlar:** P&L, Sharpe, carry alpha ya da kalibrasyon metriklerine bakarak seçim yapmak veya değiştirmek. (§1, V5 sonuçlarının teşhisidir; V6 için kanıt değildir.)

---

## 8. Profitability kriterleri

### 8.1 Mutlak kriterler (onaylandı; gevşetilmez)

Hepsi PRIMARY ve terminal unwind dahil hesaplanır.

| # | Kriter | Eşik |
|---|---|---|
| P1 | Toplam net getiri | > 0 |
| P2 | İstatistiksel kriter | Holm ile reddedilmiş **ve** LB95 > 0 (net ve carry, b = 14 ve 28) (§6.4) |
| P3 | Carry alpha (funding − fee − spread/slip) | Toplam > 0 |
| P4 | Yıllık Sharpe (net) | ≥ 1.0 |
| P5 | MaxDD | ≤ %10 |
| P6 | En kötü gün / ES97.5 | ≥ −%3 / ≥ −%2 |
| P7 | S-COST2X (§3.3 tanımı) | Net ≥ 0 |
| P8 | Konsantrasyon | §8.3 |
| P9 | Zaman kararlılığı | §8.4 |
| P10 | Risk olayları | Kill = 0, likidasyon = 0 |
| P11 | S-PIT10 ve S-FB-CONS (§3.3 tanımı) | P1 ve P3'ün işareti değişmez |
| P12 | Tahmin geçerliliği | H2a gate **VALID** (config'in ufku ve `k`'sı; `R ∈ [0.8, 1.25]`) |

### 8.2 Minimum kanıt gate'i

| | Tanım | H1 | H3 |
|---|---|---|---|
| E1 aktif gün | ≥ 12 saat boyunca gross ≥ %10 equity olan gün. H1'de gross = hedge'li (perp) notional | ≥ 60 | ≥ 60 |
| E2a episode | Bir varlıktaki tutma dönemi. Aynı varlıkta ≥ 7 gün aradan sonraki tutma ayrı episode | ≥ 3 | ≥ 10 |
| E2b bağımsız cluster | Girişler kronolojik sırayla açgözlü gruplanır: yeni cluster, mevcut cluster'ın ilk girişinden ≥ 7 gün sonraki ilk girişte başlar | ≥ 3 | ≥ 4 |
| E3 farklı underlying | Değerlendirmede tutulmuş farklı varlık | ≥ 3 | ≥ 6 |

**Gerekçe:**
- **E1 = 60 gün:** Kanıt sermaye gerçekten riskteyken üretilir. Yaklaşık iki aylık maruziyet, en uzun bootstrap bloğunun (28 gün) iki katından fazladır ve tek bir rejim parçasına dayanmayı önler.
- **E2b:** Kullanıcının "bağımsız giriş" şartını ölçer. H3 ilk gün 8–12 bacak açtığı için tek başına episode sayısı bağımsızlık göstermez.
- **E3 (H1) = 3:** Pozisyon sınırına eşit; P8a'nın iki varlıkla fiilen uygulanamamasını önler.

Herhangi biri sağlanmazsa sonuç **INSUFFICIENT** olur (INCONCLUSIVE sınıfı), GO değil.

### 8.3 Konsantrasyon (P8; yalnız P1 geçtiyse değerlendirilir)

`C_a` = varlık `a`'ya atfedilen bütün net P&L: funding + price/basis − maliyet. H1'de spot ve perp birlikte sayılır. `C⁺_a = max(C_a, 0)`.

| Kontrol | Kural | Ne zaman değerlendirilebilir |
|---|---|---|
| P8a tek varlık | `max_a C_a ≤ 0.5 × toplam net` | `C_a ≠ 0` olan ≥ 3 varlık varsa; yoksa INCONCLUSIVE |
| P8b leave-one-asset-out | Her `a` için `toplam net − C_a > 0` | Her zaman |
| P8c etkin katkıcı | `N_eff = (Σ C⁺_a)² / Σ (C⁺_a)²`; H1 ≥ 2, H3 ≥ 4 | Her zaman |
| P8d episode düzeyi | En iyi ⌈%20⌉ kapanmış episode'un net'i ≤ kapanmış episode'ların toplam net'inin %50'si | Yalnız ≥ 10 kapanmış episode varsa; yoksa INCONCLUSIVE (FAIL değil) |

Değerlendirilebilir bir kontrol başarısız olursa sonuç NO-GO (konsantre) olur.

### 8.4 Zaman kararlılığı (P9)

1. Değerlendirme penceresi 3 eşit takvim alt penceresine bölünür. Alt pencere P&L'i, koşu başlangıç equity'sine bölünür.
2. **Taban (her alt pencere, değerlendirilebilir olsun ya da olmasın):** Alt pencere P&L'i ≥ −%3. Sağlanmazsa NO-GO.
3. **Değerlendirilebilir:** Aktif gün ≥ max(15, gün sayısının %25'i).
4. **Geçer:** ≥ 2 değerlendirilebilir alt pencere var ve en az 2'sinde net > 0. Tam 2 değerlendirilebilir pencere varsa ikisi de pozitif olmalı.
5. **INCONCLUSIVE:** < 2 değerlendirilebilir alt pencere.
6. **NO-GO:** Değerlendirilebilir ama kural sağlanmıyor.

---

## 9. Holdout stratejisi

- **Holdout kapalı kalır.** Açılış yalnız §6.1'in 1–8. adımları tamamlandıktan sonra, açık onayla, **bir kez** yapılır.
- **Aynı ve tek koşuda:** bütün frozen config'ler, H2a/H2b metrikleri, cash ve V5 W/E referansları.
- Holdout ~100 gün (2026-07-01 → 2026-10-09 11:52 UTC). Pencere sonunda açık pozisyonlar terminal unwind maliyetiyle kapatılmış sayılır (§3.0).
  - H1'in 720 saatlik gate'i ve P2 büyük olasılıkla INCONCLUSIVE kalır.
  - H3'ün ADVANCE alması Sharpe ≈ 5 gerektirir.
- **Holdout'tan geçen config confirmation değildir;** yalnız forward adayıdır.
- **Gerçek confirmation:** Freeze sonrası taze forward veri. 365. gün bakışı terminaldir (§6.4.7).
- MODEL INVALID ya da NO-GO alan config kapanır (H1 NO-GO-PRIMARY için §3.3'teki feasibility yolu hariç). Holdout ikinci kez kullanılmaz.

---

## 10. Tahmini efor (öncelik sırasıyla)

| İş | Tahmin |
|---|---|
| Exact V6 spec (bu rev 2'den), ikinci red-team, freeze | 2–3 gün |
| H2: `forecast`, PIT fit, gate metrikleri, testler | 3 gün |
| H3: `riskmodel`, `book`, R10 istisnası, testler | 4–5 gün |
| Değerlendirme: merdiven, IUT + Holm, kanıt / konsantrasyon / zamansal kurallar, terminal unwind, kör dev koşusu, forward maskesi, guard | 3–4 gün |
| H1: spot bacak (tarihsel taker), sensitivity satırları, forward replay ve maker modeli | 6–8 gün |
| Bağımsız implementasyon review'u ve düzeltmeler | 2–3 gün |
| Holdout spread indirme ve tek koşu (onaydan sonra) | 1 gün |
| **Toplam** | **~4–5 hafta.** H2 + H3 yolu ~3 hafta; H1 arkasından |

## 11. Bağımsız metodolojik red-team

Planı yazmamış, temiz bağlamlı bağımsız bir reviewer. Yalnız okudu; dosya düzenlemedi, hiçbir şey koşmadı, holdout verisine erişmedi.

| Tur | Kapsam | Bulgular | Sonuç |
|---|---|---|---|
| 1 | Rev 2 ilk taslak; kullanıcının 11 şartı ve metodoloji | Critical 0 / Major 11 / Minor 14 | Hepsi düzeltildi (§0) |
| 2 | Doğrulama | 11 Major FIXED; M5 kısmen. Yeni: Major 1 (N1, H1 feasibility yolu) / Minor 6 | Hepsi düzeltildi |
| 3 | Kapanış kontrolü | N1–N7 FIXED. Yeni: Major 1 (açık episode'larda M-b tanımsız) / Minor 1 (parantez) | Reviewer'ın önerdiği metinle düzeltildi; aile büyüklüğü için önerdiği daha güvenli seçenek de alındı |

**Hüküm:** READY FOR EXACT SPEC. Açık Critical 0, Major 0.
- 3. turun iki düzeltmesi reviewer'ın kendi önerdiği metindir.
- Ayrı bir doğrulama turundan geçmedi; exact spec'in ikinci red-team'inde (§6.1, 3b) yeniden kontrol edilecek.
- Kullanıcının 11 şartının hepsi 2. turda yeniden doğrulandı ("holds").
