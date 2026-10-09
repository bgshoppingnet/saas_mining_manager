import os, json, time, hashlib
from collections import defaultdict
import requests
import bulk_shopify_sync as core

CATALOG_PATH = os.getenv('SUPPLIER_OUT_PATH', '/tmp/supplier-catalog.jsonl')
LOCATION_ID = os.getenv('SHOPIFY_LOCATION_ID', '').strip()
CYCLE_SECONDS = int(os.getenv('SHOPIFY_INVENTORY_CYCLE_SECONDS', '14400') or '14400')


def norm(v):
    return ' '.join(str(v or '').split())


def low(v):
    return norm(v).lower()


def safe_int(v):
    try:
        if v is None or str(v).strip() == '':
            return None
        return max(0, int(float(str(v).strip().replace(',', '.'))))
    except Exception:
        import re
        m = re.search(r'-?\d+', str(v or ''))
        return max(0, int(m.group(0))) if m else None


def load_lorelli():
    rows = []
    with open(CATALOG_PATH, encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            x = json.loads(line)
            if low(x.get('supplier')) == 'lorelli':
                rows.append(x)
    return rows


def cycle_id():
    return str(int(time.time() // CYCLE_SECONDS))


def index_name(cycle):
    return 'BGSLorelliInventoryIndex' + cycle


def mutation_name(cycle):
    return 'BGSLorelliInventorySet' + cycle


def index_query(name):
    return f'''query {name} {{
  productVariants {{
    edges {{
      node {{
        __typename
        id
        sku
        barcode
        inventoryItem {{ id }}
        product {{ id handle }}
      }}
    }}
  }}
}}'''


def parse_index(url):
    r = requests.get(url, timeout=(20, 180))
    r.raise_for_status()
    variants = {}
    children = defaultdict(list)
    for line in r.text.splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        oid = str(o.get('id') or '')
        parent = o.get('__parentId')
        if oid.startswith('gid://shopify/ProductVariant/'):
            variants[oid] = {
                'id': oid,
                'sku': norm(o.get('sku')),
                'barcode': norm(o.get('barcode')),
                'inventoryItemId': ((o.get('inventoryItem') or {}).get('id') if isinstance(o.get('inventoryItem'), dict) else None),
                'productId': ((o.get('product') or {}).get('id') if isinstance(o.get('product'), dict) else None),
                'handle': ((o.get('product') or {}).get('handle') if isinstance(o.get('product'), dict) else '') or '',
            }
        elif parent:
            children[parent].append(o)
    for vid, v in variants.items():
        for o in children.get(vid, []):
            oid = str(o.get('id') or '')
            if oid.startswith('gid://shopify/InventoryItem/'):
                v['inventoryItemId'] = oid
            elif oid.startswith('gid://shopify/Product/'):
                v['productId'] = oid
                v['handle'] = o.get('handle') or v.get('handle') or ''
    return list(variants.values())


def ensure_index(session, name, status, deadline):
    op = core.recent(session, name, 'query')
    if not op or str(op.get('status')) in ('FAILED', 'CANCELED', 'EXPIRED'):
        out = core.gql(session, core.START_QUERY, {'query': index_query(name)}).get('bulkOperationRunQuery') or {}
        if out.get('userErrors'):
            raise RuntimeError(str(out['userErrors']))
        op = out.get('bulkOperation') or {}
    op = core.wait(session, op, status, deadline)
    if str((op or {}).get('status')) != 'COMPLETED' or not op.get('url'):
        return None, op
    return parse_index(op['url']), op


def choose(cands):
    if not cands:
        return None, False
    unique = {x['id'] for x in cands}
    if len(unique) == 1:
        return cands[0], False
    canonical = [x for x in cands if str(x.get('handle') or '').lower().startswith('lorelli-')]
    if len({x['id'] for x in canonical}) == 1:
        return canonical[0], False
    return None, True


def build_indexes(variants):
    sku = defaultdict(list)
    ean = defaultdict(list)
    for v in variants:
        if low(v.get('sku')):
            sku[low(v['sku'])].append(v)
        if low(v.get('barcode')):
            ean[low(v['barcode'])].append(v)
    return sku, ean


def match(item, indexes):
    sku_idx, ean_idx = indexes
    s = low(item.get('sku'))
    e = low(item.get('ean'))
    all_cands = []
    if s:
        all_cands.extend(sku_idx.get(s, []))
    if e:
        all_cands.extend(ean_idx.get(e, []))
    dedup = {x['id']: x for x in all_cands}
    return choose(list(dedup.values()))


def inventory_mutation(name):
    return f'''mutation {name}($input: InventorySetQuantitiesInput!, $idempotencyKey: String!) {{
  inventorySetQuantities(input: $input) @idempotent(key: $idempotencyKey) {{
    inventoryAdjustmentGroup {{ createdAt }}
    userErrors {{ field message }}
  }}
}}'''


def run():
    started = time.time()
    deadline = started + int(os.getenv('SHOPIFY_BULK_MAX_WAIT_SECONDS', '680') or '680')
    cycle = cycle_id()
    status = {
        'state': 'running', 'supplier': 'Lorelli', 'phase': 'inventory_index',
        'cycle': cycle, 'total': 0, 'matched': 0, 'queued': 0,
        'conflicts': 0, 'missing_inventory_item': 0, 'no_quantity': 0,
        'failed': 0, 'errors': [], 'started_at': started,
    }
    core.write_status(status)
    if not LOCATION_ID:
        status.update(state='failed', fatal_error='Missing SHOPIFY_LOCATION_ID')
        core.write_status(status)
        return status
    try:
        rows = load_lorelli()
        status['total'] = len(rows)
        with requests.Session() as session:
            variants, op = ensure_index(session, index_name(cycle), status, deadline)
            if variants is None:
                status.update(state='checkpoint_wait', phase='inventory_index')
                core.write_status(status)
                return status
            indexes = build_indexes(variants)
            quantities = []
            for item in rows:
                q = safe_int(item.get('quantity'))
                if q is None:
                    status['no_quantity'] += 1
                    continue
                v, conflict = match(item, indexes)
                if conflict:
                    status['conflicts'] += 1
                    continue
                if not v:
                    continue
                status['matched'] += 1
                iid = v.get('inventoryItemId')
                if not iid:
                    status['missing_inventory_item'] += 1
                    continue
                quantities.append({'inventoryItemId': iid, 'locationId': LOCATION_ID, 'quantity': q, 'changeFromQuantity': None})

            # De-duplicate by inventory item. The same desired value must win deterministically.
            by_iid = {}
            for x in quantities:
                by_iid[x['inventoryItemId']] = x
            quantities = list(by_iid.values())
            status['queued'] = len(quantities)
            status['phase'] = 'inventory_write'
            core.write_status(status)

            rows_jsonl = []
            for n in range(0, len(quantities), 200):
                chunk = quantities[n:n+200]
                raw = cycle + json.dumps(chunk, sort_keys=True, separators=(',', ':'))
                idem = hashlib.sha256(raw.encode()).hexdigest()
                rows_jsonl.append({
                    'input': {
                        'name': 'available',
                        'reason': 'correction',
                        'referenceDocumentUri': 'gid://bgshopping/LorelliInventory/' + cycle,
                        'quantities': chunk,
                    },
                    'idempotencyKey': idem,
                })

            name = mutation_name(cycle)
            op, errors = core.run_bulk(session, name, inventory_mutation(name), rows_jsonl, status, deadline)
            if str((op or {}).get('status')) != 'COMPLETED':
                status.update(state='checkpoint_wait', phase='inventory_write')
                core.write_status(status)
                return status
            if errors:
                status['failed'] = len(errors)
                status['errors'] = errors
                status['state'] = 'completed_with_errors'
            else:
                status['state'] = 'completed'
            status['phase'] = 'done'
            status['finished_at'] = time.time()
            status['duration_seconds'] = round(status['finished_at'] - started, 2)
            core.write_status(status)
            return status
    except Exception as e:
        status.update(state='failed', fatal_error=f'{type(e).__name__}: {e}', finished_at=time.time())
        status['duration_seconds'] = round(status['finished_at'] - started, 2)
        core.write_status(status)
        return status


if __name__ == '__main__':
    run()
