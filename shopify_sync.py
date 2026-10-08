import os, json, re, time, html, hashlib
from decimal import Decimal, InvalidOperation
import requests

CATALOG_PATH = os.getenv('SUPPLIER_OUT_PATH', '/tmp/supplier-catalog.jsonl')
SYNC_STATUS_PATH = os.getenv('SHOPIFY_SYNC_STATUS_PATH', '/tmp/shopify-sync-status.json')
SHOP_DOMAIN = os.getenv('SHOPIFY_SHOP_DOMAIN', '').strip()
TOKEN = os.getenv('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()
API_VERSION = os.getenv('SHOPIFY_API_VERSION', '2026-10').strip()
WRITE_ENABLED = os.getenv('SHOPIFY_WRITE_ENABLED', 'false').lower() in ('1','true','yes','on')
LOCATION_ID = os.getenv('SHOPIFY_LOCATION_ID', '').strip()
PRIMARY_COUNTRY_CODE = os.getenv('SHOPIFY_PRIMARY_COUNTRY_CODE', 'BG').strip().upper()
PRIMARY_MARKET = os.getenv('SHOPIFY_PRIMARY_MARKET', 'Bulgaria').strip()
SECONDARY_MARKETS = os.getenv('SHOPIFY_SECONDARY_MARKETS', 'European Union,United Kingdom').strip()
DEFAULT_VENDOR = os.getenv('SHOPIFY_DEFAULT_VENDOR', '').strip()
NEW_PRODUCT_STATUS = os.getenv('SHOPIFY_NEW_PRODUCT_STATUS', 'ACTIVE').strip().upper()
INVENTORY_POLICY = os.getenv('SHOPIFY_INVENTORY_POLICY', 'CONTINUE').strip().upper()
IN_STOCK_QUANTITY = int(os.getenv('IN_STOCK_QUANTITY', '5'))
REQUEST_DELAY = float(os.getenv('SHOPIFY_REQUEST_DELAY', '0.15'))
FIX_HANDLES = os.getenv('SHOPIFY_FIX_HANDLES', 'false').lower() in ('1','true','yes','on')
ADD_IMAGES = os.getenv('SHOPIFY_ADD_IMAGES', 'true').lower() in ('1','true','yes','on')

FIND_VARIANTS = '''
query FindVariants($query: String!) {
  productVariants(first: 10, query: $query) {
    nodes {
      id sku barcode price
      inventoryItem { id tracked }
      product {
        id title handle vendor
        metafield(namespace: "supplier_sync", key: "source_image_url") { value }
      }
    }
  }
}
'''

LOCATIONS_QUERY = '''
query BulgarianLocations($first: Int!, $query: String!) {
  locations(first: $first, query: $query) {
    nodes {
      id name isActive fulfillsOnlineOrders hasActiveInventory
      address { city country countryCode province zip }
    }
  }
}
'''

PRODUCT_UPDATE = '''
mutation UpdateProduct($product: ProductUpdateInput!, $media: [CreateMediaInput!]) {
  productUpdate(product: $product, media: $media) {
    product { id handle title }
    userErrors { field message }
  }
}
'''

VARIANT_UPDATE = '''
mutation UpdateVariant($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants, allowPartialUpdates: false) {
    productVariants { id sku barcode price }
    userErrors { field message }
  }
}
'''

INVENTORY_SET = '''
mutation SetInventory($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { createdAt reason referenceDocumentUri }
    userErrors { field message }
  }
}
'''

PRODUCT_SET = '''
mutation CreateProduct($input: ProductSetInput!) {
  productSet(input: $input, synchronous: true) {
    product {
      id handle title
      variants(first: 2) { nodes { id inventoryItem { id tracked } } }
    }
    userErrors { field message }
  }
}
'''


def clean_text(v):
    v = html.unescape(str(v or ''))
    v = re.sub(r'<[^>]+>', ' ', v)
    return ' '.join(v.split())


def escape_search(v):
    return clean_text(v).replace('\\', '\\\\').replace('"', '\\"')


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


def quantity(v):
    if v is None or str(v).strip() == '':
        return None
    raw = clean_text(v).lower()
    if raw in {'true','yes','in stock','instock','available','наличен','в наличност','да'}:
        return IN_STOCK_QUANTITY
    if raw in {'false','no','out of stock','outofstock','unavailable','изчерпан','няма','не'}:
        return 0
    m = re.search(r'-?\d+', raw)
    if not m:
        return None
    return max(0, int(m.group(0)))


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
    return (base + ' Поръчайте онлайн от BGShopping.net. Доставка в България и Европа.')[:320]


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
    parts.append('<p>Основен пазар: България. Доставка и продажби към клиенти в България и Европа.</p>')
    return ''.join(parts)


def generated_handle(item):
    key = clean_text(item.get('ean') or item.get('sku') or item.get('external_id') or item.get('key'))
    supplier = clean_text(item.get('supplier'))
    return slugify(f'{supplier}-{key}')


def gql(session, query, variables):
    if not SHOP_DOMAIN or not TOKEN:
        raise RuntimeError('Missing SHOPIFY_SHOP_DOMAIN or SHOPIFY_ADMIN_ACCESS_TOKEN')
    url = f'https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/graphql.json'
    r = session.post(
        url,
        headers={'X-Shopify-Access-Token': TOKEN, 'Content-Type':'application/json'},
        json={'query':query,'variables':variables},
        timeout=(15,120),
    )
    r.raise_for_status()
    data = r.json()
    if data.get('errors'):
        raise RuntimeError(json.dumps(data['errors'], ensure_ascii=False))
    return data.get('data') or {}


def user_errors(result):
    return result.get('userErrors') or []


def discover_primary_locations(session):
    query = f'country:{PRIMARY_COUNTRY_CODE} active:true'
    data = gql(session, LOCATIONS_QUERY, {'first': 50, 'query': query})
    nodes = ((data.get('locations') or {}).get('nodes') or [])
    nodes = [x for x in nodes if x.get('isActive') and x.get('fulfillsOnlineOrders')]
    if LOCATION_ID:
        nodes.sort(key=lambda x: 0 if x.get('id') == LOCATION_ID else 1)
    return nodes


def exact_matches(nodes, item):
    sku = clean_text(item.get('sku')).lower()
    ean = clean_text(item.get('ean')).lower()
    out = []
    for n in nodes:
        nsku = clean_text(n.get('sku')).lower()
        nean = clean_text(n.get('barcode')).lower()
        sku_ok = bool(sku and nsku == sku)
        ean_ok = bool(ean and nean == ean)
        if sku_ok or ean_ok:
            if sku and ean and nsku and nean and not (nsku == sku and nean == ean):
                continue
            out.append(n)
    return out


def find_existing(session, item):
    all_nodes = []
    sku = clean_text(item.get('sku'))
    ean = clean_text(item.get('ean'))
    for field, value in (('sku', sku), ('barcode', ean)):
        if not value:
            continue
        data = gql(session, FIND_VARIANTS, {'query': f'{field}:"{escape_search(value)}"'})
        for node in (data.get('productVariants') or {}).get('nodes') or []:
            if node.get('id') not in {x.get('id') for x in all_nodes}:
                all_nodes.append(node)
    matches = exact_matches(all_nodes, item)
    if len(matches) > 1:
        raise RuntimeError('Ambiguous Shopify match: multiple variants share this SKU/EAN')
    return matches[0] if matches else None


def source_image(item):
    image = str(item.get('image') or '').strip()
    return image if image.startswith(('http://','https://')) else ''


def geo_metafields():
    return [
        {'namespace':'supplier_sync','key':'primary_market','type':'single_line_text_field','value':PRIMARY_MARKET},
        {'namespace':'supplier_sync','key':'secondary_markets','type':'single_line_text_field','value':SECONDARY_MARKETS},
    ]


def product_update_input(item, existing):
    product = existing['product']
    p = {
        'id': product['id'],
        'title': (clean_text(item.get('name')) or product.get('title') or '')[:255],
        'descriptionHtml': product_description(item),
        'vendor': clean_text(item.get('supplier')) or DEFAULT_VENDOR or product.get('vendor') or None,
        'seo': {'title': seo_title(item), 'description': seo_description(item)},
        'metafields': [
            {'namespace':'supplier_sync','key':'supplier','type':'single_line_text_field','value':clean_text(item.get('supplier')) or 'unknown'},
            {'namespace':'supplier_sync','key':'source_key','type':'single_line_text_field','value':clean_text(item.get('key')) or generated_handle(item)},
        ] + geo_metafields(),
    }
    if FIX_HANDLES:
        handle = generated_handle(item)
        if handle and handle != product.get('handle'):
            p['handle'] = handle
            p['redirectNewHandle'] = True
    image = source_image(item)
    old_image = (((product.get('metafield') or {}).get('value')) or '').strip()
    media = []
    if ADD_IMAGES and image and image != old_image:
        media = [{'mediaContentType':'IMAGE','originalSource':image,'alt':(clean_text(item.get('name')) or product.get('title') or '')[:512]}]
        p['metafields'].append({'namespace':'supplier_sync','key':'source_image_url','type':'url','value':image})
    return {k:v for k,v in p.items() if v is not None}, media


def variant_update_input(item, existing):
    v = {'id': existing['id'], 'inventoryPolicy': INVENTORY_POLICY}
    price = money(item.get('price'))
    if price is not None:
        v['price'] = price
    sku = clean_text(item.get('sku'))
    ean = clean_text(item.get('ean'))
    inv = {'tracked': True, 'requiresShipping': True}
    if sku:
        inv['sku'] = sku
    v['inventoryItem'] = inv
    if ean:
        v['barcode'] = ean
    return v


def set_inventory(session, item, inventory_item_id, locations):
    qty = quantity(item.get('quantity'))
    if qty is None or not inventory_item_id or not locations:
        return 0
    # Supplier feeds normally expose one aggregate stock value. Writing it to every
    # location would multiply total stock, so aggregate stock is assigned to the
    # preferred/first Bulgarian fulfillment point. Per-location feed data can extend this later.
    target = locations[0]
    location_id = target['id']
    token = clean_text(item.get('key') or item.get('sku') or item.get('ean') or inventory_item_id)
    idem = hashlib.sha256(f'{token}|{location_id}|{qty}'.encode('utf-8')).hexdigest()
    payload = {
        'name': 'available',
        'reason': 'correction',
        'referenceDocumentUri': f'gid://bgshopping-catalog-sync/SupplierSync/{idem[:24]}',
        'quantities': [{
            'inventoryItemId': inventory_item_id,
            'locationId': location_id,
            'quantity': qty,
            'changeFromQuantity': None,
        }],
    }
    data = gql(session, INVENTORY_SET, {'input': payload})
    result = data.get('inventorySetQuantities') or {}
    if user_errors(result):
        raise RuntimeError(json.dumps(user_errors(result), ensure_ascii=False))
    return 1


def build_new_product_input(item):
    name = clean_text(item.get('name')) or clean_text(item.get('sku')) or clean_text(item.get('ean'))
    if not name:
        raise ValueError('Missing product name/SKU/EAN')
    handle = generated_handle(item)
    p = {
        'title': name[:255],
        'handle': handle,
        'descriptionHtml': product_description(item),
        'vendor': clean_text(item.get('supplier')) or DEFAULT_VENDOR or None,
        'status': NEW_PRODUCT_STATUS,
        'seo': {'title': seo_title(item), 'description': seo_description(item)},
        'metafields': [
            {'namespace':'supplier_sync','key':'supplier','type':'single_line_text_field','value':clean_text(item.get('supplier')) or 'unknown'},
            {'namespace':'supplier_sync','key':'source_key','type':'single_line_text_field','value':clean_text(item.get('key')) or handle},
        ] + geo_metafields(),
        'productOptions': [{'name':'Title','position':1,'values':[{'name':'Default Title'}]}],
    }
    image = source_image(item)
    if image and ADD_IMAGES:
        p['files'] = [{'originalSource':image,'contentType':'IMAGE','alt':name[:512],'duplicateResolutionMode':'APPEND_UUID'}]
        p['metafields'].append({'namespace':'supplier_sync','key':'source_image_url','type':'url','value':image})
    variant = {
        'optionValues':[{'optionName':'Title','name':'Default Title'}],
        'inventoryItem': {'tracked':True,'requiresShipping':True},
        'inventoryPolicy': INVENTORY_POLICY,
    }
    sku = clean_text(item.get('sku'))
    ean = clean_text(item.get('ean'))
    if sku: variant['inventoryItem']['sku'] = sku
    if ean: variant['barcode'] = ean
    price = money(item.get('price'))
    if price is not None: variant['price'] = price
    p['variants'] = [variant]
    return {k:v for k,v in p.items() if v is not None}


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
    status = {
        'state':'running','write_enabled':WRITE_ENABLED,'started_at':started,
        'primary_market':PRIMARY_MARKET,'secondary_markets':SECONDARY_MARKETS,
        'primary_country_code':PRIMARY_COUNTRY_CODE,'locations':[],
        'matched':0,'updated':0,'created':0,'inventory_updated':0,
        'dry_run':0,'skipped':0,'errors':[],
    }
    session = requests.Session()
    try:
        locations = discover_primary_locations(session) if TOKEN and SHOP_DOMAIN else []
        status['locations'] = [
            {
                'id':x.get('id'),'name':x.get('name'),
                'city':((x.get('address') or {}).get('city')),
                'countryCode':((x.get('address') or {}).get('countryCode')),
                'fulfillsOnlineOrders':x.get('fulfillsOnlineOrders'),
            }
            for x in locations
        ]
        if WRITE_ENABLED and not locations:
            raise RuntimeError(f'No active fulfillment locations found for {PRIMARY_COUNTRY_CODE}')
        for item in iter_catalog() or []:
            try:
                if not clean_text(item.get('sku')) and not clean_text(item.get('ean')):
                    status['skipped'] += 1
                    continue
                if not WRITE_ENABLED:
                    status['dry_run'] += 1
                    continue
                existing = find_existing(session, item)
                if existing:
                    status['matched'] += 1
                    pinput, media = product_update_input(item, existing)
                    pdata = gql(session, PRODUCT_UPDATE, {'product':pinput, 'media':media or None})
                    presult = pdata.get('productUpdate') or {}
                    if user_errors(presult):
                        raise RuntimeError(json.dumps(user_errors(presult), ensure_ascii=False))
                    vinput = variant_update_input(item, existing)
                    vdata = gql(session, VARIANT_UPDATE, {'productId':existing['product']['id'], 'variants':[vinput]})
                    vresult = vdata.get('productVariantsBulkUpdate') or {}
                    if user_errors(vresult):
                        raise RuntimeError(json.dumps(user_errors(vresult), ensure_ascii=False))
                    status['updated'] += 1
                    status['inventory_updated'] += set_inventory(session, item, (existing.get('inventoryItem') or {}).get('id'), locations)
                else:
                    new_input = build_new_product_input(item)
                    ndata = gql(session, PRODUCT_SET, {'input':new_input})
                    nresult = ndata.get('productSet') or {}
                    if user_errors(nresult):
                        raise RuntimeError(json.dumps(user_errors(nresult), ensure_ascii=False))
                    product = nresult.get('product') or {}
                    if not product.get('id'):
                        raise RuntimeError('Shopify returned no product after create')
                    status['created'] += 1
                    nodes = ((product.get('variants') or {}).get('nodes') or [])
                    inventory_item_id = ((nodes[0].get('inventoryItem') or {}).get('id')) if nodes else None
                    status['inventory_updated'] += set_inventory(session, item, inventory_item_id, locations)
                time.sleep(REQUEST_DELAY)
            except Exception as e:
                status['errors'].append({
                    'key':item.get('key'),'sku':item.get('sku'),'ean':item.get('ean'),
                    'supplier':item.get('supplier'),'error':f'{type(e).__name__}: {e}'
                })
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
