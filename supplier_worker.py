import os, json, time, hashlib
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

OUT_PATH = os.getenv('SUPPLIER_OUT_PATH', '/tmp/supplier-catalog.jsonl')
STATUS_PATH = os.getenv('SUPPLIER_STATUS_PATH', '/tmp/supplier-status.json')
TIMEOUT = (15, 120)

DEFAULT_FEEDS = [
    {"name": "Euromaster", "url": "https://api.euromasterbg.com/feeds/productfeedshops.xml", "enabled": True},
    {"name": "KikkaBoo", "url": "https://kikkaboo-b2b.com/media/feed/products-bg-online.xml", "enabled": True},
    {"name": "Lorelli", "url": "https://lorelli.eu/ExportRssXmlFeed.aspx?token=38052958-16d5-43a7-9724-738c2550b7c1&lang=bg-bg", "enabled": True},
]

def feeds():
    raw = os.getenv('SUPPLIER_FEEDS_JSON', '').strip()
    if not raw:
        return DEFAULT_FEEDS
    data = json.loads(raw)
    return [x for x in data if x.get('enabled', True) and x.get('url')]

def text(node, names):
    for child in list(node):
        tag = child.tag.split('}')[-1].lower()
        if tag in names and child.text:
            v = child.text.strip()
            if v:
                return v
    return ''

def descendants_with_product_shape(root):
    candidates = []
    for node in root.iter():
        children = list(node)
        if not children:
            continue
        tags = {c.tag.split('}')[-1].lower() for c in children}
        score = len(tags.intersection({'sku','ean','barcode','id','code','product_id','name','title','price','quantity','stock','availability'}))
        if score >= 3:
            candidates.append(node)
    return candidates

def normalize(node, supplier):
    sku = text(node, {'sku','code','product_code','item_code','model'})
    ean = text(node, {'ean','barcode','gtin','ean13'})
    ext_id = text(node, {'id','product_id','item_id'})
    name = text(node, {'name','title','product_name'})
    price = text(node, {'price','retail_price','rrp_price','sale_price'})
    qty = text(node, {'quantity','qty','stock','availability','available'})
    url = text(node, {'url','link','product_url'})
    image = text(node, {'image','image_url','picture','photo','main_image'})
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
        'quantity': qty,
        'url': url,
        'image': image,
    }

def fetch_feed(session, cfg):
    r = session.get(cfg['url'], timeout=TIMEOUT, allow_redirects=True, headers={'User-Agent':'BGShopping-Supplier-Worker/1.0'})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    rows = []
    for node in descendants_with_product_shape(root):
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
            for item in rows:
                key = item['ean'] or item['sku'] or item['external_id'] or item['key']
                dedupe_key = key.strip().lower()
                if dedupe_key in merged:
                    duplicates += 1
                    continue
                item['fingerprint'] = stable_hash(item)
                merged[dedupe_key] = item
                accepted += 1
            status['feeds'][name] = {'ok':True,'parsed':len(rows),'accepted':accepted,'bytes':size}
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
