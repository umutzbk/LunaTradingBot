# Research V3: clarification 1 (pre-results)

- **Tarih:** 2026-10-09 04:21 UK. Hiçbir V3 research koşusu başlamadan ve hiçbir V3 sonucu görülmeden yazıldı.
- **Kapsam:** Spec §3A ve §10'un uygulamadaki karşılığını netleştirir. Kriter, grid ya da parametre değiştirmez.
- **Kaynak:** Freeze sonrası bağımsız takip review'ı (kod `9c70bbb`, verdict "ready to run").

## Premium'un gün bazında tanımı

Spec §10, işlemsiz günleri NaN sayar. Uygulama bundan biraz daha sıkıdır: günlük prim, karar barındaki (23:45 UTC)
perp ve spot kapanışlarından hesaplanır. **İki venue'dan birinde o bar ileri doldurulmuşsa (işlem yoksa) o günün
primi NaN'dır** (`luna/v3/data.py`, `luna/v3/signals.py`). Gerekçe: prim, aynı anda gözlenmiş iki fiyat ister.
Bayat (ileri doldurulmuş) bir kapanış, sahte bir prim üretir.

Review'ın ölçtüğü etki (yalnızca 23:45 barlarının doluluk oranı; getiri verisine bakılmadı):

- MUBARAK spot'un 23:45 barı günlerin %22'sinde gerçek. Bu yüzden pratikte premium ailesinin dışında kalır.
- ENA ve ZEC %85–87.
- Diğer varlıklar %94–100.

## Rapor yerleşimi (spec E2, §6/§9)

Dataset-reuse uyarısı HTML raporunun ilk paragrafına konur. Kümülatif deneme sayıları (V1 208; V2 perp 184 / spot 88;
V3) HTML'de de gösterilir. Bunlar yalnızca raporlama değişiklikleridir.
