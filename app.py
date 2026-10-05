from flask import Flask, Response, jsonify, stream_with_context
import os, html, requests, re
from urllib.parse import quote

app = Flask(__name__)
SHOP_URL = os.getenv('SHOP_URL', 'https://bgshopping.net').rstrip('/')
PUBLIC_SHOP_URL = os.getenv('PUBLIC_SHOP_URL', 'https://bgshopping.net').rstrip('/')
MYSHOPIFY_URL = os.getenv('MYSHOPIFY_URL', 'https://kynvva-gx.myshopify.com').rstrip('/')
VAT_RATE = float(os.getenv('VAT_RATE', '0.20'))


def esc(v):
    return html.escape(str(v or ''), quote=False)


def strip_html(s):
    s = re.sub(r'<[^>]+>', ' ', s or '')
    return ' '.join(html.unescape(s).split())


def product_xml(p):
    handle = p.get('handle', '')
    vendor = p.get('vendor', '')
    ptype = p.get('product_type', '')
    desc = strip_html(p.get('body_html', ''))
    images = p.get('images') or []

    for v in p.get('variants') or []:
        if not v.get('available', True):
            continue

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
        yield '\n'.join(parts) + '\n'


def source_candidates():
    bases = []
    for base in (SHOP_URL, PUBLIC_SHOP_URL, MYSHOPIFY_URL):
        if base and base not in bases:
            bases.append(base)
    paths = ('/products.json', '/collections/all/products.json')
    return [(base, path) for base in bases for path in paths]


def probe_source(session, base, path):
    url = f'{base}{path}'
    try:
        r = session.get(url, params={'limit': 250, 'page': 1}, timeout=(10, 45), allow_redirects=True)
        content_type = r.headers.get('content-type', '')
        data = r.json() if 'json' in content_type.lower() or r.text.lstrip().startswith('{') else {}
        products = data.get('products', []) if isinstance(data, dict) else []
        return {
            'base': base,
            'path': path,
            'url': r.url,
            'status': r.status_code,
            'content_type': content_type,
            'count': len(products),
            'bytes': len(r.content),
        }, products
    except Exception as e:
        return {
            'base': base,
            'path': path,
            'status': 0,
            'count': 0,
            'error': f'{type(e).__name__}: {e}',
        }, []


def choose_source(session):
    probes = []
    for base, path in source_candidates():
        probe, products = probe_source(session, base, path)
        probes.append(probe)
        if products:
            return base, path, products, probes
    return None, None, [], probes


def stream_feed():
    yield '<?xml version="1.0" encoding="UTF-8"?>\n<products>\n'

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (compatible; BGShopping-Pazaruvaj-Feed/3.0; +https://bgshopping.net)',
        'Accept': 'application/json,text/plain,*/*',
    })

    base, path, first_batch, _ = choose_source(session)
    if not base:
        yield '</products>\n'
        return

    page = 1
    batch = first_batch
    seen = set()
    while batch:
        for p in batch:
            pid = p.get('id')
            if pid in seen:
                continue
            if pid is not None:
                seen.add(pid)
            yield from product_xml(p)

        if len(batch) < 250:
            break

        page += 1
        r = session.get(f'{base}{path}', params={'limit': 250, 'page': page}, timeout=(10, 90), allow_redirects=True)
        r.raise_for_status()
        data = r.json()
        batch = data.get('products', []) if isinstance(data, dict) else []
        if page > 500:
            break

    yield '</products>\n'


@app.get('/')
def home():
    return jsonify(
        service='BGShopping Pazaruvaj Feed',
        feed='/pazaruvaj.xml',
        health='/health',
        diagnostics='/debug-source',
        source=SHOP_URL,
    )


@app.get('/health')
def health():
    return jsonify(ok=True, shop=SHOP_URL, public_shop=PUBLIC_SHOP_URL, myshopify=MYSHOPIFY_URL)


@app.get('/debug-source')
def debug_source():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (compatible; BGShopping-Pazaruvaj-Feed/3.0; +https://bgshopping.net)',
        'Accept': 'application/json,text/plain,*/*',
    })
    base, path, products, probes = choose_source(session)
    return jsonify(ok=bool(products), selected_base=base, selected_path=path, selected_count=len(products), probes=probes)


@app.get('/pazaruvaj.xml')
def pazaruvaj():
    return Response(
        stream_with_context(stream_feed()),
        content_type='application/xml; charset=utf-8',
        headers={'Cache-Control': 'no-cache, no-store, must-revalidate'},
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')))
