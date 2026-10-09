import os, json, gzip, base64, hashlib

CATALOG_PATH = os.getenv('SUPPLIER_OUT_PATH', '/tmp/supplier-catalog.jsonl')
PART_DIR = os.path.join(os.path.dirname(__file__), 'data', 'bge_min_parts')


def _fingerprint(item):
    return hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def load_snapshot_rows():
    if not os.path.isdir(PART_DIR):
        return []
    names = sorted(x for x in os.listdir(PART_DIR) if x.endswith('.b64'))
    if not names:
        return []
    encoded = ''.join(open(os.path.join(PART_DIR, n), encoding='ascii').read().strip() for n in names)
    raw = gzip.decompress(base64.b64decode(encoded)).decode('utf-8')
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        # Compact row: [id,name,EUR qty,image list,brand,category]
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
    rows = load_snapshot_rows()
    if not rows:
        return {'injected': 0, 'reason': 'snapshot_missing'}
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
    tmp = CATALOG_PATH + '.bge.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for obj in existing:
            f.write(json.dumps(obj, ensure_ascii=False) + '\n')
        for obj in rows:
            f.write(json.dumps(obj, ensure_ascii=False) + '\n')
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, CATALOG_PATH)
    return {'injected': len(rows), 'reason': 'embedded_snapshot'}
