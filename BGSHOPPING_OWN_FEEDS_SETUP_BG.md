# BGShopping.net: Shopify CDN XML и JSON фийдове

## Реален резултат на 29 октомври 2026 г.

**Публичните 7 адреса на bgshopping.net вече сочат към Shopify CDN, а не към Render или GitHub Raw.** Потвърдено е с Shopify Admin API (7 от 7 URL redirects) и външен GitHub Actions тест от 2026-10-28 23:51 UTC: всички 7 завършват с **HTTP 200**, URL на `cdn.shopify.com`, правилен `application/xml` или `application/json` и без `Content-Disposition: attachment`.

| Публичен адрес | Shopify CDN файл |
|---|---|
| https://bgshopping.net/pazaruvaj.xml | bgshopping-pazaruvaj.xml |
| https://bgshopping.net/pazarovaj.xml | bgshopping-pazaruvaj.xml (наследен сгрешен адрес) |
| https://bgshopping.net/shopmania.xml | bgshopping-shopmania.xml |
| https://bgshopping.net/google-shopping.xml | bgshopping-google-shopping.xml |
| https://bgshopping.net/meta-catalog.xml | bgshopping-meta-catalog.xml |
| https://bgshopping.net/ai-products.json | bgshopping-ai-products.json |
| https://bgshopping.net/feed-status.json | bgshopping-feed-status.json |

Генерирани от публичния Shopify продуктов каталог на 2026-10-28T23:35:13Z; продуктови варианта: **12 103** в Pazaruvaj, Google, Meta и AI; **10 000** в ShopMania (лимит на адаптера). Важат само текущо налични артикули с цена EUR и изображение; няма записи по продуктите на магазина.

## Реални Shopify Files

Шестте файлови ресурса са `READY` без грешки, със съответния MIME и размер под 19 MB:

| Файл | Resource ID | Размер |
|---|---|---:|
| pazaruvaj.xml | gid://shopify/GenericFile/76944569237849 | 16 913 121 B |
| shopmania.xml | gid://shopify/GenericFile/76944571203929 | 12 917 744 B |
| google-shopping.xml | gid://shopify/GenericFile/76944571236697 | 18 002 945 B |
| meta-catalog.xml | gid://shopify/GenericFile/76944571892057 | 18 002 946 B |
| ai-products.json | gid://shopify/GenericFile/76944571924825 | 17 857 189 B |
| feed-status.json | gid://shopify/GenericFile/76944571990361 | 256 B |

## Важно: автоматичното обновяване на Shopify CDN още изисква авторизация

GitHub Actions **генерира** проверените фийдове на всеки 4 часа от актуалния публичен Shopify каталог (без Render). Но Shopify CDN съхранява копия: то **не получава автоматично** новото съдържание, докато отделната задача `scripts/sync_shopify_cdn.py` няма разрешен Admin API достъп.

Свързаното с ChatGPT приложение `Shopify ChatGPT MCP App` разполага с `write_files` и `write_online_store_navigation`, но неговите вътрешни токени не могат да се предоставят на GitHub Actions. Не извличайте или публикувайте такива токени.

На 29.10.2026 г. GitHub Actions отчетът потвърди, че **няма конфигурирани** нито `SHOPIFY_CLIENT_ID` + `SHOPIFY_CLIENT_SECRET`, нито стар валиден `SHOPIFY_FEED_TOKEN`. Затова CDN файловете са актуализирани **ръчно с разрешения Shopify конектор**, а **не непрекъснато**. След шест часа проверката за актуалност ще даде предупреждение. Работният процес за обновяване вече **отчита грешка**, а не подвеждащ успех, ако му липсва авторизация.

## Последна еднократна настройка без Opera

За изцяло автоматично обновяване Shopify Dev Dashboard приложение, инсталирано в `kynvva-gx.myshopify.com`, трябва да има одобрени scopes `write_files` и `write_online_store_navigation` (и достъп за съответното четене). За новите приложения използвайте **Dev Dashboard**, а не legacy admin-created custom apps.

1. От https://dev.shopify.com/dashboard създайте отделно приложение `BGShopping Feed Sync` или използвайте подходящо вече инсталирано приложение **без да нарушавате Euromaster AI Sync**. Публикувайте версия и я инсталирайте в същата Shopify организация.
2. В https://github.com/bgshoppingnet/saas_mining_manager/settings/secrets/actions задайте секретите `SHOPIFY_CLIENT_ID` и `SHOPIFY_CLIENT_SECRET` от Dev Dashboard. При вече съществуващ стар admin-created app е поддържан `SHOPIFY_FEED_TOKEN`. Никога не поставяйте тайните в GitHub файлове или разговор.
3. Изпълнете https://github.com/bgshoppingnet/saas_mining_manager/actions/workflows/update-feed-cdn.yml чрез `Run workflow`; процесът ще обнови шестте GenericFile ресурса, ще провери `READY` и MIME, и ще обнови точните седем пренасочвания. Без потвърден успех няма да прави частично пренасочване.

**Важно:** токени и тайни никога не се записват в хранилището. Клиентското удостоверяване работи само при подходящата Shopify организация и инсталирано приложение с одобрени права.

## Наблюдение и ограничения

- Генериране: https://github.com/bgshoppingnet/saas_mining_manager/actions/workflows/update-feed-cdn.yml (на всеки 4 часа).
- Външен тест с контрол за 6-часова актуалност: https://github.com/bgshoppingnet/saas_mining_manager/actions/workflows/verify-live-feed-links.yml (на всеки 4 часа).
- Публична дата и брой артикули: https://bgshopping.net/feed-status.json.
- Продължават съществуващите нативни Shopify канали Google & YouTube, Facebook & Instagram, Pinterest и Shop, без допълнително дублиране на feeds.
- XML адресите в Shopify са **пренасочвания** към `cdn.shopify.com`, а не тематични файлове в root на магазина.
- Render проектите не участват в работата на тези фийдове, но остават спрени в стария Render акаунт; не са изтрити.
- Shopify фийдовете все още не са гарантирано одобрени/регистрирани като източник в Pazaruvaj, ShopMania, Google Merchant или Meta Commerce Manager. Само URL HTTP 200 не удостоверява това.
- Без промяна на цени, наличности, продуктови описания, поръчки или реклами.
