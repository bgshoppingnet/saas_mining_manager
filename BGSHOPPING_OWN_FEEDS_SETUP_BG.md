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
