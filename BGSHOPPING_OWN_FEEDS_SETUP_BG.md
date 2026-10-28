# BGShopping.net: собствен каталог за Google, Meta, Pazaruvaj и AI

Източник на продуктовите данни: **Shopify Admin GraphQL** (не са фийдове на доставчици).

## Подготвени публични маршрути

Планиран основен сървър: `https://bgshopping-pazaruvaj-feed.onrender.com`

| Канал | Път | Формат |
|---|---|---|
| Google Merchant Center | `/google-shopping.xml` | RSS 2.0, Google `g:` namespace |
| Facebook / Instagram (Meta Commerce Manager) | `/meta-catalog.xml` | RSS 2.0, Google `g:` namespace |
| Pazaruvaj | `/pazaruvaj.xml` | XML според наличния адаптер |
| Индексиране и AI | `/ai-products.json` | JSON-LD |
| Наблюдение | `/feed-status` | JSON |

## Правила

- Собствен източник: текущите активни и публикувани продукти от Shopify.
- Продажна цена от Shopify в **EUR**, без изкуствено намаляване и без смяна на цените в магазина.
- Само артикули с положителна цена, име и поне една снимка.
- Google: наличността се описва като `in_stock` или `out_of_stock`.
- Meta: само варианти с `availableForSale=true`.
- Идентификатори за Google и Meta: `shopify-variant-<variant_id>`, стабилни и уникални.
- Баркодове и снимки на варианти се четат от актуалните GraphQL полета `barcodes` и `media`.
- Опресняване: `REFRESH_SECONDS=14400` (4 часа) **само когато процесът работи**.
- Съществуващата синхронизация към Shopify остава изключена без изричните флагове за разрешение на запис; фийдовете само четат.
- Не се пипат поръчки, продукти, складове или реклама.

## Проверка

Първо потвърдете, че `/health` дава HTTP 200 и `/feed-status` показва `google_ready=true`, `meta_ready=true`, `pazaruvaj_ready=true`, `last_error=null`. След това XML трябва да е с HTTP 200 и `Content-Type: application/xml`, RSS корен, артикули и реални снимки/цени.

След включване на работещ хостинг:
1. В **Google Merchant Center → Data sources** добавете `/google-shopping.xml` като основен планиран източник за България, EUR. Не добавяйте втори паралелен източник със същите артикули, докато Google & YouTube вече ги поддържа.
2. В **Meta Commerce Manager → Catalog → Data sources** добавете `/meta-catalog.xml` като scheduled feed за правилния каталог. Ако Shopify Facebook & Instagram вече синхронизира същите артикули, преминете към един източник контролирано, без създаване на дубликати.
3. Проверете **Diagnostics**, действителните публикувани артикули и часовата отметка на последното обновяване.

## Блокиращо условие към 29.10.2026 г.

Render услугите `bgshopping-pazaruvaj-feed` и `bgshopping-feed-cdn` са **suspended (billing)**. Публичните фийдове НЕ са потвърдени като работещи. Измененията са в GitHub `bgshoppingnet/saas_mining_manager`, клон `main`, но това не разрешава billing спирането. Няма разрешение за включване на платени услуги.

Администраторски токени, подписани експорти и секрети **не трябва** да се добавят в GitHub. Няма разрешение за автоматично активиране на платени кампании.
