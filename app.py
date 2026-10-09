from flask import Flask, Response, jsonify
import os, html, requests, re, threading, time, json
from urllib.parse import quote
from run_catalog_sync_v3 import run as run_catalog_sync

app = Flask(__name__)
PUBLIC_SHOP_URL = os.getenv('PUBLIC_SHOP_URL', 'https://bgshopping.net').rstrip('/')
VAT_RATE = float(os.getenv('VAT_RATE', '0.20'))
REFRESH_SECONDS = int(os.getenv('REFRESH_SECONDS', '14400'))
ENABLE_CATALOG_SYNC = os.getenv('ENABLE_CATALOG_SYNC', 'true').lower() in ('1', 'true', 'yes', 'on')
SHOPIFY_SHOP_DOMAIN = os.getenv('SHOPIFY_SHOP_DOMAIN', '').strip()
SHOPIFY_ADMIN_ACCESS_TOKEN = os.getenv('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()
SHOPIFY_API_VERSION = os.getenv('SHOPIFY_API_VERSION', '2026-10').strip()
FEED_PATH = '/tmp/pazaruvaj.xml'
TMP_PATH = '/tmp/pazaruvaj.xml.tmp'
CATALOG_STATUS_PATH = os.getenv('CATALOG_SYNC_STATUS_PATH', '/tmp/catalog-sync-status.json')
SUPPLIER_STATUS_PATH = os.getenv('SUPPLIER_STATUS_PATH', '/tmp/supplier-status.json')
SHOPIFY_STATUS_PATH = os.getenv('SHOPIFY_SYNC_STATUS_PATH', '/tmp/shopify-sync-status.json')
state = {'building': False, 'last_ok': None, 'last_error': None, 'products': 0, 'variants': 0, 'bytes': 0}
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
      variants(first: 100) {
        nodes {
          id
          title
          sku
          barcode
          price
          availableForSale
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
        'shopify_domain_configured': bool(SHOPIFY_SHOP_DOMAIN),
        'shopify_token_configured': bool(SHOPIFY_ADMIN_ACCESS_TOKEN),
        'shopify_location_configured': bool(os.getenv('SHOPIFY_LOCATION_ID', '').strip()),
        'fix_handles': os.getenv('SHOPIFY_FIX_HANDLES', 'false').lower() in ('1','true','yes','on'),
        'add_images': os.getenv('SHOPIFY_ADD_IMAGES', 'true').lower() in ('1','true','yes','on'),
    }


def admin_gql(session, query, variables=None):
    if not SHOPIFY_SHOP_DOMAIN or not SHOPIFY_ADMIN_ACCESS_TOKEN:
        raise RuntimeError('Shopify Admin API is not configured')
    url = f'https://{SHOPIFY_SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json'
    r = session.post(
        url,
        headers={
            'X-Shopify-Access-Token': SHOPIFY_ADMIN_ACCESS_TOKEN,
            'Content-Type': 'application/json',
            'User-Agent': 'BGShopping-Pazaruvaj-Feed/7.0',
        },
        json={'query': query, 'variables': variables or {}},
        timeout=(20, 120),
    )
    r.raise_for_status()
    data = r.json()
    if data.get('errors'):
        raise RuntimeError('Shopify GraphQL: ' + json.dumps(data['errors'], ensure_ascii=False))
    return data.get('data') or {}


def product_xml(p):
    handle = (p.get('handle') or '').strip()
    if not handle:
        return '', 0

    vendor = (p.get('vendor') or '').strip()
    ptype = (p.get('productType') or '').strip()
    desc = strip_html(p.get('descriptionHtml') or '')
    image_nodes = ((p.get('images') or {}).get('nodes') or [])[:3]
    image_urls = []
    for img in image_nodes:
        url = (img or {}).get('url') or ''
        if url and url not in image_urls:
            image_urls.append(url)
    if not image_urls:
        featured = ((p.get('featuredImage') or {}).get('url') or '').strip()
        if featured:
            image_urls.append(featured)

    chunks = []
    variants = 0
    for v in ((p.get('variants') or {}).get('nodes') or []):
        if not v.get('availableForSale', False):
            continue

        try:
            gross = float(v.get('price') or 0)
            if gross <= 0:
                continue
            net = gross / (1 + VAT_RATE)
        except Exception:
            continue

        vid = numeric_gid(v.get('id'))
        sku = (v.get('sku') or '').strip()
        barcode = (v.get('barcode') or '').strip()
        identifier = sku or barcode or vid
        if not identifier:
            continue

        title = (p.get('title') or '').strip()
        vtitle = (v.get('title') or '').strip()
        if vtitle and vtitle != 'Default Title':
            title = f'{title} - {vtitle}'
        if not title:
            continue

        link = f'{PUBLIC_SHOP_URL}/products/{quote(handle)}'
        if vid:
            link += f'?variant={vid}'

        parts = [
            '<product>',
            f'<identifier>{esc(identifier)}</identifier>',
            f'<manufacturer>{esc(vendor)}</manufacturer>',
            f'<name>{esc(title)}</name>',
            f'<category>{esc(ptype)}</category>',
            f'<product_url>{esc(link)}</product_url>',
            f'<price>{gross:.2f}</price>',
            f'<net_price>{net:.2f}</net_price>',
        ]
        if sku:
            parts.append(f'<sku>{esc(sku)}</sku>')
        if barcode:
            parts.append(f'<ean>{esc(barcode)}</ean>')
        if desc:
            parts.append(f'<description>{esc(desc)}</description>')
        for i, src in enumerate(image_urls, start=1):
            parts.append(f'<image{i}>{esc(src)}</image{i}>')
        parts.append('<delivery_time>1</delivery_time>')
        parts.append('</product>')
        chunks.append('\n'.join(parts) + '\n')
        variants += 1

    return ''.join(chunks), variants


def build_feed_once():
    with lock:
        if state['building']:
            return False
        state['building'] = True
        state['last_error'] = None

    try:
        session = requests.Session()
        product_count = 0
        variant_count = 0
        cursor = None
        page = 0

        with open(TMP_PATH, 'w', encoding='utf-8', newline='\n') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<products>\n')
            while True:
                page += 1
                data = admin_gql(session, PRODUCTS_QUERY, {'cursor': cursor})
                conn = data.get('products') or {}
                nodes = conn.get('nodes') or []
                for p in nodes:
                    xml, variants = product_xml(p)
                    if xml:
                        f.write(xml)
                        product_count += 1
                        variant_count += variants

                pi = conn.get('pageInfo') or {}
                if not pi.get('hasNextPage'):
                    break
                cursor = pi.get('endCursor')
                if not cursor or page >= 1000:
                    break

            f.write('</products>\n')
            f.flush()
            os.fsync(f.fileno())

        size = os.path.getsize(TMP_PATH)
        if product_count <= 0 or variant_count <= 0 or size < 1000:
            raise RuntimeError(f'Feed integrity check failed: products={product_count}, variants={variant_count}, bytes={size}')

        os.replace(TMP_PATH, FEED_PATH)
        with lock:
            state['last_ok'] = time.time()
            state['products'] = product_count
            state['variants'] = variant_count
            state['bytes'] = size
            state['last_error'] = None

        print(json.dumps({
            'feed_build': 'completed',
            'products': product_count,
            'variants': variant_count,
            'bytes': size,
            'source': 'Shopify Admin GraphQL',
            'pages': page,
        }, ensure_ascii=False), flush=True)
        return True

    except Exception as e:
        try:
            if os.path.exists(TMP_PATH):
                os.remove(TMP_PATH)
        except Exception:
            pass
        with lock:
            state['last_error'] = f'{type(e).__name__}: {e}'
        print(json.dumps({'feed_build': 'failed', 'error': state['last_error']}, ensure_ascii=False), flush=True)
        return False

    finally:
        with lock:
            state['building'] = False


def feed_loop():
    while True:
        build_feed_once()
        time.sleep(REFRESH_SECONDS)


def catalog_loop():
    if not ENABLE_CATALOG_SYNC:
        return
    while True:
        try:
            print(json.dumps({'catalog_sync':'starting'}, ensure_ascii=False), flush=True)
            result = run_catalog_sync()
            print(json.dumps({'catalog_sync':'finished','state':(result or {}).get('state')}, ensure_ascii=False), flush=True)
        except Exception as e:
            print(json.dumps({'catalog_sync_error': f'{type(e).__name__}: {e}'}, ensure_ascii=False), flush=True)
        time.sleep(REFRESH_SECONDS)


def start_builders():
    threading.Thread(target=feed_loop, name='bgshopping-feed-builder', daemon=True).start()
    threading.Thread(target=catalog_loop, name='bgshopping-catalog-sync', daemon=True).start()


@app.get('/')
def home():
    return jsonify(service='BGShopping Catalog Sync', feed='/pazaruvaj.xml', feed_status='/feed-status', catalog_status='/catalog-status', supplier_status='/supplier-status', shopify_status='/shopify-sync-status')


@app.get('/health')
def health():
    return jsonify(ok=True)


@app.get('/feed-status')
def feed_status():
    with lock:
        data = dict(state)
    exists = os.path.exists(FEED_PATH)
    data['ready'] = exists and os.path.getsize(FEED_PATH) > 1000
    data['file_bytes'] = os.path.getsize(FEED_PATH) if exists else 0
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
    if not os.path.exists(FEED_PATH) or os.path.getsize(FEED_PATH) <= 1000:
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?>\n<products></products>\n',
            status=503,
            content_type='application/xml; charset=utf-8',
            headers={'Retry-After': '60', 'Cache-Control': 'no-store'},
        )

    try:
        with open(FEED_PATH, 'rb') as f:
            payload = f.read()
    except Exception as e:
        return Response(
            f'<?xml version="1.0" encoding="UTF-8"?>\n<error>{esc(type(e).__name__)}</error>\n',
            status=503,
            content_type='application/xml; charset=utf-8',
            headers={'Retry-After': '60', 'Cache-Control': 'no-store'},
        )

    if len(payload) <= 1000 or b'<product>' not in payload:
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?>\n<products></products>\n',
            status=503,
            content_type='application/xml; charset=utf-8',
            headers={'Retry-After': '60', 'Cache-Control': 'no-store'},
        )

    return Response(
        payload,
        status=200,
        content_type='application/xml; charset=utf-8',
        headers={
            'Cache-Control': 'public, max-age=300',
            'Content-Length': str(len(payload)),
            'X-BGShopping-Feed-Products': str(state.get('products', 0)),
            'X-BGShopping-Feed-Variants': str(state.get('variants', 0)),
        },
    )


start_builders()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')))
