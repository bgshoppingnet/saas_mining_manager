# BGShopping.net: работещи XML и JSON фийдове без Render

## Текущо състояние (29 октомври 2026)

Всички адреси по-долу са **възстановени като Shopify URL пренасочвания** към реални файлове на публичния GitHub `feed-cdn` клон.

| URL на BGShopping | Изходен файл |
|---|---|
| https://bgshopping.net/pazaruvaj.xml | feeds/pazaruvaj.xml |
| https://bgshopping.net/pazarovaj.xml | feeds/pazaruvaj.xml (стар сгрешен URL) |
| https://bgshopping.net/shopmania.xml | feeds/shopmania.xml |
| https://bgshopping.net/google-shopping.xml | feeds/google-shopping.xml |
| https://bgshopping.net/meta-catalog.xml | feeds/meta-catalog.xml |
| https://bgshopping.net/ai-products.json | feeds/ai-products.json |
| https://bgshopping.net/feed-status.json | feeds/feed-status.json |

Външна проверка през GitHub Actions `Verify Live BGShopping Feed Links` потвърди **HTTP 200 и правилна GitHub цел за всички 7 URL** на 29 октомври 2026 г. Последният валидиран каталог е генериран в 2026-10-28T23:06:26Z (01:06:26 ч. българско време), със следните резултати:

- **12 103 налични продуктови варианта** с реална EUR цена и снимка
- Pazaruvaj: 12 103 XML записа
- Google Shopping: 12 103 XML записа
- AI JSON: 12 103 JSON-LD продукта
- Meta XML: 12 103 записа
- ShopMania: 10 000 записа (ограничение в съществуващия адаптер)
- Shopify има общо 24 156 продукта, но фийдовете умишлено не включват неналични артикули или артикули без реална цена/снимка.

## Автоматично обновяване

GitHub Actions работен процес `.github/workflows/update-feed-cdn.yml` на `main`:

- Стартира по график `17 */4 * * *` (веднъж на 4 часа), както и ръчно.
- Изтегля директно актуалния публичен продуктов JSON от `https://bgshopping.net/products.json`, с пълна страницация и проверки за цялост.
- Генерира файловете в публичния клон `feed-cdn` с `scripts/snapshot_public_shopify.py`, `scripts/rebuild_channel_feeds.py` и `scripts/build_meta_catalog.py`.
- Проверява XML парсването, актуалността, минималния брой артикули и наличието на изображения, преди публикуване.
- Ако Shopify е недостъпен или изтеглянето е непълно, **не подменя** последния проверен каталог.
- Не използва Render, Shopify Admin токен, платена услуга или запис по продуктите.

Графикът на GitHub Actions е с възможни закъснения. Действителната актуалност винаги се вижда в `/feed-status.json` и `feeds/last-updated.txt`.

## Нативните Shopify канали остават отделни

Google & YouTube, Facebook & Instagram, Pinterest и Shop продължават да получават продукти по съществуващата директна интеграция на Shopify. Тези XML адреси **не се добавят като втори активен източник автоматично** и не гарантират одобрение от външна платформа. Не добавяйте дублиращи фийдове за същите артикули, без да проверите настройките на съответния Merchant/Commerce акаунт.

## Render

Двата стари Render проекта са спрени. Shopify **няма пренасочвания към Render**. Старият `refresh-feeds-after-generator-update.yml`, който зависеше от Render, е изтрит от `main`. Наличният Render конектор не предлага изтриване на услуги; окончателно изтриване от Render акаунта трябва да се направи там, ако е необходимо.

## Проверка

- Публичен URL тест: `https://github.com/bgshoppingnet/saas_mining_manager/actions/workflows/verify-live-feed-links.yml`
- Генериране на фийдовете: `https://github.com/bgshoppingnet/saas_mining_manager/actions/workflows/update-feed-cdn.yml`
- Публичен статус: `https://bgshopping.net/feed-status.json`

Без платени услуги, без промяна на цени, складови наличности, поръчки или реклами.


## Shopify Files CDN: преминаване без Render (29 октомври 2026 г.)

**Проверено с реалните продукти:** всичките шест фийда вече са качени и READY в Shopify Files. Независим GitHub Actions тест потвърди HTTP 200, `application/xml` за XML и `application/json` за JSON, без `Content-Disposition: attachment`.

