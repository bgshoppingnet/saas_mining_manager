import os, json, re, time, html
from decimal import Decimal, InvalidOperation
import requests

CATALOG_PATH = os.getenv('SUPPLIER_OUT_PATH', '/tmp/supplier-catalog.jsonl')
SYNC_STATUS_PATH = os.getenv('SHOPIFY_SYNC_STATUS_PATH', '/tmp/shopify-sync-status.json')
SHOP_DOMAIN = os.getenv('SHOPIFY_SHOP_DOMAIN', '').strip()
TOKEN = os.getenv('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()
API_VERSION = os.getenv('SHOPIFY_API_VERSION', '2026-10').strip()
WRITE_ENABLED = os.getenv('SHOPIFY_WRITE_ENABLED', 'false').lower() in ('1','true','yes','on')
DEFAULT_VENDOR = os.getenv('SHOPIFY_DEFAULT_VENDOR', '').strip()
REQUEST_DELAY = float(os.getenv('SHOPIFY_REQUEST_DELAY', '0.15'))

PRODUCT_SET = '''
mutation ProductSet($identifier: ProductSetIdentifiers, $input: ProductSetInput!) {
  productSet(identifier: $identifier, input: $input, synchronous: true) {
    product { id handle title }
    userErrors { field message }
  }
}
'''

def clean_text(v):
    v = html.unescape(str(v or ''))
    v = re.sub(r'<[^>]+>', ' ', v)
    return ' '.join(v.split())

def slugify(v):
    s = clean_text(v).lower()
    s = re.sub(r'[^a-z0-9а-я]+', '-', s, flags=re.I)
    return s.strip('-')[:120]

def money(v):
    if v in (None, ''):
        return None
    s = str(v).strip().replace(' ', '').replace(',', '.')
    s = re.sub(r'[^0-9.\-]', '', s)
    try:
        d = Decimal(s)
        if d < 0:
            return None
        return format(d.quantize(Decimal('0.01')), 'f')
    except (InvalidOperation, ValueError):
        return None

def seo_title(item):
    name = clean_text(item.get('name'))
    supplier = clean_text(item.get('supplier'))
    title = name or item.get('sku') or item.get('ean') or 'Продукт'
    if supplier and supplier.lower() not in title.lower():
        title = f'{title} | {supplier}'
    return title[:70]

def seo_description(item):
    name = clean_text(item.get('name'))
    supplier = clean_text(item.get('supplier'))
    bits = [x for x in (name, supplier) if x]
    base = ' • '.join(bits)
    suffix = ' Поръчайте онлайн от BGShopping.net.'
    return (base + suffix)[:320]

def product_description(item):
    name = clean_text(item.get('name'))
    supplier = clean_text(item.get('supplier'))
    sku = clean_text(item.get('sku'))
    ean = clean_text(item.get('ean'))
    parts = []
    if name:
        parts.append(f'<p><strong>{html.escape(name)}</strong></p>')
    if supplier:
        parts.append(f'<p>Марка / доставчик: {html.escape(supplier)}</p>')
    details = []
    if sku: details.append(f'SKU: {html.escape(sku)}')
    if ean: details.append(f'EAN: {html.escape(ean)}')
    if details:
        parts.append('<p>' + ' · '.join(details) + '</p>')
    return ''.join(parts)

def product_identifier(item):
    # Prefer stable handles until a dedicated unique metafield definition is enabled.
    key = clean_text(item.get('ean') or item.get('sku') or item.get('external_id') or item.get('key'))
    supplier = clean_text(item.get('supplier'))
    return slugify(f'{supplier}-{key}')

def build_product_input(item):
    name = clean_text(item.get('name')) or clean_text(item.get('sku')) or clean_text(item.get('ean'))
    if not name:
        raise ValueError('Missing product name/SKU/EAN')
    handle = product_identifier(item)
    p = {
        'title': name[:255],
        'handle': handle,
        'redirectNewHandle': True,
        'descriptionHtml': product_description(item),
        'vendor': clean_text(item.get('supplier')) or DEFAULT_VENDOR or None,
        'status': 'ACTIVE',
        'seo': {
            'title': seo_title(item),
            'description': seo_description(item),
        },
        'metafields': [
            {'namespace':'supplier_sync','key':'supplier','type':'single_line_text_field','value':clean_text(item.get('supplier')) or 'unknown'},
            {'namespace':'supplier_sync','key':'source_key','type':'single_line_text_field','value':clean_text(item.get('key')) or handle},
        ],
    }
    image = str(item.get('image') or '').strip()
    if image.startswith(('http://','https://')):
        p['files'] = [{
            'originalSource': image,
            'contentType': 'IMAGE',
            'alt': name[:512],
            'duplicateResolutionMode': 'APPEND_UUID',
        }]
    variant = {
        'optionValues': [{'optionName':'Title','name':'Default Title'}],
        'inventoryItem': {
            'sku': clean_text(item.get('sku')) or None,
            'tracked': True,
            'requiresShipping': True,
        },
        'barcode': clean_text(item.get('ean')) or None,
        'inventoryPolicy': 'CONTINUE',
    }
    price = money(item.get('price'))
    if price is not None:
        variant['price'] = price
    variant = {k:v for k,v in variant.items() if v is not None}
    if variant.get('inventoryItem'):
        variant['inventoryItem'] = {k:v for k,v in variant['inventoryItem'].items() if v is not None}
    p['productOptions'] = [{'name':'Title','position':1,'values':[{'name':'Default Title'}]}]
    p['variants'] = [variant]
    return {k:v for k,v in p.items() if v is not None}

def gql(session, query, variables):
    if not SHOP_DOMAIN or not TOKEN:
        raise RuntimeError('Missing SHOPIFY_SHOP_DOMAIN or SHOPIFY_ADMIN_ACCESS_TOKEN')
    url = f'https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/graphql.json'
    r = session.post(url, headers={'X-Shopify-Access-Token': TOKEN, 'Content-Type':'application/json'}, json={'query':query,'variables':variables}, timeout=(15,120))
    r.raise_for_status()
    data = r.json()
    if data.get('errors'):
        raise RuntimeError(json.dumps(data['errors'], ensure_ascii=False))
    return data.get('data') or {}

def iter_catalog():
    if not os.path.exists(CATALOG_PATH):
        return
    with open(CATALOG_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)

def run():
    started = time.time()
    status = {'state':'running','write_enabled':WRITE_ENABLED,'started_at':started,'created_or_updated':0,'dry_run':0,'skipped':0,'errors':[]}
    session = requests.Session()
    try:
        for item in iter_catalog() or []:
            try:
                product_input = build_product_input(item)
                handle = product_input['handle']
                if not WRITE_ENABLED:
                    status['dry_run'] += 1
                    continue
                payload = gql(session, PRODUCT_SET, {'identifier': {'handle': handle}, 'input': product_input})
                result = payload.get('productSet') or {}
                errs = result.get('userErrors') or []
                if errs:
                    raise RuntimeError(json.dumps(errs, ensure_ascii=False))
                if not result.get('product'):
                    raise RuntimeError('Shopify returned no product')
                status['created_or_updated'] += 1
                time.sleep(REQUEST_DELAY)
            except Exception as e:
                status['errors'].append({'key':item.get('key'),'supplier':item.get('supplier'),'error':f'{type(e).__name__}: {e}'})
        status['state'] = 'completed' if not status['errors'] else 'completed_with_errors'
    except Exception as e:
        status['state'] = 'failed'
        status['errors'].append({'error':f'{type(e).__name__}: {e}'})
    status['finished_at'] = time.time()
    status['duration_seconds'] = round(status['finished_at'] - started, 2)
    with open(SYNC_STATUS_PATH, 'w', encoding='utf-8') as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    print(json.dumps(status, ensure_ascii=False))
    return status

if __name__ == '__main__':
    run()
