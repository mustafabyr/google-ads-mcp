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
| 22 | `targeting_setting` güncelleme maskesinde yaprak değildir; `FIELD_HAS_SUBFIELDS` hatası verir, `targeting_setting.target_restrictions` kullanılmalı | `GÖZLEM` | `add_audience_to_campaign` çağrısı hata verdi, istek kimliği `Kg-Q9EexhNQI7uNEZrJfLQ` (hesap 4935403315, kampanya 24214097685) | 2026-09-09 |
| 23 | Kitle ölçütleri yazıldıktan sonra kampanya ayarı güncellemesi patlarsa ölçütler hesapta yarım kalır — işlem atomik değildir | `GÖZLEM` | Aynı çağrıda 2 `USER_LIST` ölçütü oluştu, kampanya güncellemesi hata verdi; ölçütler elle geri alındı | 2026-09-09 |
| 24 | Aynı cinsiyet/yaş/konum ölçütü ikinci kez oluşturulamaz; teklif ayarı ilk seferde create, sonrasında update olmalı | `KONVANSİYON` | `CRITERION_ALREADY_EXISTS`; araç mevcut ölçütleri önce okuyup dallanıyor | — |
| 25 | Aramada çoğu kullanıcının yaşı/cinsiyeti bilinmez; `UNDETERMINED` kovaları genellikle en büyük kovadır | `KONVANSİYON` | Hesap verisiyle doğrulanacak; araç bu yüzden hariç tutma yerine teklif ayarını öneriyor | — |
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
| 35 | `metrics.*` alanları `FROM conversion_action` ile seçilemez; dönüşüm işlemi bazlı metrik `FROM campaign` + `segments.conversion_action` ile alınır | `GÖZLEM` | Hesap 644-051-8752, istek kimliği `h64MJ-y4SacXlx8KIE94Iw`: "metric is incompatible with the resource in the FROM clause" | 2026-09-09 |
| 36 | Paylaşılan negatif listeye tek istekte ~12'den fazla terim gönderilince istek reddediliyor; araç 10'luk partilere bölüyor | `GÖZLEM` | Sahada gözlendi (Faz 3 brifi, İP-5.2); istek kimliği kaydedilmedi — bir sonraki reddi kimliğiyle buraya yaz | 2026-09-18 |
| 37 | Google Ads API v25'te yalnızca-arama (call-only) reklam tipi yok: `Ad` mesajında `call_ad` alanı, kütüphanede `CallAdInfo` bulunmuyor; karşılığı arama kampanyası + RSA + CALL varlığı | `KOD` | `google/ads/googleads/v25/resources/types/ad.py` alan listesi; `client.get_type("CallAdInfo")` → "does not exist in Google Ads API v25" | 2026-09-18 |
| 38 | Dönüşüm eylemi durumu enum'unda `PAUSED` yok (`ENABLED`, `REMOVED`, `HIDDEN`); güncellemeyle `HIDDEN`/`REMOVED` reddediliyor, eylem yalnız panelden kaldırılır | `KOD` + `GÖZLEM` | `ConversionActionStatusEnum` (v25); sahada `HIDDEN`/`REMOVED` reddi (Faz 3 brifi, İP-5.5) | 2026-09-18 |
| 39 | `segments.click_type` ile filtrelenen sorguda alanın SELECT'te de olması zorunlu | `GÖZLEM` | İstek kimliği `hvpndczlKRL7nP1HcJvNrg`: "The following field must be present in SELECT clause: 'segments.click_type'" | 2026-09-18 |
| 40 | `metrics.phone_calls` / `metrics.phone_impressions` `FROM customer` ile seçilemez; kampanya düzeyinden toplanır | `GÖZLEM` | "could not support requested resources: 'CUSTOMER'" (üretim hesabı, 2026-09-18) | 2026-09-18 |
| 41 | Plan modunda yer tutucu kimlik (`9990000000000001` gibi 16 haneli) ve negatif kimlik GAQL'de kabul edilir ve 0 satır döner | `GÖZLEM` | `campaign.id = -1` ve `campaign.id = 99999999999999` sorguları üretim hesabında 0 satırla döndü | 2026-09-18 |
| 42 | Denetim: Hedef Gösterim Payı hedefi %50'nin altında, sıra kaybı %30'un üstünde ve bütçe kaybı %5'in altındaysa "strateji yarışmayı bırakmış, bütçe boşta" | `KONVANSİYON` | Sahada bir hesapta görülen durumun eşiklenmiş hâli; Google eşiği değil, müşteriye öyle sunma | — |
| 43 | Denetim: tek reklam grubu kampanya harcamasının %50'sinden fazlasını alıp 30 günde 0 birincil dönüşüm üretiyorsa uyarı | `KONVANSİYON` | Sahada görülen durumun eşiklenmiş hâli (Faz 3 brifi, İP-6) | — |
| 44 | Denetim: 12'den az başlıklı RSA "ince" sayılır (Google 15'e kadar izin verir, tavsiyesi çok başlık) | `KONVANSİYON` | Faz 3 brifi, İP-6; Google'ın dokümanındaki "15 başlığa kadar" ile uyumlu, eşik uygulama tercihi | — |
| 45 | Denetim: panelden yapılmış değişiklikler son 7 günde `change_event.client_type = 'GOOGLE_ADS_WEB_CLIENT'` ile okunur ve karar günlüğüyle (±2 gün, kaynak türü) eşleştirilir | `GÖZLEM` | Sorgu üretim hesabında doğrulandı (2026-09-18): `client_type` filtresi çalışıyor | 2026-09-18 |
| 46 | Negatif ↔ pozitif çakışması: GENİŞ negatif tüm kelimeleri içeriyorsa, SIRALI negatif kelimeler sırayla geçiyorsa, TAM negatif metin aynıysa anahtar kelimeyi engeller | `DOKÜMAN` | https://support.google.com/google-ads/answer/2453972 (negatif eşleme türleri) | 2026-09-18 |

## Kural

- Skill'e ya da araç dokümanına **yeni bir sayısal eşik** eklenirken bu kütüğe de
  satır eklenir. Kütükte olmayan eşik kullanılmaz.
- `KONVANSİYON` satırları müşteriye sunulurken kaynak olarak Google gösterilmez.
- Kontrol tarihi **12 aydan eskiyse** yeniden doğrulanır; doğrulanamazsa etiketi
  `KONVANSİYON`'a düşürülür.
- `GÖZLEM` satırlarına mümkünse istek kimliği yazılır — tekrar üretilebilir olsun.
