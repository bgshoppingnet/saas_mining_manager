from flask import Flask, Response, jsonify
import os, html, requests, re, threading, time, json
from urllib.parse import quote
from run_catalog_sync_v3 import run as run_catalog_sync
from run_catalog_sync import ensure_shopify_access_token

app = Flask(__name__)
PUBLIC_SHOP_URL = os.getenv('PUBLIC_SHOP_URL', 'https://bgshopping.net').rstrip('/')
VAT_RATE = float(os.getenv('VAT_RATE', '0.20'))
REFRESH_SECONDS = int(os.getenv('REFRESH_SECONDS', '14400'))
ENABLE_CATALOG_SYNC = os.getenv('ENABLE_CATALOG_SYNC', 'true').lower() in ('1', 'true', 'yes', 'on')
SHOPIFY_API_VERSION = os.getenv('SHOPIFY_API_VERSION', '2026-10').strip()

PAZARUVAJ_PATH = '/tmp/pazaruvaj.xml'
PAZARUVAJ_TMP = '/tmp/pazaruvaj.xml.tmp'
GOOGLE_PATH = '/tmp/google-shopping.xml'
GOOGLE_TMP = '/tmp/google-shopping.xml.tmp'
AI_PATH = '/tmp/ai-products.json'
AI_TMP = '/tmp/ai-products.json.tmp'

CATALOG_STATUS_PATH = os.getenv('CATALOG_SYNC_STATUS_PATH', '/tmp/catalog-sync-status.json')
SUPPLIER_STATUS_PATH = os.getenv('SUPPLIER_STATUS_PATH', '/tmp/supplier-status.json')
SHOPIFY_STATUS_PATH = os.getenv('SHOPIFY_SYNC_STATUS_PATH', '/tmp/shopify-sync-status.json')

state = {
    'building': False,
    'last_ok': None,
    'last_error': None,
    'products': 0,
    'variants': 0,
    'bytes': 0,
    'pages': 0,
    'google_items': 0,
    'google_bytes': 0,
    'ai_items': 0,
    'ai_bytes': 0,
}
lock = threading.Lock()

PRODUCTS_QUERY = '''
query FeedProducts($cursor: String) {
  products(first: 100, after: $cursor, query: "status:active", sortKey: ID) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      handle
      vendor
      productType
      descriptionHtml
      featuredImage { url }
      images(first: 3) { nodes { url } }
      variants(first: 20) {
        nodes {
          id
          title
          sku
          barcode
          price
          availableForSale
          image { url }
        }
      }
    }
  }
}
'''


def esc(v):
    return html.escape(str(v or ''), quote=False)


def strip_html(s):
    s = re.sub(r'<[^>]+>', ' ', s or '')
    return ' '.join(html.unescape(s).split())


def numeric_gid(gid):
    return str(gid or '').rsplit('/', 1)[-1]


def read_json_status(path):
    try:
        if not os.path.exists(path):
            return None
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        return {'state': 'status_read_error', 'error': f'{type(e).__name__}: {e}'}


