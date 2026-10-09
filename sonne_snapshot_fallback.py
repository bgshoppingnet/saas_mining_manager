import os, json, hashlib, struct
from io import BytesIO
import requests
from PIL import Image

CATALOG_PATH = os.getenv('SUPPLIER_OUT_PATH', '/tmp/supplier-catalog.jsonl')
PAYLOAD_URL = os.getenv('SONNE_SNAPSHOT_IMAGE_URL', 'https://cdn.shopify.com/s/files/1/1044/1844/3609/files/BGShopping-Sonne-catalog-payload.png?v=1791510697')


def _fingerprint(item):
    return hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def load_snapshot_rows():
    r = requests.get(PAYLOAD_URL, timeout=(15, 120), headers={'User-Agent':'BGShopping-Supplier-Worker/6.0'})
    r.raise_for_status()
    im = Image.open(BytesIO(r.content)).convert('RGB')
    raw = im.tobytes()
    if len(raw) < 4:
        raise RuntimeError('Sonne snapshot image is empty')
    length = struct.unpack('>I', raw[:4])[0]
    if length <= 0 or length > len(raw) - 4:
        raise RuntimeError('Sonne snapshot image payload length is invalid')
    text = raw[4:4+length].decode('utf-8')
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        item['fingerprint'] = item.get('fingerprint') or _fingerprint(item)
        rows.append(item)
    return rows


def inject_if_missing():
    existing = []
    sonne_found = 0
    if os.path.exists(CATALOG_PATH):
        with open(CATALOG_PATH, encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                obj = json.loads(line)
                if str(obj.get('supplier') or '').strip().lower() == 'sonne':
                    sonne_found += 1
                else:
                    existing.append(obj)
    if sonne_found:
        return {'injected': 0, 'reason': 'live_feed_present', 'existing_sonne': sonne_found}
    rows = load_snapshot_rows()
    if not rows:
        return {'injected': 0, 'reason': 'snapshot_empty'}
    tmp = CATALOG_PATH + '.sonne.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for obj in existing:
            f.write(json.dumps(obj, ensure_ascii=False) + '\n')
        for obj in rows:
            f.write(json.dumps(obj, ensure_ascii=False) + '\n')
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, CATALOG_PATH)
    return {'injected': len(rows), 'reason': 'shopify_cdn_snapshot'}
