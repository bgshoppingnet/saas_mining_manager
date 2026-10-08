import os, json, time, hashlib, re
import requests
import xml.etree.ElementTree as ET

OUT_PATH = os.getenv('SUPPLIER_OUT_PATH', '/tmp/supplier-catalog.jsonl')
STATUS_PATH = os.getenv('SUPPLIER_STATUS_PATH', '/tmp/supplier-status.json')
TIMEOUT = (15, 180)

DEFAULT_FEEDS = [
    {"name": "Lorelli", "url": "https://lorelli.eu/ExportRssXmlFeed.aspx?token=38052958-16d5-43a7-9724-738c2550b7c1&lang=bg-bg", "enabled": True},
]

PRODUCT_HINTS = {
    'sku','pnumber','code','product_code','item_code','model','ean','barcode','gtin','ean13',
    'id','product_id','item_id','name','name_bg','title','product_name','price','price_with_tax',
    'retail_price','rrp_price','sale_price','quantity','qty','stock','availability','availability_status'
}


def feeds():
    raw = os.getenv('SUPPLIER_FEEDS_JSON', '').strip()
    if not raw:
        return DEFAULT_FEEDS
    data = json.loads(raw)
    return [x for x in data if x.get('enabled', True) and x.get('url')]


def tag_name(elem):
    return elem.tag.split('}')[-1].lower()


def text(node, names):
    names = {x.lower() for x in names}
    for child in list(node):
        if tag_name(child) in names and child.text:
            v = child.text.strip()
            if v:
                return v
    return ''


def all_values(node, names):
    names = {x.lower() for x in names}
    values = []
    for child in node.iter():
        if child is node:
            continue
        if tag_name(child) in names and child.text:
            v = child.text.strip()
            if v and v not in values:
                values.append(v)
    return values


def descendants_with_product_shape(root):
    candidates = []
    for node in root.iter():
        children = list(node)
        if not children:
            continue
        tags = {tag_name(c) for c in children}
        score = len(tags.intersection(PRODUCT_HINTS))
        if tag_name(node) in {'product','item','offer'} and score >= 2:
            candidates.append(node)
        elif score >= 3:
            candidates.append(node)
    return candidates


def normalize(node, supplier):
    sku = text(node, {'sku','pnumber','code','product_code','item_code','model','model_code','catalog_number'})
    ean = text(node, {'ean','barcode','gtin','ean13','upc'})
    ext_id = text(node, {'id','product_id','item_id','offer_id'})
    name = text(node, {'name','name_bg','title','product_name','title_bg'})
    price = text(node, {'price_with_tax','price','retail_price','rrp_price','sale_price','final_price','price_gross'})
    qty = text(node, {'quantity','qty','stock','availability','availability_status','available','stock_quantity'})
    url = text(node, {'url','link','product_url','product_link'})
    image = text(node, {'image','image_url','picture','photo','main_image','main_picture'})
    description = text(node, {'description','description_bg','body_html','long_description','short_description'})
    brand = text(node, {'brand','manufacturer','vendor','make'})
    category = text(node, {'category','category_bg','product_type','category_name'})
    currency = text(node, {'currency','currency_code'}) or 'EUR'
    images = all_values(node, {'image','image_url','picture','photo','main_image','main_picture','photos','image_1','image_2','image_3','image_4','image_5'})
    if image and image not in images:
        images.insert(0, image)
    key = ean or sku or ext_id or (name + '|' + supplier)
    if not key.strip():
        return None
    return {
        'supplier': supplier,
        'key': key.strip(),
        'sku': sku,
        'ean': ean,
        'external_id': ext_id,
        'name': name,
        'price': price,
        'currency': currency,
        'quantity': qty,
        'url': url,
        'image': image or (images[0] if images else ''),
        'images': images[:10],
        'description': description,
        'brand': brand,
        'category': category,
    }


def fetch_feed(session, cfg):
    r = session.get(cfg['url'], timeout=TIMEOUT, allow_redirects=True, headers={'User-Agent':'BGShopping-Supplier-Worker/2.0'})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    rows = []
    seen_node_ids = set()
    for node in descendants_with_product_shape(root):
        marker = id(node)
        if marker in seen_node_ids:
            continue
        seen_node_ids.add(marker)
        item = normalize(node, cfg['name'])
        if item:
            rows.append(item)
    return rows, len(r.content)


def stable_hash(item):
    payload = json.dumps(item, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def run():
    started = time.time()
    status = {'state':'running','started_at':started,'feeds':{},'unique_items':0,'duplicates':0,'errors':[]}
    session = requests.Session()
    merged = {}
    duplicates = 0
    for cfg in feeds():
        name = cfg['name']
        try:
            rows, size = fetch_feed(session, cfg)
            accepted = 0
            rejected = 0
            for item in rows:
                key = item['ean'] or item['sku'] or item['external_id'] or item['key']
                if not key or not key.strip():
                    rejected += 1
                    continue
                dedupe_key = key.strip().lower()
                if dedupe_key in merged:
                    duplicates += 1
                    continue
                item['fingerprint'] = stable_hash(item)
                merged[dedupe_key] = item
                accepted += 1
            status['feeds'][name] = {
                'ok':True,'parsed':len(rows),'accepted':accepted,'rejected':rejected,'bytes':size,
                'with_name':sum(1 for x in rows if x.get('name')),
                'with_image':sum(1 for x in rows if x.get('image')),
                'with_sku_or_ean':sum(1 for x in rows if x.get('sku') or x.get('ean')),
            }
        except Exception as e:
            status['feeds'][name] = {'ok':False,'error':f'{type(e).__name__}: {e}'}
            status['errors'].append({'supplier':name,'error':f'{type(e).__name__}: {e}'})
    tmp = OUT_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for item in merged.values():
            f.write(json.dumps(item, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, OUT_PATH)
    status['state'] = 'completed' if not status['errors'] else 'completed_with_errors'
    status['finished_at'] = time.time()
    status['duration_seconds'] = round(status['finished_at'] - started, 2)
    status['unique_items'] = len(merged)
    status['duplicates'] = duplicates
    with open(STATUS_PATH, 'w', encoding='utf-8') as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    print(json.dumps(status, ensure_ascii=False))
    return status


if __name__ == '__main__':
    run()
