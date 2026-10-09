# V1 + V2 failure analysis: Luna neden edge bulamadı?

- **Tarih:** 2026-10-09 (overnight, V2 closure sonrası)
- **Girdiler:** `docs/results/baseline.md`, `interim-findings-2026-10-06.md`, `walkforward.md`, `walkforward-spot.md`
  (SC-2 öncesi, superseded), `xrp-outlier-investigation.md`, `research-v2.md` (final, kod `e0203c8` = `1cc450a`),
  V2 final `summary.json` dosyaları.
- **Kapsam:** Yalnızca development verisi (OOS 2024-09 → 2026-07). Final generalization seti (2026-07 → 2026-10)
  okunmadı. Bu belge bir teşhis belgesidir: hiçbir V1/V2 kriterini ya da sonucunu değiştirmez.

## Özet

V1 ve V2'de toplam 208 + 272 (perp 184, spot 88) grid noktası denendi. Hiçbir hesap GO almadı; final V2 sonuçlarının
ikisi de INSUFFICIENT DATA ve aday yok. Teşhis, sırasıyla:
1. **Fiyat tabanlı kural sinyallerinde, bu veride maliyet sonrası istatistiksel olarak ayırt edilebilir alpha gösterilemedi.**
   Mean reversion ve pairs brüt bazda da negatif. Momentum'un brüt edge'i küçük ve birkaç kuyruk işlemine yoğunlaşmış.
2. **Maliyet**, 15m'de ve spot'ta (SC-2) belirleyici. Spot naive momentum'da fee'ler brüt kârın %66'sını yiyor.
3. **Exit / trend yakalama zayıf.** Perp adayında medyan MFE +0.95R, ama medyan işlem −0.58R'de kapanıyor.
4. **Ranker, kalın kuyruklu stratejileri sistematik olarak reddediyor.** Ancak filtresiz sonuçlar negatif olduğu için
   bu "yanlış ret" değil, kanıt eksikliği.
5. **Beta ve küçük sermaye** ana neden değil. Perp'te BTC korelasyonu 0.03–0.15. Cap'ler adayda bağlayıcı değil.

## 1. Gözlenen sonuçlar

| Deney | Hesap | Karar | İşlem | Ort. R | Not |
|---|---|---|---|---|---|
| V1 walk-forward (15m; momentum, MR, pairs, ranker) | perp | INSUFFICIENT DATA | 24 | −0.12 | 0/11 pencere pozitif |
| V1 walk-forward | spot (eski ücret) | INSUFFICIENT DATA | 0 | n/a | ranker hiçbir hücreyi kabul etmedi |
| V2 final (TSMOM/breakout 15m–1d, xsec haftalık/günlük) | perp | INSUFFICIENT DATA | 27 | +0.27 | CI [−0.37, +0.96]; 2/11 pencere pozitif; ex-top3 −0.43 (kırılgan) |
| V2 final (SC-2 ücretleri) | spot | INSUFFICIENT DATA | 0 | n/a | 16 konfigürasyonun hiçbiri train'de kabul edilmedi |
| Naive momentum (V1, aynı OOS) | perp | (baseline) | 287 | +1.44 | ~+300R'lik tek XRP işlemi; ex-top1 +0.39, CI sıfırı kesiyor |
| Naive momentum long-only | spot (SC-2) | (baseline) | 158 | +0.98 | CI [−0.52, +3.51], DD %50 |

## 2. Hipotez bazında ayrıştırma

### 2.1 Alpha gerçekten yok mu?
- **Mean reversion ve pairs (15m):** Brüt bazda bile negatif. In-sample grid'de 26 parametre setinin hiçbirinin brüt
  R'si pozitif değil. OOS'ta da güvenilir biçimde negatif (CI tamamen < 0). **Sonuç:** bu ailelerde alpha yok.
- **Momentum:** 15m'de küçük bir brüt edge var (in-sample, 8 setin 7'sinde brüt R > 0; en iyi set brüt +0.035R).
  Naive momentum'un tüm dönemdeki brüt kârı +$181. Ancak edge, tekil kuyruk olaylarından ayrıştırılamıyor
  (bkz. 2.3). V2 bunu 1h/4h/1d'de ve cross-sectional olarak yeniden test etti: perp'te 20 konfigürasyonun yalnızca 7'si en az bir train penceresinde kabul edildi. İç içe seçilen aday
