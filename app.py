from flask import Flask, Response, jsonify, stream_with_context
import os, html, requests, re
from urllib.parse import quote

app = Flask(__name__)
SHOP_URL = os.getenv('SHOP_URL', 'https://bgshopping.net').rstrip('/')
PUBLIC_SHOP_URL = os.getenv('PUBLIC_SHOP_URL', 'https://bgshopping.net').rstrip('/')
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


def stream_feed():
    yield '<?xml version="1.0" encoding="UTF-8"?>\n<products>\n'

    session = requests.Session()
    session.headers.update({'User-Agent': 'BGShopping-Pazaruvaj-Feed/2.0'})
    since_id = 0
    seen = set()

    while True:
        params = {'limit': 250}
        if since_id:
            params['since_id'] = since_id

        r = session.get(f'{SHOP_URL}/products.json', params=params, timeout=(10, 90))
        r.raise_for_status()
        batch = r.json().get('products', [])
        if not batch:
            break

        for p in batch:
            pid = p.get('id')
            if pid in seen:
                continue
            if pid is not None:
                seen.add(pid)
            yield from product_xml(p)

        last_id = batch[-1].get('id')
        if len(batch) < 250 or not last_id or last_id == since_id:
            break
        since_id = last_id

    yield '</products>\n'


@app.get('/')
def home():
    return jsonify(
        service='BGShopping Pazaruvaj Feed',
        feed='/pazaruvaj.xml',
        health='/health',
        source=SHOP_URL,
    )


@app.get('/health')
def health():
    return jsonify(ok=True, shop=SHOP_URL)


@app.get('/pazaruvaj.xml')
def pazaruvaj():
    return Response(
        stream_with_context(stream_feed()),
        content_type='application/xml; charset=utf-8',
        headers={'Cache-Control': 'public, max-age=3600'},
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')))
