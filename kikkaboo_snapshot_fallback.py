import os
import json
import struct
from io import BytesIO
import requests
from PIL import Image

CATALOG_PATH = os.getenv('SUPPLIER_OUT_PATH', '/tmp/supplier-catalog.jsonl')
PAYLOAD_URL = os.getenv('KIKKABOO_SNAPSHOT_IMAGE_URL', 'https://cdn.shopify.com/s/files/1/1044/1844/3609/files/BGShopping-KikkaBoo-catalog-payload.png?v=1791512718')


def load_rows():
    response = requests.get(PAYLOAD_URL, timeout=(15, 120))
    response.raise_for_status()
    raw = Image.open(BytesIO(response.content)).convert('RGB').tobytes()
    size = struct.unpack('>I', raw[:4])[0]
    text = raw[4:4 + size].decode('utf-8')
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def inject_if_missing():
    other_rows = []
    existing = 0
    if os.path.exists(CATALOG_PATH):
        with open(CATALOG_PATH, encoding='utf-8') as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if str(row.get('supplier') or '').strip().lower() == 'kikkaboo':
                    existing += 1
                else:
                    other_rows.append(row)
    if existing:
        return {'injected': 0, 'reason': 'already_present', 'existing_kikkaboo': existing}
    rows = load_rows()
    temp_path = CATALOG_PATH + '.kikkaboo.tmp'
    with open(temp_path, 'w', encoding='utf-8') as handle:
        for row in other_rows + rows:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    os.replace(temp_path, CATALOG_PATH)
    return {'injected': len(rows), 'reason': 'shopify_cdn_snapshot'}