27 işlem ve +0.27R ile kırılgan (ex-top1 +0.02, ex-top3 −0.43, medyan −0.58R). Spot'ta (SC-2 ücretleri) hiçbir
konfigürasyon kabul edilmedi.
- **Rastgele seçim (random ranker):** perp −0.23R (1122 işlem), spot −0.26R; ikisinde de CI tamamen negatif.
  Sinyal üretimi tek başına değer üretmiyor.
- **Teşhis:** Kural tabanlı fiyat sinyallerinde (trend, geri dönüş, spread), maliyet sonrası istatistiksel olarak
  ayırt edilebilir bir alpha, bu veri ve bu sermaye ölçeğinde **gösterilemedi**. "Alpha kesinlikle yok" demek için
  veri yetersiz: momentum'un kuyruk getirisi gerçek olabilir ama 21 aylık OOS'ta kanıtlanamaz.

### 2.2 Maliyet / turnover mı yiyor?
- **15m perp:** Momentum'da maliyet brüt kârın ~%60'ı (baseline). In-sample'da en iyi momentum setinin brüt
  +0.035R'si, gidiş-dönüş ≈ %0.16 maliyetle net −0.10R'ye dönüyor. **Bu timeframe'de maliyet belirleyici.**
- **Spot:** SC-2 ile ücretler 0.25/0.40'tan 0.40/0.80'e çıktı (taker gidiş-dönüş %1.6). SC-2 öncesi tek bir
  günlük-xsec konfigürasyonu train'de bir kez kabul ediliyordu; SC-2 sonrası hiçbiri kabul edilmedi. Naive
  momentum'da fee brüt kârın neredeyse yarısı. **Spot'ta maliyet bariyeri bağlayıcı.**
- **Yavaş timeframe'ler (V2):** Maliyet payı düştü (perp adayında maliyet / brüt kâr %24; brüt R +0.35 → net R +0.27), ama bu kez işlem sayısı düştü ve train
  pencereleri n ≥ 30'u üretemedi. Maliyet sorunu çözülünce örneklem sorunu ortaya çıktı.
- **Teşhis:** Maliyet, 15m ve spot için birincil neden; yavaş perp stratejilerinde ikincil.

### 2.3 Ranker fat-tail stratejileri fazla mı filtreliyor?
- Naive momentum profili: kazanma oranı %17–22, medyan işlem ≈ −1R, kâr birkaç büyük işlemde
  (perp: net kârın ~%84'ü XRP'den; tek işlem +300R).
- Ranker kuralı (n ≥ 30, shrinkage, bootstrap %90 alt sınırı > 0) 6 aylık train penceresinde böyle bir dağılımı
  neredeyse hiç geçiremez: birkaç büyük kazanç train penceresine düşmezse ortalama negatif, düşerse bootstrap
  aralığı çok geniş.
- **Ama:** Filtre "yanlış" bir şeyi reddetmiyor. Naive momentum'un kendisinde de en büyük işlem çıkarılınca CI sıfırı
  kesiyor; spot'ta tam haliyle bile kesiyor. Filtresiz (random ranker) sonuçlar ise net negatif.
- **Teşhis:** Ranker, kalın kuyruklu stratejileri sistematik olarak reddediyor. Bu bir tasarım tercihi (D3). Gevşetmek
  bu veride kanıtı güçlendirmez, yalnızca kabul edilen gürültüyü artırır. Fat-tail stratejiler için farklı bir kabul
  kuralı (ör. trimmed/medyan bazlı, daha uzun train) **ayrı ve önceden kayıtlı** bir hipotez olarak test edilebilir;
  V3'ün birincil konusu değil.

### 2.4 Exit / trend capture mı kötü?
- V2 perp candidate MFE/MAE ve payoff: MFE ortalaması +2.79R, medyanı +0.95R. MAE ortalaması −0.75R, medyanı −0.86R. Payoff oranı 2.89, kazanma
oranı %33. Elde tutma süresi medyan 7.75 saat (p10 2.2 saat, p90 47.8 saat). Medyan işlem, kapanmadan önce yaklaşık
+1R lehte hareket görüyor ama −0.58R'de kapanıyor.
- V1 en kötü gün örneği (DOGE): kârlı pozisyon gün içinde kârın çoğunu geri verdi. %2'lik acil stop girişe göre
  ölçüldüğü için kâr erimesinde tetiklenmiyor (beklenen davranış).
