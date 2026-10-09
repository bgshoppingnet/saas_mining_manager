import os, json, hashlib, struct
from io import BytesIO
import requests
from PIL import Image

CATALOG_PATH = os.getenv('SUPPLIER_OUT_PATH', '/tmp/supplier-catalog.jsonl')
PAYLOAD_URL = os.getenv('BGELECTRONICS_SNAPSHOT_IMAGE_URL', 'https://cdn.shopify.com/s/files/1/1044/1844/3609/files/BGShopping-BGElectronics-catalog-payload.png?v=1791510020')


def _fingerprint(item):
    return hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def load_snapshot_rows():
    r = requests.get(PAYLOAD_URL, timeout=(15, 120), headers={'User-Agent':'BGShopping-Supplier-Worker/6.0'})
    r.raise_for_status()
    im = Image.open(BytesIO(r.content)).convert('RGB')
    raw = im.tobytes()
    if len(raw) < 4:
        raise RuntimeError('BGE snapshot image is empty')
    length = struct.unpack('>I', raw[:4])[0]
    if length <= 0 or length > len(raw) - 4:
        raise RuntimeError('BGE snapshot image payload length is invalid')
    text = raw[4:4+length].decode('utf-8')
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        pid, name, price, qty, images, brand, category = json.loads(line)
        images = [str(x).strip() for x in (images or []) if str(x).strip()]
        item = {
            'supplier': 'BGElectronics',
            'key': str(pid),
            'sku': str(pid),
            'ean': '',
            'external_id': str(pid),
            'name': str(name or '').strip(),
            'price': str(price or '').strip(),
            'currency': 'EUR',
            'quantity': str(qty or '0'),
            'url': '',
            'image': images[0] if images else '',
            'images': images[:3],
            'description': '',
            'brand': str(brand or '').strip(),
            'category': str(category or '').strip(),
        }
        item['fingerprint'] = _fingerprint(item)
        rows.append(item)
    return rows


def inject_if_missing():
    existing = []
    bge_found = 0
    if os.path.exists(CATALOG_PATH):
        with open(CATALOG_PATH, encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                obj = json.loads(line)
                if str(obj.get('supplier') or '').strip().lower() == 'bgelectronics':
                    bge_found += 1
                else:
                    existing.append(obj)
    if bge_found:
        return {'injected': 0, 'reason': 'live_feed_present', 'existing_bge': bge_found}
    rows = load_snapshot_rows()
    if not rows:
        return {'injected': 0, 'reason': 'snapshot_empty'}
    tmp = CATALOG_PATH + '.bge.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for obj in existing:
            f.write(json.dumps(obj, ensure_ascii=False) + '\n')
        for obj in rows:
            f.write(json.dumps(obj, ensure_ascii=False) + '\n')
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, CATALOG_PATH)
    return {'injected': len(rows), 'reason': 'shopify_cdn_snapshot'}