def safe_sync_config():
    raw = os.getenv('SUPPLIER_FEEDS_JSON', '').strip()
    feed_count = 1
    if raw:
        try:
            feed_count = len([x for x in json.loads(raw) if x.get('enabled', True) and x.get('url')])
        except Exception:
            feed_count = None
    return {
        'enabled': ENABLE_CATALOG_SYNC,
        'refresh_seconds': REFRESH_SECONDS,
        'supplier_feed_count': feed_count,
        'shopify_write_enabled': os.getenv('SHOPIFY_WRITE_ENABLED', 'false').lower() in ('1','true','yes','on'),
        'shopify_domain_configured': bool(os.getenv('SHOPIFY_SHOP_DOMAIN', '').strip()),
        'shopify_token_configured': bool(os.getenv('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()),
        'shopify_client_id_configured': bool(os.getenv('SHOPIFY_CLIENT_ID', '').strip()),
        'shopify_client_secret_configured': bool(os.getenv('SHOPIFY_CLIENT_SECRET', '').strip()),
        'shopify_location_configured': bool(os.getenv('SHOPIFY_LOCATION_ID', '').strip()),
    }


def admin_gql(session, query, variables=None):
    shop = os.getenv('SHOPIFY_SHOP_DOMAIN', '').strip()
    token = os.getenv('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()
    if not shop or not token:
        raise RuntimeError('Shopify Admin API is not configured after authentication')
    url = f'https://{shop}/admin/api/{SHOPIFY_API_VERSION}/graphql.json'

    last_error = None
    for attempt in range(1, 11):
        try:
            r = session.post(
                url,
                headers={
                    'X-Shopify-Access-Token': token,
                    'Content-Type': 'application/json',
                    'User-Agent': 'BGShopping-Commerce-Feeds/8.0',
                },
                json={'query': query, 'variables': variables or {}},
                timeout=(20, 120),
            )
            if r.status_code == 429:
                retry_after = int(r.headers.get('Retry-After') or '2')
                print(json.dumps({'feed_build':'throttled','attempt':attempt,'retry_after':retry_after}, ensure_ascii=False), flush=True)
                time.sleep(min(max(retry_after, 1), 15))
                continue
            r.raise_for_status()
            data = r.json()
            errors = data.get('errors') or []
            if errors:
                throttled = any((e.get('extensions') or {}).get('code') == 'THROTTLED' for e in errors if isinstance(e, dict))
                if throttled:
                    delay = min(2 * attempt, 15)
                    print(json.dumps({'feed_build':'throttled','attempt':attempt,'retry_after':delay}, ensure_ascii=False), flush=True)
                    time.sleep(delay)
                    continue
                raise RuntimeError('Shopify GraphQL: ' + json.dumps(errors, ensure_ascii=False))
            return data.get('data') or {}
        except requests.RequestException as e:
            last_error = e
            if attempt >= 10:
                raise
            delay = min(2 * attempt, 15)
            print(json.dumps({'feed_build':'http_retry','attempt':attempt,'error':f'{type(e).__name__}: {e}','retry_after':delay}, ensure_ascii=False), flush=True)
            time.sleep(delay)

    raise RuntimeError(f'Shopify Admin GraphQL retries exhausted: {last_error}')


def base_product_data(p):
    handle = (p.get('handle') or '').strip()
    if not handle:
        return None
    image_nodes = ((p.get('images') or {}).get('nodes') or [])[:3]
    product_images = []
    for img in image_nodes:
        url = ((img or {}).get('url') or '').strip()
        if url and url not in product_images:
            product_images.append(url)
    featured = ((p.get('featuredImage') or {}).get('url') or '').strip()
    if featured and featured not in product_images:
        product_images.insert(0, featured)
    return {
        'handle': handle,
        'vendor': (p.get('vendor') or '').strip(),
        'product_type': (p.get('productType') or '').strip(),
        'description': strip_html(p.get('descriptionHtml') or ''),
        'title': (p.get('title') or '').strip(),
        'images': product_images[:3],
    }


def variant_data(base, v):
    if not base:
        return None
    try:
        gross = float(v.get('price') or 0)
        if gross <= 0:
            return None
    except Exception:
        return None

    vid = numeric_gid(v.get('id'))
    sku = (v.get('sku') or '').strip()
    barcode = (v.get('barcode') or '').strip()
    identifier = sku or barcode or vid
    if not identifier:
        return None

    title = base['title']
    vtitle = (v.get('title') or '').strip()
    if vtitle and vtitle != 'Default Title':
        title = f'{title} - {vtitle}'
    if not title:
        return None

    link = f"{PUBLIC_SHOP_URL}/products/{quote(base['handle'])}"
    if vid:
        link += f'?variant={vid}'

    images = []
    variant_image = (((v.get('image') or {}).get('url')) or '').strip()
    if variant_image:
        images.append(variant_image)
    for src in base['images']:
        if src and src not in images:
            images.append(src)
        if len(images) >= 3:
            break

    return {
        'id': identifier,
        'variant_id': vid,
        'sku': sku,
        'barcode': barcode,
        'title': title,
        'description': base['description'],
        'brand': base['vendor'],
        'product_type': base['product_type'],
        'link': link,
        'price': gross,
        'net_price': gross / (1 + VAT_RATE),
        'available': bool(v.get('availableForSale', False)),
        'images': images[:3],
    }


def pazaruvaj_xml_item(d):
    if not d or not d['available']:
        return ''
    parts = [
        '<product>',
        f"<identifier>{esc(d['id'])}</identifier>",
        f"<manufacturer>{esc(d['brand'])}</manufacturer>",
        f"<name>{esc(d['title'])}</name>",
        f"<category>{esc(d['product_type'])}</category>",
        f"<product_url>{esc(d['link'])}</product_url>",
        f"<price>{d['price']:.2f}</price>",
        f"<net_price>{d['net_price']:.2f}</net_price>",
    ]
    if d['sku']:
        parts.append(f"<sku>{esc(d['sku'])}</sku>")
    if d['barcode']:
        parts.append(f"<ean>{esc(d['barcode'])}</ean>")
    if d['description']:
        parts.append(f"<description>{esc(d['description'])}</description>")
    for i, src in enumerate(d['images'], start=1):
        tag = 'Image_url' if i == 1 else f'Image_url_{i}'
        parts.append(f'<{tag}>{esc(src)}</{tag}>')
    parts.append('<delivery_time>1</delivery_time>')
    parts.append('</product>')
    return '\n'.join(parts) + '\n'


def google_xml_item(d):
    if not d:
        return ''
    availability = 'in_stock' if d['available'] else 'out_of_stock'
    parts = [
        '<item>',
        f"<g:id>{esc(d['id'])}</g:id>",
        f"<title>{esc(d['title'])}</title>",
        f"<link>{esc(d['link'])}</link>",
        f"<g:availability>{availability}</g:availability>",
        '<g:condition>new</g:condition>',
        f"<g:price>{d['price']:.2f} EUR</g:price>",
    ]
    if d['description']:
        parts.append(f"<description>{esc(d['description'])}</description>")
    if d['brand']:
        parts.append(f"<g:brand>{esc(d['brand'])}</g:brand>")
    if d['product_type']:
        parts.append(f"<g:product_type>{esc(d['product_type'])}</g:product_type>")
    if d['barcode']:
        parts.append(f"<g:gtin>{esc(d['barcode'])}</g:gtin>")
    elif d['sku']:
        parts.append(f"<g:mpn>{esc(d['sku'])}</g:mpn>")
    if d['images']:
        parts.append(f"<g:image_link>{esc(d['images'][0])}</g:image_link>")
        for src in d['images'][1:]:
            parts.append(f"<g:additional_image_link>{esc(src)}</g:additional_image_link>")
    parts.append('</item>')
    return '\n'.join(parts) + '\n'


def ai_json_item(d):
    if not d:
        return None
    item = {
        '@type': 'Product',
        'id': d['id'],
        'name': d['title'],
        'url': d['link'],
        'image': d['images'],
        'brand': d['brand'] or None,
        'category': d['product_type'] or None,
        'sku': d['sku'] or None,
        'gtin': d['barcode'] or None,
        'description': d['description'] or None,
        'offers': {
            '@type': 'Offer',
            'price': f"{d['price']:.2f}",
            'priceCurrency': 'EUR',
            'availability': 'https://schema.org/InStock' if d['available'] else 'https://schema.org/OutOfStock',
            'url': d['link'],
        },
    }
    return item


def build_feeds_once():
    with lock:
        if state['building']:
            return False
        state['building'] = True
        state['last_error'] = None
        state['pages'] = 0

    try:
        auth = ensure_shopify_access_token()
        session = requests.Session()
        pazar_products = 0
        pazar_variants = 0
        google_items = 0
        ai_items = 0
        cursor = None
        page = 0
        first_ai = True

        with open(PAZARUVAJ_TMP, 'w', encoding='utf-8', newline='\n') as pf, \
             open(GOOGLE_TMP, 'w', encoding='utf-8', newline='\n') as gf, \
             open(AI_TMP, 'w', encoding='utf-8', newline='\n') as af:

            pf.write('<?xml version="1.0" encoding="UTF-8"?>\n<products>\n')
            gf.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            gf.write('<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">\n<channel>\n')
            gf.write('<title>BGShopping.net Product Feed</title>\n')
            gf.write(f'<link>{esc(PUBLIC_SHOP_URL)}</link>\n')
            gf.write('<description>BGShopping.net product catalog for shopping channels</description>\n')
            af.write('{"@context":"https://schema.org","generated_at":')
            af.write(json.dumps(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
            af.write(',"currency":"EUR","products":[\n')

            while True:
                page += 1
                data = admin_gql(session, PRODUCTS_QUERY, {'cursor': cursor})
                conn = data.get('products') or {}
                nodes = conn.get('nodes') or []

                for p in nodes:
                    base = base_product_data(p)
                    wrote_pazar_product = False
                    for v in ((p.get('variants') or {}).get('nodes') or []):
                        d = variant_data(base, v)
                        if not d:
                            continue

                        px = pazaruvaj_xml_item(d)
                        if px:
                            pf.write(px)
                            pazar_variants += 1
                            wrote_pazar_product = True

                        gx = google_xml_item(d)
                        if gx:
                            gf.write(gx)
                            google_items += 1

                        ai = ai_json_item(d)
                        if ai:
                            if not first_ai:
                                af.write(',\n')
                            af.write(json.dumps(ai, ensure_ascii=False, separators=(',', ':')))
                            first_ai = False
                            ai_items += 1

                    if wrote_pazar_product:
                        pazar_products += 1

                with lock:
                    state['pages'] = page
                    state['products'] = pazar_products
                    state['variants'] = pazar_variants
                    state['google_items'] = google_items
                    state['ai_items'] = ai_items

                if page == 1 or page % 10 == 0:
                    print(json.dumps({
                        'feed_build':'progress',
                        'page':page,
                        'pazaruvaj_products':pazar_products,
                        'pazaruvaj_variants':pazar_variants,
                        'google_items':google_items,
                        'ai_items':ai_items,
                    }, ensure_ascii=False), flush=True)

                pi = conn.get('pageInfo') or {}
                if not pi.get('hasNextPage'):
                    break
                cursor = pi.get('endCursor')
                if not cursor or page >= 1000:
                    break

            pf.write('</products>\n')
            gf.write('</channel>\n</rss>\n')
            af.write('\n]}\n')

            for f in (pf, gf, af):
                f.flush()
                os.fsync(f.fileno())

        pazar_size = os.path.getsize(PAZARUVAJ_TMP)
        google_size = os.path.getsize(GOOGLE_TMP)
        ai_size = os.path.getsize(AI_TMP)

        if pazar_products <= 0 or pazar_variants <= 0 or pazar_size < 1000:
            raise RuntimeError(f'Pazaruvaj feed integrity failed: products={pazar_products}, variants={pazar_variants}, bytes={pazar_size}')
        if google_items <= 0 or google_size < 1000:
            raise RuntimeError(f'Google feed integrity failed: items={google_items}, bytes={google_size}')
        if ai_items <= 0 or ai_size < 1000:
            raise RuntimeError(f'AI feed integrity failed: items={ai_items}, bytes={ai_size}')

        os.replace(PAZARUVAJ_TMP, PAZARUVAJ_PATH)
        os.replace(GOOGLE_TMP, GOOGLE_PATH)
        os.replace(AI_TMP, AI_PATH)

        with lock:
            state['last_ok'] = time.time()
            state['products'] = pazar_products
            state['variants'] = pazar_variants
            state['bytes'] = pazar_size
            state['pages'] = page
            state['google_items'] = google_items
            state['google_bytes'] = google_size
            state['ai_items'] = ai_items
            state['ai_bytes'] = ai_size
            state['last_error'] = None

        print(json.dumps({
            'feed_build':'completed',
            'pazaruvaj_products':pazar_products,
            'pazaruvaj_variants':pazar_variants,
            'pazaruvaj_bytes':pazar_size,
            'google_items':google_items,
            'google_bytes':google_size,
            'ai_items':ai_items,
            'ai_bytes':ai_size,
            'source':'Shopify Admin GraphQL',
            'pages':page,
            'auth_mode':(auth or {}).get('mode'),
        }, ensure_ascii=False), flush=True)
        return True

    except Exception as e:
        for path in (PAZARUVAJ_TMP, GOOGLE_TMP, AI_TMP):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        with lock:
            state['last_error'] = f'{type(e).__name__}: {e}'
        print(json.dumps({'feed_build':'failed','error':state['last_error']}, ensure_ascii=False), flush=True)
        return False
    finally:
        with lock:
            state['building'] = False


def orchestrator_loop():
    while True:
        feed_ok = build_feeds_once()
        if ENABLE_CATALOG_SYNC:
            try:
                print(json.dumps({'catalog_sync':'starting_after_feeds','feed_ok':feed_ok}, ensure_ascii=False), flush=True)
                result = run_catalog_sync()
                print(json.dumps({'catalog_sync':'finished','state':(result or {}).get('state')}, ensure_ascii=False), flush=True)
            except Exception as e:
                print(json.dumps({'catalog_sync_error':f'{type(e).__name__}: {e}'}, ensure_ascii=False), flush=True)
        time.sleep(REFRESH_SECONDS)


def start_builders():
    threading.Thread(target=orchestrator_loop, name='bgshopping-feeds-and-catalog', daemon=True).start()


def serve_file(path, content_type, min_size=1000):
    if not os.path.exists(path) or os.path.getsize(path) <= min_size:
        return Response('Feed is being generated.\n', status=503, content_type='text/plain; charset=utf-8', headers={'Retry-After':'60','Cache-Control':'no-store'})
    with open(path, 'rb') as f:
        payload = f.read()
    return Response(payload, status=200, content_type=content_type, headers={'Cache-Control':'public, max-age=300','Content-Length':str(len(payload))})


@app.get('/')
def home():
    return jsonify(
        service='BGShopping Commerce Feeds',
        pazaruvaj='/pazaruvaj.xml',
        google_shopping='/google-shopping.xml',
        ai_products='/ai-products.json',
        feed_status='/feed-status',
        catalog_status='/catalog-status',
        supplier_status='/supplier-status',
        shopify_status='/shopify-sync-status',
    )


@app.get('/health')
def health():
    return jsonify(ok=True)


@app.get('/feed-status')
def feed_status():
    with lock:
        data = dict(state)
    data['pazaruvaj_ready'] = os.path.exists(PAZARUVAJ_PATH) and os.path.getsize(PAZARUVAJ_PATH) > 1000
    data['google_ready'] = os.path.exists(GOOGLE_PATH) and os.path.getsize(GOOGLE_PATH) > 1000
    data['ai_ready'] = os.path.exists(AI_PATH) and os.path.getsize(AI_PATH) > 1000
    if data['last_ok']:
        data['last_ok_iso'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(data['last_ok']))
    return jsonify(data)


@app.get('/catalog-status')
def catalog_status():
    return jsonify(config=safe_sync_config(), status=read_json_status(CATALOG_STATUS_PATH))


@app.get('/supplier-status')
def supplier_status():
    return jsonify(status=read_json_status(SUPPLIER_STATUS_PATH))


@app.get('/shopify-sync-status')
def shopify_sync_status():
    return jsonify(config=safe_sync_config(), status=read_json_status(SHOPIFY_STATUS_PATH))


@app.get('/pazaruvaj.xml')
def pazaruvaj():
    return serve_file(PAZARUVAJ_PATH, 'application/xml; charset=utf-8')


@app.get('/google-shopping.xml')
def google_shopping():
    return serve_file(GOOGLE_PATH, 'application/xml; charset=utf-8')


@app.get('/ai-products.json')
def ai_products():
    return serve_file(AI_PATH, 'application/ld+json; charset=utf-8')


start_builders()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')))