- **Teşhis:** Exit ve trend yakalama, ölçülebilir bir zayıflık. Gerçekleşen R, ortalama MFE'nin yaklaşık %10'u. Trailing ATR
ve time-stop çıkışları lehte hareketin büyük kısmını geri veriyor. Ancak exit parametrelerini bu veride yeniden
optimize etmek, momentum grid'ini yeniden açmak demek; V3 bunu yapmaz (kullanıcı talimatı). Not edildi: gelecekte
**önceden kayıtlı** bir exit hipotezi olarak test edilebilir.

### 2.5 Bull-market beta mı momentum gibi görünüyor?
- OOS dönemi bütününde piyasa yatay: BTC al-tut −%0.8, eşit ağırlıklı evren −%0.2 (spot doc, aynı OOS).
  Momentum kârı bu yüzden "pasif beta" değil.
- Ancak kâr, birkaç güçlü yükseliş epizoduna (XRP Kasım 2024) yoğunlaşmış. Long-only spot naive momentum'un kârı
  rally'lerden gelir; bu, koşullu (trend dönemlerine bağlı) bir beta. Ölçüm: perp naive momentum'un günlük getirisinin BTC korelasyonu 0.03, perp adayınınki 0.15. Spot long-only naive
momentum'unki 0.35. Perp'te kâr beta ile açıklanmıyor; spot'ta beta payı belirgin.
- **Teşhis:** Sonuçlar koşulsuz beta ile açıklanamaz. Ama kârın dönem yoğunlaşması "rejim-koşullu beta" ile
  "momentum alpha"yı ayırmayı imkânsız kılıyor.

### 2.6 Küçük sermaye / minimum emir kısıtı mı baskın?
- Sermaye $265, işlem başı risk %0.75 ≈ $2. Cross-sectional'da 13 varlıkta, 1x gross ile bacak başına ≈ $10–20.
- Kraken minimum emir büyüklükleri ve adım yuvarlaması bazı hedefleri bloke ediyor (`below_min_notional`, dust
  write-off'ları). R hesabında cap etkisi var: cap küçültünce R şişiyor; V2 bunun için cap-adjusted R raporluyor.
  Perp adayında işlem başı planlanan risk ortalama $2.03, ortalama maruziyet %3.7, ortalama kaldıraç 0.016.
Cap-adjusted R ile planned R aynı (+0.27), yani cap'ler adayda bağlayıcı değil. Sermaye çoğu zaman atıl duruyor.
Bu, örneklemi küçültüyor ama sonucu tek başına açıklamıyor.
- **Teşhis:** Küçük sermaye ölçüm gürültüsünü ve maliyet tabanını büyütüyor, ama ana neden değil. Negatif brüt R'li
  aileler büyük sermayede de negatif kalır.

## 3. Sonuç ve V3'e etkisi

1. **Fiyat tabanlı kural sinyalleri tükendi:** trend, breakout, cross-sectional momentum, mean reversion, pairs; 15m'den
   1d'ye kadar. V3 bunların varyasyonlarını yeniden optimize etmeyecek.
2. **Maliyet tabanı yüksek** (özellikle spot). V3'te yeni hipotezlerin turnover'ı düşük olmalı (haftalık ya da daha
   yavaş), ya da maliyeti yapısal olarak telafi eden bir getiri kaynağı (carry) içermeli.
3. **Kanıt eşiği aynı kalmalı.** V3, V1/V2 kabul ve GO/NO-GO kurallarını gevşetmeyecek. Fat-tail kabul kuralı ayrı bir
   hipotez olarak not edildi, V3 kapsamı dışı.
4. **Veri kısıtı:** Funding geçmişi yalnızca 2025-10-06'dan başlıyor (development döneminde ~9 ay). Order book ve
   spread geçmişi yok. Bu yüzden funding-tabanlı ve mikro-yapı aileleri ya proxy ile (perp–spot primi, bar verisinden,
   2023'ten beri) test edilmeli ya da veri toplanana kadar ertelenmeli.
5. **Dataset uyarısı:** 2024-09 → 2026-07 development seti V1 ve V2'de defalarca incelendi. V3 sonuçları bu set
   üzerinde "keşif" niteliğindedir; final generalization kanıtı değildir.
