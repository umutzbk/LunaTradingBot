# Luna: ara bulgular (2026-10-06)

> Bu belge **ara sonuçtur.** Walk-forward (out-of-sample) doğrulaması henüz yapılmadı. Aşağıdaki "grid" sonuçları **in-sample**: tüm dönem aynı anda görülerek hesaplandı. Bu yüzden iyimser tarafa yanlıdır.

## Kurulum
- **Borsa ve enstrüman:** Kraken Futures USD-margined perp'ler, 15 dakikalık barlar.
- **Dönem:** 2024-07-02 → 2026-10-06, yaklaşık 27 ay.
- **Evren:** başlangıç tarihinde günlük ortalama hacmi ≥ $10M ve ≥ 18 ay geçmişi olanlar: **BTC, ETH, SOL**.
- **Sermaye:** $265 (~£200). İşlem başı risk equity'nin %0.75'i; pozisyon büyüklüğü = risk ÷ stop mesafesi. En fazla 3 pozisyon, gross kaldıraç en fazla 2x.
- **Maliyetler:**
  - Taker fee %0.05
  - Half-spread: BTC/ETH 1 bps, diğerleri 3 bps
  - Slippage 2 bps
  - Bu ayarlarla BTC için gidiş-dönüş maliyeti ≈ %0.16
- **Funding:** gerçek saatlik veri, yalnızca son 12 ay. Öncesi "bilinmiyor" olarak işaretli; uydurma funding kullanılmıyor.
- **Fill modeli (kötümser):**
  - Sinyal bar t kapanışında üretilir, fill bar t+1 açılışında olur.
  - IOC limit emirler, fiyat bandı %0.3.
  - Stop'lar kapanışta değerlendirilir.
  - Ayrıca bar içi acil stop var: pozisyon zararı equity'nin %2'sine ulaşınca kapanır.
- **Doğrulama:**
  - Backtest motoru ile araştırma işlem listeleri işlem işlem aynı sonucu veriyor (1e-9 hassasiyet).
  - Lookahead testleri var.
  - Muhasebe tutuyor: kapanan pozisyonların net P&L'i hesaptaki equity değişimine eşit.

## 1) Naive baseline'lar (portföy simülasyonu, tüm risk kuralları açık)

### A. Gerçekçi koşu: %15 drawdown kill-switch açık
Dört koşunun dördü de birkaç ay içinde %15 drawdown limitine çarpıp durdu. Bu yüzden örneklem küçük.

| Strateji | İşlem | Ort. R | %90 CI | Net $ |
|---|---|---|---|---|
| combined | 66 | −0.13 | [−0.51, +0.34] | −17.7 |
| naive momentum (EMA 96/480 kesişimi, 3×ATR stop) | 23 | +0.42 | [−0.66, +1.87] | +17.3 |
| naive mean reversion (z(96) > 2) | 145 | −0.15 | [−0.29, −0.01] | −38.3 |
| naive pairs (ETH/BTC, β=1, z(288) > 2) | 97 | −0.22 | [−0.34, −0.11] | −39.9 |

### B. Edge ölçümü: kill-switch kapalı, tüm dönem

| Strateji | İşlem | Ort. R | %90 CI | PF | Max DD | Brüt $ | Fee $ | Spread+slip $ | Net $ |
|---|---|---|---|---|---|---|---|---|---|
| naive momentum | 329 | **+0.18** | [−0.18, +0.56] | 1.09 | 27% | +181 | 64 | 45 | **+70** |
| naive mean reversion | 925 | −0.19 | [−0.26, −0.13] | 0.67 | 74% | −57 | 81 | 56 | −194 |
| naive pairs | 220 | −0.19 | [−0.27, −0.11] | 0.42 | 28% | −29 | 27 | 16 | −71 |

## 2) Luna stratejileri: tüm parametre grid'i, in-sample, maliyetli işlem listeleri
- Strateji başına bağımsız işlem listeleri; portföy limitleri uygulanmadı.
- Rejim filtresi açık.
- Toplam 27.960 işlem.

| Strateji | Parametre seti | Medyan net R | En iyi | En kötü | CI alt sınırı > 0 olan | Brüt > 0 olan |
|---|---|---|---|---|---|---|
| Momentum (EMA farkı/ATR + Donchian kırılımı + trailing ATR stop) | 8 | −0.16 | −0.10 | −0.20 | **0** | 7 / 8 |
| Mean reversion (EWMA z, yalnızca RANGE rejiminde) | 8 | −0.49 | −0.34 | −0.66 | **0** | 0 / 8 |
| Pairs (ETH/BTC, SOL/ETH, SOL/BTC; rolling β) | 18 | −0.24 | −0.14 | −0.33 | **0** | 0 / 18 |

En iyi parametre setleri:
- Momentum `F48_S192_N288_k3`: n=542, net −0.10R, %90 CI [−0.24, +0.04], **brüt +0.035R**
- Mean reversion `W96_zin2.0_zout0.5`: n=1236, net −0.34R, **brüt de negatif** (−0.17R)
- Pairs `W672_zin1.5_zout0.5`: n=655, net −0.14R, **brüt de negatif** (−0.05R)

## Yorum
1. **15 dakikalık zaman diliminde, bu maliyetlerle, test edilen hiçbir kurala dayalı stratejide net edge yok.** In-sample'da bile 34 parametre setinin hiçbiri sıfırın üzerinde değil; out-of-sample sonuç bundan iyi olamaz.
2. **Momentum'da küçük bir brüt edge var** (7 setin brüt R'si pozitif). Ama bu edge, gidiş-dönüş ≈ %0.16'lık maliyetin altında kalıyor. Naive momentum (EMA kesişimi, Donchian şartı yok) tüm dönemde net +0.18R yaptı, ama güven aralığı sıfırı kesiyor.
3. **Mean reversion ve pairs brüt bazda bile zararda.** Bu kripto perp'lerde, 15 dakikalık barlarda, kısa vadeli geri dönüş fikri bu haliyle çalışmıyor.
4. **Maliyet sürücüsü işlem sıklığı.** Momentum'da brüt kârın ~%60'ını fee ve spread yiyor.
5. **Risk katmanı amacına uygun çalışıyor.** Naive stratejiler %15 drawdown kill-switch'ine birkaç ayda çarpıp durdu.

## Açık sorular ve sonraki adımlar
- Walk-forward (6 ay train / 2 ay test, ilk test penceresi ~2025-01) ve GO/NO-GO raporu hâlâ yapılacak. Beklenti: **NO-GO**.
- Olası yönler:
  - **Daha yavaş zaman dilimleri** (1 saat / 4 saat / günlük momentum): daha az işlem, daha düşük maliyet payı.
  - **Daha geniş evren:** 2025'ten itibaren likit perp sayısı 5'e çıkıyor.
  - **Maker emirlerle maliyeti düşürmek:** perp maker fee %0.02.
  - **Funding / basis carry** gibi farklı edge kaynakları.
- Spot alternatifi: spot verisi hâlâ indiriliyor. Spot'ta maliyet daha yüksek (maker %0.25), bu yüzden 15 dakikalık stratejiler için daha da zor.
