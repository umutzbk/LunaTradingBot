# Luna Research V4: Funding / Basis (tasarım taslağı)

**Durum:**
- TASLAK, **frozen değil**, kod yazılmadı.
- Kullanıcı onayından sonra donar: commit ve `research-v4-spec-frozen` tag'i.
- Donmadan önce hiçbir V4 getiri sonucu hesaplanmaz.

**Bağlam:**
- V2 kapandı, aday yok (`docs/results/research-v2-closure.md`).
- V3 sonucu NO-GO, aday yok (`docs/results/research-v3.md`).
- Veri envanteri: `docs/results/v4-data-availability.md`.
- Toplayıcı: `luna-v4-forward-collector-design.md`.

## 0. Değişmez kurallar

| # | Kural |
|---|---|
| R1 | **Holdout (2026-07-01 → 2026-10-09) kullanılmaz.** V2/V3'te aday çıkmadığı için açılmadı. V4 de onu okumaz; bütün V4 yükleyicileri `assert_before_cutoff(2026-07-01)` korumasını kullanır. İleride açılıp açılmayacağı yalnızca kullanıcının kararıdır. |
| R2 | V3'ten sonra yeni **fiyat-sinyali parametre optimizasyonu** yapılmaz: momentum, low-vol, prim-getiri sinyali için yeni grid yok. V4 hipotezleri **carry geliri** (funding) ve **basis**'e dayanır. |
| R3 | **Funding uydurulmaz.** 2025-10-06 öncesi için yalnızca açıkça etiketlenmiş `premium_proxy` kullanılır. Proxy gerçek funding'le aynı tabloda veya kolonda birleştirilmez. |
| R4 | Canlı emir yok, gerçek para yok. Perp, UK perakende yatırımcı için kısıtlı bir üründür; V4 yalnızca research ve paper kapsamındadır. |
| R5 | Kabul kuralları gevşetilmez: V2 §3.5 kriter 1–7 aynen geçerli, kriter 8 V3'teki gibi (null baseline'ı günlük Sharpe'ta geçmek). Bunlar V4 frozen olmadan önce sabitlenir. |
| R6 | Deneme sayımı sürer. Mevcut kümülatif sayı: V1 208, V2 perp 184 + spot 88, V3 10 konfigürasyon / 124 grid noktası. V4'ün önceden kaydedilmiş deneme bütçesi: **en fazla 16 konfigürasyon** (§3). |
| R7 | **Tarihsel sonuçlar keşiftir.** Tek GO kanıtı, toplayıcının frozen tarihten sonra kaydettiği **forward** veridir (§4, Stage 2). |

## 1. Neden funding/basis?

V1–V3 bulgusu: **fiyat sinyallerinde** maliyetten sonra ayırt edilebilir bir alpha yok.

