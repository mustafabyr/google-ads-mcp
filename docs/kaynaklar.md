# Kaynak Kütüğü

Playbook ve skill'lerde kullandığımız her sayısal eşiğin nereden geldiği.
Amaç tek bir şey: **kaynağı olmayan iddia, iddia sayılmaz.**

Google Ads kuralları yılda birkaç kez değişir. Tarihsiz bir kontrol listesi iki
yıl sonra yanlış bilgi kaynağıdır — bu dosya onu engellemek için var.

## Durum etiketleri

| Etiket | Anlamı |
|---|---|
| `DOKÜMAN` | Google'ın resmî dokümanında ya da resmî yazışmasında geçiyor |
| `GÖZLEM` | Bu hesaplarda API'nin fiilen döndürdüğü davranış; istek kimliğiyle kayıtlı |
| `KOD` | İstemci kütüphanesinin kaynağından okundu |
| `KONVANSİYON` | Sektörde yaygın kabul; Google'ın güncel dokümanıyla **doğrulanmadı** |

`KONVANSİYON` etiketli bir eşiği müşteriye "Google böyle diyor" diye sunma.
"Yaygın uygulama şu" de.

## Kütük

| # | İddia | Durum | Kaynak | Kontrol |
|---|---|---|---|---|
| 1 | Duyarlı arama reklamı: 15 başlık × 30 karakter, 4 açıklama × 90 karakter, görünen URL yolu 15 karakter | `DOKÜMAN` | Google Ads API `ResponsiveSearchAdInfo`; araç tarafında da doğrulanıyor | 2026-09-03 |
| 2 | Basic Access: günde 15.000 işlem, ücretsiz | `DOKÜMAN` | Google'dan gelen token onay e-postası, 2026-09-02 | 2026-09-02 |
| 3 | `include_in_conversions_metric` oluşturma sırasında ayarlanamaz, yalnızca güncellemeyle | `GÖZLEM` | `create_conversion_action` reddi; fork kodundaki not | 2026-09-03 |
| 4 | GOOGLE_HOSTED dönüşüm işlemleri (İşletme Profili yerel işlemleri) değiştirilemez | `GÖZLEM` | `MUTATE_NOT_ALLOWED` + `IMMUTABLE_FIELD`, istek kimliği `ND3U2e_us5QsDcb2P3NqGQ` | 2026-09-03 |
| 5 | `campaign_conversion_goal` (kategori, köken) çiftiyle adreslenir; kökeni `UNKNOWN` olan satır adreslenemez | `GÖZLEM` | `BAD_RESOURCE_ID`, istek kimliği `tQcr1lHvRxGesMaukz9_EQ` | 2026-09-03 |
| 6 | `CampaignConversionGoalService.mutate` `partial_failure` almaz; parti ya hep ya hiç | `KOD` | `google/ads/googleads/v25/services/types/campaign_conversion_goal_service.py` | 2026-09-03 |
| 7 | GAQL alan yolu satır kesemez; `UNEXPECTED_INPUT` verir | `GÖZLEM` | İstek kimliği `4CpIiMqAg2OTS9RQKvcEzQ` | 2026-09-03 |
| 8 | Çağrı uzantısı `USE_RESOURCE_LEVEL` + boş `call_conversion_action` durumunda arama hiçbir yere yazılmaz | `GÖZLEM` | Hesap 644-051-8752, asset 50888313754 | 2026-09-03 |
| 9 | `positive_geo_target_type` varsayılanı `PRESENCE_OR_INTEREST` | `GÖZLEM` | `create_search_campaign` ile açılan 24214097685 ve 24219748067 kampanyaları bu değerle okundu (hesap 4935403315) | 2026-09-06 |
| 10 | Google en az 4 site bağlantısı olmadan sitelink göstermiyor | `KONVANSİYON` | Yaygın kabul; doğrulanacak | — |
| 11 | Akıllı teklif için 30 günde ~30 dönüşüm eşiği | `KONVANSİYON` | Google zaman içinde bu rehberliği değiştirdi; **müşteriye Google kuralı diye sunma** | — |
| 12 | Öğrenme evresi 7-14 gün | `KONVANSİYON` | Kampanya tipine göre değişir; doğrulanacak | — |
| 13 | Bütçeyi tek seferde %20'den fazla değiştirmek öğrenmeyi sıfırlar | `KONVANSİYON` | Uygulama kaynaklı; Google'ın belgelediği bir eşik değil | — |
| 14 | Aynı hesaptan iki kampanya aynı açık artırmaya girmez; tam eşleme öncelik kazanır | `KONVANSİYON` | Doğrulanacak | — |
| 15 | Reklam gücü bir performans metriği değil, tamamlanmışlık göstergesi | `KONVANSİYON` | Google'ın kendi uyarısıyla örtüşür; doğrulanacak | — |
| 16 | `metrics.conversions` yalnızca `include_in_conversions_metric` açık işlemleri sayar; `metrics.all_conversions` hepsini sayar | `GÖZLEM` | Hesap 644-051-8752, 7 gün: 0'a karşı 526 | 2026-09-04 |
| 17 | `TargetImpressionShare.location_fraction_micros` bir bütünün micro'sudur: %1 = 10.000, %100 = 1.000.000 — paranın micro'su değil | `KOD` | `google/ads/googleads/v25/common/types/bidding.py`, `TargetImpressionShare` docstring: "For example, 1% equals 10,000." | 2026-09-06 |
| 18 | `TargetImpressionShare.cpc_bid_ceiling_micros` zorunlu alandır | `KOD` | Aynı dosya: "This is a required field entered by the advertiser" | 2026-09-06 |
| 19 | `campaign_criterion.bid_modifier` aralığı 0.1 – 10.0 | `KOD` | `google/ads/googleads/v25/resources/types/campaign_criterion.py`: "The modifier must be in the range: 0.1 - 10.0" | 2026-09-06 |
| 20 | API ile açılan kampanyada dil ölçütü oluşmaz; dil ölçütü yoksa tüm diller hedeflenir | `GÖZLEM` | 24214097685 ve 24219748067'de `campaign_criterion.type = LANGUAGE` sorgusu boş döndü (hesap 4935403315) | 2026-09-06 |
| 17 | `metrics.search_budget_lost_impression_share` yalnızca kampanya seviyesinde okunur | `GÖZLEM` | `AD_GROUP` FROM ile hata, istek kimliği `oFF0rp5nBD5zWK9W1s2MTg` | 2026-09-04 |
| 18 | `AdGroupAd` durumu `REMOVED`'a güncellemeyle çekilemez, ayrı `remove` işlemi gerekir | `GÖZLEM` | `INVALID_ENUM_VALUE`, istek kimliği `OBW-MzDdbztl2HYKL-iB6g` | 2026-09-04 |
| 19 | Paylaşılan negatif liste en fazla 5.000 terim tutar | `KONVANSİYON` | Google dokümanında doğrulanacak | — |
| 20 | Arama ağında yeniden pazarlama listesi için ~1.000 aktif üye eşiği | `KONVANSİYON` | Araç bunu varsaymaz; `user_list.eligible_for_search` bayrağını okur | — |
| 21 | Deney trafik bölmesi 1-99 arası olmalı | `KONVANSİYON` | Test hesabında doğrulanacak | — |
| 22 | Yüklenen çevrimdışı dönüşüm raporlarda 3 saate kadar gecikebilir | `KONVANSİYON` | Google dokümanında doğrulanacak | — |
| 23 | Günlük bütçe aylık tavan değildir; Google iyi bir günde günlüğün 2 katına kadar harcayabilir | `KONVANSİYON` | Google dokümanında doğrulanacak | — |
| 24 | `quality_info.quality_score` 0 dönmesi "puanlanmadı" demektir, "kötü" demek değil | `GÖZLEM` | Hesap 644-051-8752, trafiksiz kelimeler | 2026-09-04 |
| 25 | Konum uzantısı API ile yaratılmaz; Business Profile bağlantısından senkronlanan varlık kümesi kampanyaya bağlanır | `GÖZLEM` | Hesap 644-051-8752, `LOCATION_SYNC` kümesi 7135026640 | 2026-09-04 |
| 26 | `ConversionUploadService.UploadClickConversions` yeni entegrasyonlara kapalı; Google Data Manager API'ye yönlendiriyor | `GÖZLEM` | Hesap 644-051-8752, API mesajı: "limited to existing users" | 2026-09-04 |
| 27 | `Label` mesajında `description` alanı yok; açıklama `label.text_label.description` altında | `GÖZLEM` | v25, `Unknown field for Label: description` | 2026-09-04 |
| 28 | Gösterim payı alanlarında 0,0999 "%10'dan az", 0,9001 "%90'dan çok" demektir | `KONVANSİYON` | Hesap 644-051-8752'de dört kampanyada 0,0999 gözlendi; Google dokümanında doğrulanacak | — |
| 29 | proto-plus, Python'da çakışan alan adlarını yeniden adlandırır: mesajın `type` alanı sözlükte `type_` olur | `GÖZLEM` | Altı araçta sessiz `null`; konum varlık kümesi filtresi hiçbir şeyle eşleşmedi | 2026-09-04 |
| 30 | `campaign_asset.field_type = 'LOCATION'` GAQL'de reddedilir; adres uzantısı campaign_asset değil, Business Profile'dan senkronlanan AssetSet'tir | `GÖZLEM` | `BAD_ENUM_CONSTANT`, istek kimliği `Abcjmjin-VKKTKIMnj2IrQ` | 2026-09-04 |
| 31 | `customer_client.manager = false` filtresi alt hesapları verir; MCC'nin kendisi manager=true olduğu için listeye girmez | `DOKÜMAN` | https://developers.google.com/google-ads/api/fields/v25/customer_client | 2026-09-07 |
| 32 | `change_event` sorgusu hem alt hem üst tarih sınırı ve `LIMIT` ister; tek sınırla hata döner | `DOKÜMAN` | https://developers.google.com/google-ads/api/docs/change-event | 2026-09-07 |
| 33 | Öneriler otomatik uygulama API'den ne okunabilir ne kapatılabilir; tek kanıt `change_event.client_type` alanındaki robot istemcilerdir | `GÖZLEM` | Hesap 644-051-8752'de bir ayda 5 negatif kelime silinmesi | 2026-09-07 |
| 34 | Dönüşümün teklife girip girmediğine eylemin `include_in_conversions_metric` alanı değil, hesabın (kategori, köken) anahtarlı dönüşüm hedefi karar verir | `GÖZLEM` | Hesap 493-540-3315: WhatsApp eylemi bayrak false, 1791 dönüşüm sayıldı; hesap toplamı 1931 = 1791 + 140 | 2026-09-07 |

## Kural

- Skill'e ya da araç dokümanına **yeni bir sayısal eşik** eklenirken bu kütüğe de
  satır eklenir. Kütükte olmayan eşik kullanılmaz.
- `KONVANSİYON` satırları müşteriye sunulurken kaynak olarak Google gösterilmez.
- Kontrol tarihi **12 aydan eskiyse** yeniden doğrulanır; doğrulanamazsa etiketi
  `KONVANSİYON`'a düşürülür.
- `GÖZLEM` satırlarına mümkünse istek kimliği yazılır — tekrar üretilebilir olsun.
