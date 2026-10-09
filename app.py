from flask import Flask, Response, jsonify, send_file
import os, html, requests, re, threading, time, json
from urllib.parse import quote
from run_catalog_sync_v2 import run as run_catalog_sync

app = Flask(__name__)
SHOP_URL = os.getenv('SHOP_URL', 'https://bgshopping.net').rstrip('/')
PUBLIC_SHOP_URL = os.getenv('PUBLIC_SHOP_URL', 'https://bgshopping.net').rstrip('/')
MYSHOPIFY_URL = os.getenv('MYSHOPIFY_URL', 'https://kynvva-gx.myshopify.com').rstrip('/')
VAT_RATE = float(os.getenv('VAT_RATE', '0.20'))
REFRESH_SECONDS = int(os.getenv('REFRESH_SECONDS', '14400'))
ENABLE_CATALOG_SYNC = os.getenv('ENABLE_CATALOG_SYNC', 'true').lower() in ('1', 'true', 'yes', 'on')
FEED_PATH = '/tmp/pazaruvaj.xml'
TMP_PATH = '/tmp/pazaruvaj.xml.tmp'
CATALOG_STATUS_PATH = os.getenv('CATALOG_SYNC_STATUS_PATH', '/tmp/catalog-sync-status.json')
SUPPLIER_STATUS_PATH = os.getenv('SUPPLIER_STATUS_PATH', '/tmp/supplier-status.json')
SHOPIFY_STATUS_PATH = os.getenv('SHOPIFY_SYNC_STATUS_PATH', '/tmp/shopify-sync-status.json')
state = {'building': False, 'last_ok': None, 'last_error': None, 'products': 0, 'variants': 0}
lock = threading.Lock()


def esc(v):
    return html.escape(str(v or ''), quote=False)


def strip_html(s):
    s = re.sub(r'<[^>]+>', ' ', s or '')
    return ' '.join(html.unescape(s).split())


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
        'shopify_location_configured': bool(os.getenv('SHOPIFY_LOCATION_ID', '').strip()),
        'fix_handles': os.getenv('SHOPIFY_FIX_HANDLES', 'false').lower() in ('1','true','yes','on'),
        'add_images': os.getenv('SHOPIFY_ADD_IMAGES', 'true').lower() in ('1','true','yes','on'),
    }


def product_xml(p):
    handle = p.get('handle', '')
    vendor = p.get('vendor', '')
    ptype = p.get('product_type', '')
    desc = strip_html(p.get('body_html', ''))
    images = p.get('images') or []
    chunks = []
    variants = 0
    for v in p.get('variants') or []:
        if not v.get('available', True):
            continue
        variants += 1
        vid = v.get('id', '')
        title = p.get('title', '')
        vtitle = v.get('title', '')
        if vtitle and vtitle != 'Default Title':
            title = f'{title} - {vtitle}'
        price = v.get('price') or '0'
        try:
            gross = float(price)
            net = gross / (1 + VAT_RATE)
        except Exception:
            net = 0
        link = f'{PUBLIC_SHOP_URL}/products/{quote(handle)}?variant={vid}'
        sku = v.get('sku', '')
        barcode = v.get('barcode') or ''
        parts = [
            '<product>',
            f'<identifier>{esc(vid)}</identifier>',
            f'<manufacturer>{esc(vendor)}</manufacturer>',
            f'<name>{esc(title)}</name>',
            f'<category>{esc(ptype)}</category>',
            f'<product_url>{esc(link)}</product_url>',
            f'<price>{esc(price)}</price>',
            f'<net_price>{net:.2f}</net_price>',
        ]
        if sku:
            parts.append(f'<sku>{esc(sku)}</sku>')
        if barcode:
            parts.append(f'<ean>{esc(barcode)}</ean>')
        if desc:
            parts.append(f'<description>{esc(desc)}</description>')
        for i, img in enumerate(images[:3], start=1):
            src = img.get('src') or ''
            if src:
                parts.append(f'<image{i}>{esc(src)}</image{i}>')
        parts.append('<delivery_time>1</delivery_time>')
        parts.append('</product>')
        chunks.append('\n'.join(parts) + '\n')
    return ''.join(chunks), variants


def source_candidates():
    bases = []
    for base in (SHOP_URL, PUBLIC_SHOP_URL, MYSHOPIFY_URL):
        if base and base not in bases:
            bases.append(base)
    paths = ('/products.json', '/collections/all/products.json')
    return [(base, path) for base in bases for path in paths]


def choose_source(session):
    probes = []
    for base, path in source_candidates():
        try:
            r = session.get(f'{base}{path}', params={'limit': 250}, timeout=(10, 45), allow_redirects=True)
            data = r.json()
            products = data.get('products', []) if isinstance(data, dict) else []
            probes.append({'base': base, 'path': path, 'status': r.status_code, 'count': len(products)})
            if products:
                return base, path, products, probes
        except Exception as e:
            probes.append({'base': base, 'path': path, 'status': 0, 'count': 0, 'error': f'{type(e).__name__}: {e}'})
    return None, None, [], probes


def build_feed_once():
    with lock:
        if state['building']:
            return
        state['building'] = True
        state['last_error'] = None
    try:
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (compatible; BGShopping-Pazaruvaj-Feed/5.0; +https://bgshopping.net)',
            'Accept': 'application/json,text/plain,*/*',
        })
        base, path, batch, _ = choose_source(session)
        if not base:
            raise RuntimeError('No Shopify JSON product source returned products')
        seen = set()
        since_id = 0
        product_count = 0
        variant_count = 0
        request_count = 0
        with open(TMP_PATH, 'w', encoding='utf-8', newline='\n') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<products>\n')
            while batch and request_count < 500:
                max_id = since_id
                for p in batch:
                    pid = p.get('id')
                    if pid in seen:
                        continue
                    if pid is not None:
                        seen.add(pid)
                        try:
                            max_id = max(max_id, int(pid))
                        except Exception:
                            pass
                    xml, variants = product_xml(p)
                    if xml:
                        f.write(xml)
                        variant_count += variants
                    product_count += 1
                if len(batch) < 250 or max_id <= since_id:
                    break
                since_id = max_id
                request_count += 1
                r = session.get(f'{base}{path}', params={'limit': 250, 'since_id': since_id}, timeout=(10, 60), allow_redirects=True)
                r.raise_for_status()
                data = r.json()
                batch = data.get('products', []) if isinstance(data, dict) else []
            f.write('</products>\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(TMP_PATH, FEED_PATH)
        with lock:
            state['last_ok'] = time.time()
            state['products'] = product_count
            state['variants'] = variant_count
            state['last_error'] = None
    except Exception as e:
        try:
            if os.path.exists(TMP_PATH):
                os.remove(TMP_PATH)
        except Exception:
            pass
        with lock:
            state['last_error'] = f'{type(e).__name__}: {e}'
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
    data['ready'] = os.path.exists(FEED_PATH)
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
    if not os.path.exists(FEED_PATH):
        return Response('<?xml version="1.0" encoding="UTF-8"?>\n<products></products>\n', status=503, content_type='application/xml; charset=utf-8', headers={'Retry-After': '60', 'Cache-Control': 'no-store'})
    return send_file(FEED_PATH, mimetype='application/xml', as_attachment=False, conditional=True, max_age=300)


start_builders()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')))