Funding farklı bir getiri kaynağı:
- Kaldıraçlı talebin yapısal primini öder: long'lar, kaldıraçlı maruziyet için short'lara faiz benzeri bir ücret öder.
- Bu gelir yön tahmini gerektirmez. Yönsüz (hedge'li) pozisyonla toplanabilir.
- Literatürde de gözleniyor: kripto perp'lerde funding ve basis, kalıcı ama zamanla değişen bir carry primidir.

**Riskleri:**
- Funding'in tersine dönmesi.
- Spot ücretleri: SC-2'de 0,40/0,80, gidiş-dönüş taker yaklaşık %1,6.
- İki bacak arasında basis şoku.
- Likidasyon. Paper'da da modellenir.

## 2. Hipotezler

Her hipotez beş şeyle birlikte kaydedilir: ekonomik gerekçe, ölçülebilir iddia, null baseline, maliyet modeli ve öldürme kriteri.

### H1: Perp–spot basis / cash-and-carry (delta-nötr)

- **Gerekçe:** Funding pozitifken spot long ve perp short pozisyonu funding'i toplar. Basis daralırsa ek kazanç olur.
- **İddia:** Önceden belirlenen bir giriş eşiğinin üstünde, maliyet sonrası carry geliri pozitiftir:
  - **Gelir:** gerçek funding + basis değişimi.
  - **Maliyet:** spot 0,40/0,80, perp 0,02/0,05 ve spread.
- **Kurallar:**
  - Giriş: son 7 günün ortalama funding'i yıllık %Y'nin üstünde olunca, Y ∈ {10, 20}.
  - Çıkış: 7 günlük ortalama ≤ 0 olunca, ya da en geç 90 gün sonra.
  - Asgari tutma süresi yoktur; maliyet zaten bunu cezalandırır.
  - Evren: perp ve spot'ta likit olan varlıklar.
- **Kısıtlar:**
  - Spot short açılamaz, bu yüzden **ters carry** (funding negatifken) **uygulanamaz**. Yalnızca pozitif funding tarafı test edilir.
  - Mevcut paper hesapları ayrı (perp $265, spot $265). Carry iki bacaklıdır, bu yüzden V4'te birleşik bir **sanal** carry hesabı modellenir. Paper bot'lar değişmez.
- **Null:**
  - (a) Her zaman açık carry: eşik yok.
  - (b) Rastgele giriş zamanları: aynı sayıda giriş ve aynı tutma süreleri, 5 seed.
- **Öldürme kriteri:** Maliyet sonrası ortalama getiri, gerçek funding penceresinde **ve** proxy penceresinde 2× maliyetle ≤ 0 çıkarsa H1 Stage 2'ye geçmez.
- **Deneme:** 2 konfigürasyon.

### H2: Funding-rate carry (yalnızca perp, cross-sectional)

- **Gerekçe:** Yüksek funding ödeyen perp'leri short, düşük veya negatif funding'li perp'leri long tutmak, piyasa-nötr bir funding farkı geliri üretir.
- **Kurallar:** Trailing funding penceresi L ∈ {7, 30} gün, her bacakta k ∈ {1, 2} varlık, haftalık rebalance, V2 portföy risk motoru ve SC-1 emir limitleri.
- **V3 A'dan farkı:** V3 A, primi fiyat-getirisi tahmincisi olarak test etti. H2 ise **funding nakit akışını** P&L'e açıkça yazar ve hedefi carry gelirinin net değeridir. Bilgi örtüşmesi raporda beyan edilir.
- **Null:** Funding sıralaması karıştırılmış (`shuffle<seed>`, 5 seed); aynı k ve aynı rebalance.
- **Deneme:** 4 konfigürasyon.

### H3: Cross-sectional funding dispersion

- **Gerekçe:** Varlıklar arasında funding dağılımı genişken, H2'nin topladığı fark daha büyük olur ama kalabalık pozisyon riski de artar.
- **Test:** **Yeni bir strateji değil, H2'nin koşullu analizi.** Dispersion, trailing 30 gün varlıklar arası standart sapmadır ve train medyanının altında ya da üstünde olmak üzere iki gruba ayrılır. Test: H2 P&L'i yüksek dispersion döneminde daha mı iyi? Bölme önceden kaydedilir, grid yoktur.
- **Null:** Dispersion etiketinin zamanda bloklu permütasyonu.
- **Deneme:** 1 konfigürasyon.

### H4: Funding mean reversion / persistence

- **Gerekçe:**
  - Carry ancak funding **kalıcıysa** toplanabilir.
  - Aşırı funding ise kalabalık pozisyonun çözülmesiyle **geri dönebilir**.
- **Testler:**
  - (a) **Persistence:** Funding'in kendi geleceğini tahmin etmesi; getiri değil. Örnek: f(t, son 7 gün) ile f(t, sonraki 7 gün) arasındaki rank korelasyonu ve yarılanma süresi. Bu tanımlayıcı bir test ve H1/H2'nin ön koşulu; deneme sayılmaz ama raporlanır.
  - (b) **Aşırı funding sonrası perp getirisi:** Funding üst ve alt desil, sonraki 7 günün delta-ham perp getirisi. **Önceki zayıflatıcı bulgu:** V3 A'da benzer bilgi NO-GO çıktı.
- **Null:** Funding serisinin zamanda bloklu kaydırılması (+30 gün).
- **Deneme:** 1 konfigürasyon (b).

### H5: Funding × basis etkileşimi

- **Gerekçe:** Venue basis (Kraken perp − Kraken spot) ile funding'in birlikte yüksek olması, carry girişi için daha iyi koşul olabilir: basis daralma kazancı + funding geliri. Uyumsuzluk ise venue'ya özgü bir dislokasyon olabilir.
- **Test:** H1 girişleri için 2×2 (funding yüksek/düşük × basis geniş/dar). Eşikler train medyanından alınır, grid yoktur.
- **Veri uyarısı:** Venue basis tarihsel olarak yalnızca 15m trade kapanışlarından ölçülebiliyor, bu yüzden gürültülü. Güvenilir ölçüm yalnızca forward toplayıcıdaki eşzamanlı mid'lerle mümkün. H5'in tarihsel sonucu **yalnızca bilgi amaçlıdır**.
- **Deneme:** 1 konfigürasyon.

**Toplam önceden kaydedilen deneme:** 2 + 4 + 1 + 1 + 1 = **9**. Bütçe 16'dır; fark yalnızca Stage 1'de önceden tanımlı bir robustness varyantı için kullanılabilir.

## 3. Veri ve pencereler

| Pencere | Tarih | Kullanım | Kanıt değeri |
|---|---|---|---|
| Proxy-tarihsel | 2023-01 (OI 2023-03, spread 2023-05) → 2026-07-01 | H1/H2/H5 için `premium_proxy`. Gerçek funding yerine geçmez. | Keşif. V3 A kontaminasyonu beyan edilir. |
| Gerçek-funding-tarihsel | 2025-10-06 → 2026-07-01 (268 gün) | Bütün hipotezler, gerçek funding ile | Keşif. Tek OOS penceresi; istatistiksel güç düşük. |
| Holdout | 2026-07-01 → 2026-10-09 | **Kullanılmaz** (R1) | — |
| **Forward** | spec freeze tarihi → en az +6 ay | Stage 2 değerlendirmesi | **Tek GO kanıtı** |

**Walk-forward düzeni:**
- Proxy penceresi: V2 düzeni, 6 ay train / 2 ay test.
- Gerçek-funding penceresi: kısa olduğu için tek bölme, 6 ay train / 2,8 ay test, ve tek pencere olarak raporlanır. Bu, V2 düzeninden bir sapma değil; veri kısıtı. Spec'te sabitlenir.

## 4. Aşamalar

- **Stage 0, veri doğrulama (getiri yok):**
  - `backfill-v4` ve toplayıcı devreye alınır.
  - Boşluk raporu çıkarılır.
  - Proxy ile gerçek funding örtüşmesi tüm varlıklarda, yalnızca development penceresinde ölçülür.
  - Funding semantiği birim testlerle doğrulanır: `relativeFundingRate` saatlik kesir, işaret ve settle zamanı.
- **Stage 1, tarihsel keşif:** H1–H5 tek seferde çalıştırılır.
  - Raporun ilk paragrafında şunlar beyan edilir: veri yeniden kullanımı, proxy etiketi ve V3 A örtüşmesi.
  - Çıktı **GO olamaz**; yalnızca her hipotez için "Stage 2'ye geç" ya da "öldür" kararı verilebilir.
- **Stage 2, forward doğrulama:**
  - Stage 2'ye geçen hipotezler frozen konfigürasyonla forward veride **kağıt üzerinde** izlenir; günlük, sanal hesap. Paper bot'lardan ayrıdır ve ayrı bir rapor üretir.
  - **En erken değerlendirme:** freeze + 6 ay. Örneğin 2026-10 freeze edilirse 2027-04.
  - Karar V2 §3.5 kriterleri 1–7 ve kriter 8 (null Sharpe) ile verilir.
  - Ara bakışlar sayılır. Her biri ledger'a bir deneme olarak yazılır, bu yüzden en fazla aylık durum raporu ve sinyal sağlığı görülür; getiri istatistiği yalnızca final'de.

## 5. Maliyet modeli

- Spot: SC-2, maker 0,40 / taker 0,80. Perp: 0,02 / 0,05. Taker birincil, 2× maliyet sensitivity'si.
- Funding: yalnızca bilinen funding (known_only). Proxy döneminde funding nakit akışı `premium_proxy`'den türetilir ve **ayrı bir satırda** raporlanır.
- Spread:
  - Perp: analytics spread geçmişi kullanılır (2023-05'ten beri, saatlik).
  - Spot: tarihsel spread yok, bu yüzden sabit ve muhafazakâr bir varsayım konur. Bu bir **kullanıcı kararı**: 10 veya 20 bps. Forward veride gerçek spread ölçülür.
- Başabaş örneği (H1): spot taker gidiş-dönüş %1,6 + perp %0,1 ≈ %1,7. Yıllık %20 funding ile başabaş tutma süresi ≈ 31 gün. Spot ücreti carry'nin ana engeli olabilir; bunu Stage 1 açıkça ölçer.

## 6. Kodlamaya geçmeden önce kullanıcının vereceği kararlar

1. Bu taslağın onayı ve freeze'i: hipotez listesi, deneme bütçesi (9/16), pencereler.
2. Toplayıcının başlatılması: `luna-v4-forward-collector-design.md` onaylanırsa toplayıcı TDD ile yazılır ve **önce o** devreye alınır. Toplama ne kadar erken başlarsa forward kanıt o kadar erken birikir.
3. Spot spread varsayımı: 10 veya 20 bps.
4. Forward veri yedekleme hedefi: bulut klasörü, harici disk ya da başka bir yer.
5. Stage 2 asgari süresi: 6 ay ya da daha uzun.