| Файл | Shopify CDN име | Размер |
|---|---|---:|
| Pazaruvaj XML | bgshopping-pazaruvaj.xml | 16 913 121 B |
| ShopMania XML | bgshopping-shopmania.xml | 12 917 744 B |
| Google Shopping XML | bgshopping-google-shopping.xml | 18 002 945 B |
| Meta XML | bgshopping-meta-catalog.xml | 18 002 946 B |
| AI JSON | bgshopping-ai-products.json | 17 857 189 B |
| Feed status JSON | bgshopping-feed-status.json | 256 B |

Фийдовете са оптимизирани под лимита 20 MB на Shopify Files. **Същите 12 103 налични варианта**, цени и идентификатори се запазват. Съкратен е единствено текстът на описанията в експортните файлове; продуктите и описанията в Shopify не са променяни.

**Важно:** докато не е настроена автоматичната авторизация за запис в Shopify, действащите адреси `bgshopping.net/*.xml` и `/ai-products.json` остават с предишния работещ GitHub източник, защото Shopify CDN файлът е статичен след качване и би остарявал. Не пренасочвайте към статичните Shopify копия без активна синхронизация.

### Веднъж настроен безопасен автоматичен sync

**Препоръчителен метод за ново приложение през 2026 г.:** Shopify Dev Dashboard (`https://dev.shopify.com/dashboard`) → Apps → създаване на приложение → API access scopes `write_files` и `write_online_store_navigation` (съответните права за четене са включени) → Release → Install за магазина `kynvva-gx.myshopify.com`. Приложението и магазинът трябва да са в **една Shopify организация**, за да работи client-credentials grant. В Dev Dashboard → App → Settings вземете Client ID и Client secret; запишете ги **само** като GitHub Actions repository secrets `SHOPIFY_CLIENT_ID` и `SHOPIFY_CLIENT_SECRET` през `https://github.com/bgshoppingnet/saas_mining_manager/settings/secrets/actions`. Никога не ги поставяйте в чат, файлове, git комити или публични URL адреси. Софтуерът извлича краткоживеещ Admin API токен при изпълнение; няма нужда ръчно да се сменя на 24 ч. Ако има вече инсталирано старо admin-created приложение с валиден offline токен, поддържа се и `SHOPIFY_FEED_TOKEN`, но **нови admin-created custom apps вече не могат да се създават**.

Външна документация: https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant и https://shopify.dev/docs/apps/build/authentication-authorization/legacy/admin-custom-apps.

Тогава работният процес `.github/workflows/update-feed-cdn.yml` автоматично:

1. Извлича каталога директно от Shopify и проверява всички фийдове.
2. Обновява същите шест Shopify Files чрез `scripts/sync_shopify_cdn.py` и `fileUpdate`, без да създава дубликати.
3. Проверява `READY`, размер и MIME на всички файлове преди промяна на който и да е линк.
4. Използва `urlRedirectUpdate`, за да насочи 7-те познати адреса към Shopify CDN, когато **всички** файлове са готови.

При липсващ `SHOPIFY_FEED_TOKEN` този последен етап се пропуска безопасно и GitHub остава действащият автоматичен източник. Не се активира платен хостинг.

Автоматичното генериране чрез GitHub Actions е на всеки 4 часа; действителното време може да се отклонява при натоварване на платформата.

Проверка на шестте Shopify файла: https://github.com/bgshoppingnet/saas_mining_manager/actions/workflows/verify-shopify-hosted-feeds.yml

### Състояние към 29 октомври 2026, 01:35 (Europe/Sofia)

Шестте компактни файла са в Shopify CDN със съответния MIME и размер, но `SHOPIFY_CLIENT_ID` и `SHOPIFY_CLIENT_SECRET` не са конфигурирани в GitHub Actions към последната проверка. Поради тази липса **7-те работещи branded redirects остават временно към GitHub**. Автоматичното им преминаване към Shopify се извършва от `scripts/sync_shopify_cdn.py` само след валидирано качване на всичките шест файла и активна авторизация. Планът за автоматизация през ChatGPT е блокиран от лимита за активни задачи; не са спирани други автоматизации.
