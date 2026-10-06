# Luna Research V2: Momentum Odaklı Araştırma · Tasarım ve Plan Taslağı

- **Tarih:** 2026-10-06
- **Durum:** TASLAK. **Uygulama yok.** P2 (Task 23–28) tamamlandıktan sonra kullanıcı onayıyla başlar.
- **Ön koşullar:** Luna Core + Crypto (alt proje #1) P0–P2 tamamlanmış olmalı. `docs/results/walkforward.md` ve `docs/results/xrp-outlier-investigation.md` okunmuş olmalı.

## 0. Neden V2?

V1 walk-forward sonucu (perp, OOS 2024-09 → 2026-07):
- Luna'nın ranker'lı tam sistemi **INSUFFICIENT DATA** verdi: 24 işlem, −0.12R.
- Mean reversion ve pairs OOS'ta güvenilir şekilde negatif.
- Tek umut verici sinyal **naive momentum** (+1.44R, CI [+0.08, +3.53]). Ama bu sonuç tek bir XRP işlemine (+300R) bağlı. O işlem çıkarılınca ortalama +0.39R ve CI sıfırı kesiyor.

V2'nin amacı:
> Momentum'da maliyet sonrası, tek işleme ya da tek varlığa bağlı olmayan, OOS'ta tekrar eden bir edge var mı? Varsa hangi timeframe, hangi evren ve hangi maliyet/emir modelinde?

## 1. Değişmez kurallar (V1'den devralınır)

- **Holdout'a dokunulmaz.** V1'in holdout'u (2026-07 → 2026-10, perp) ancak V2'de tek bir aday "GO-adayı" seçildiğinde, **bir kez** çalıştırılır. Bunun için kullanıcının açık onayı gerekir. V2 araştırması bu dönemi hiç görmez: `run_walkforward` kayıtlı holdout ile çakışan pencereleri zaten reddediyor.
- **V1 strateji parametreleri değiştirilmez.** V2 yeni strateji sınıfları ve yeni grid'ler ekler; V1 sınıfları ve sonuçları referans olarak kalır.
- **Doğrulama disiplini aynı kalır:**
  - Walk-forward: 6 ay train, 2 ay test, test pencereleri ardışık.
  - Ranker: hiyerarşik shrinkage, n ≥ 30, bootstrap alt sınırı > 0.
  - Funding: bilinmeyen funding uydurulmaz.
  - Maliyetler: taker baz, sensitivity'ler ayrı raporlanır.
  - GO/NO-GO kriterleri spec §8 ile aynı.
- **Çoklu test dürüstlüğü:** V2'de denenen her konfigürasyon (timeframe × evren × strateji × parametre) sayılır ve raporlanır. Konfigürasyon seçimi yalnızca train pencerelerinde yapılır.

## 2. Araştırma soruları ve deneyler

### 2.1 Timeframe karşılaştırması: 15m vs 1h vs 4h vs 1d
- 15m barlar 1h, 4h ve 1d'ye **causal** olarak resample edilir; yalnızca kapanmış üst-timeframe barları kullanılır. Karar ve fill her zaman üst-timeframe bar kapanışından sonraki ilk 15m bar açılışında yapılır.
- Strateji parametreleri timeframe'e göre ölçeklenir: EMA/ATR/Donchian uzunlukları **zaman cinsinden** sabit tutulur (örn. "1 gün", "5 gün"), bar sayısı cinsinden değil. Ayrıca her timeframe'in kendi küçük grid'i olur.
- Rapor: timeframe başına işlem sayısı, turnover, brüt/net R, maliyetin brüt kâr içindeki payı.

### 2.2 Strateji aileleri

| Aile | Tanım | Not |
|---|---|---|
| Time-series momentum (TSMOM) | V1 naive momentum genellemesi: EMA kesişimi / getiri işareti; ATR stop; time-stop | V1 naive = referans |
| Volatility breakout | N-bar range veya ATR kanalı kırılımı (Donchian/Keltner); volatilite sıkışması filtresi opsiyonel | Momentum'un giriş varyantı |
| Cross-sectional momentum | Evren içinde lookback getirisine göre sıralama; en güçlü k varlık long, perp'te en zayıf k short (market-neutral opsiyonu); periyodik rebalance | Spot'ta yalnızca long-only versiyonu |

Hepsi mevcut `Strategy`/`SignalFrame` arayüzünü kullanır. Cross-sectional için gereken genişleme şu: çok varlıklı sinyal ve rebalance hedefi. Bu, ranker/constructor'a "rebalance" modu ekler; tasarım detayı V2 spec onayında netleşecek.

### 2.3 Sağlamlık (robustness) testleri. Hepsi OOS sonuçlar üzerinde.
1. **Varlık leave-one-out:** her varlık sırayla evrenden çıkarılır, tüm walk-forward yeniden koşar. Hedef: hiçbir tek varlığın çıkarılması net sonucu sıfırın altına çekmemeli.
2. **En büyük kazananların çıkarılması:** top-1, top-3 ve top-%1 işlem çıkarılır, expectancy ve bootstrap CI yeniden hesaplanır. Hedef: CI alt sınırı top-1 çıkarıldıktan sonra da > 0 kalmalı (GO için ek koşul).
3. **Evren karşılaştırması:** BTC/ETH-only ile geniş evren (V1 kuralı: ADV ≥ $10M, ≥ 18 ay geçmiş) karşılaştırılır.
4. **Kuyruk analizi:** R dağılımı, en kötü %1, en uzun kayıp serisi, aylık getiri dağılımı.

### 2.4 Maliyet ve emir modeli
- **Maker vs taker:** perp maker %0.02, taker %0.05. Maker için post-only limit + "fill if traded through" modeli (spot'taki gibi) perp'e de eklenir. Dolmayan emir kaçırılan işlem demektir; bu fırsat maliyeti de raporlanır.
- **2× maliyet sensitivity:** her konfigürasyon için zorunlu (V1 kuralı).
- **Turnover ve maliyet atribüsyonu:** strateji × timeframe × varlık bazında fee, spread+slip ve funding'in brüt P&L'e oranı; işlem başı maliyet bps.

### 2.5 XRP +300R incelemesinin sonuçları
`docs/results/xrp-outlier-investigation.md`'nin bulguları V2'nin girdisidir. Bulgu "gerçek" çıkarsa iki şey yapılır:
- Kalın kuyruklu R dağılımı için ranker'ın kabul kuralına **ek bir kontrol** önerilir: trimmed-mean ve medyan raporlanır, top-1 çıkarılmış CI şartı. Bu bir **spec değişikliğidir** ve kullanıcı onayı gerekir.
- Pozisyon başına **notional üst sınırı ve kâr-alma/kademeli çıkış** sensitivity'si eklenir; ancak V1 parametreleri değişmez.

## 3. Çıktılar
- `docs/results/research-v2.md`: tüm deneylerin özet tablosu. Denenen konfigürasyon sayısı, OOS GO/NO-GO, robustness sonuçları ve maliyet atribüsyonu içerir.
- Konfigürasyon başına HTML rapor (V1 rapor altyapısı).
- **Tek bir "aday" konfigürasyon** ya da "aday yok" sonucu. Aday varsa holdout için kullanıcı onayı istenir.

## 4. Uygulama planı taslağı (task'lar; onaydan sonra writing-plans ile detaylandırılacak)

| # | Task | İçerik |
|---|---|---|
| V2-1 | Timeframe resampler | Causal 15m → 1h/4h/1d; karar/fill zamanlaması; nedensellik testleri |
| V2-2 | Zaman-ölçekli parametreler | Strateji uzunluklarını zaman cinsinden tanımlayan yardımcı; timeframe başına grid |
| V2-3 | TSMOM ailesi | V1 naive genellemesi; tradelist eşdeğerlik testi (V1 kalıbı) |
| V2-4 | Volatility breakout | Donchian/Keltner kırılımı; eşdeğerlik testi |
| V2-5 | Cross-sectional momentum | Çok varlıklı sinyal + rebalance modu (constructor/planner genişlemesi); market-neutral opsiyon |
| V2-6 | Perp maker emir modeli | SimBroker + tradelist'e post-only perp; kaçırılan işlem sayımı |
| V2-7 | Robustness motoru | Leave-one-out, top-k çıkarma, evren karşılaştırması; mevcut walk-forward üzerinde orkestrasyon |
| V2-8 | Maliyet atribüsyonu raporu | Turnover, maliyet/brüt oranı, işlem başı bps; strateji × TF × varlık |
| V2-9 | V2 rapor + karar | Özet tablo, deneme sayısı, aday seçimi (yalnızca train), GO/NO-GO; holdout yok |

**Tahmini maliyet:** 9 görev, her biri V1'deki gibi implementer + reviewer döngüsüyle. Hesaplama yükü timeframe × robustness kombinasyonlarıyla katlanır. İşlem listesi cache'i ve 1h/4h/1d'deki daha az bar sayısı bunu dengeler.

## 5. Açık sorular (V2 spec onayında karara bağlanacak)
1. Cross-sectional rebalance sıklığı: günlük mü, haftalık mı?
2. Market-neutral cross-sectional, risk modelinde nasıl temsil edilecek? "Pozisyon başına stop" kavramı yerine portföy seviyesi risk bütçesi gerekebilir.
3. Ranker'ın kalın kuyruk kuralı (2.5): spec değişikliği yapılsın mı?
4. Spot account V2'ye dahil mi? (maker %0.25 → 1h+ timeframe'lerde anlamlı olabilir)
