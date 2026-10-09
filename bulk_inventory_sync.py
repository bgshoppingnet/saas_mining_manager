import os, json, time, hashlib, re
from collections import defaultdict, Counter
import xml.etree.ElementTree as ET
import requests
import bulk_shopify_sync as core
import supplier_worker as sw

CATALOG_PATH = os.getenv('SUPPLIER_OUT_PATH', '/tmp/supplier-catalog.jsonl')
LOCATION_ID = os.getenv('SHOPIFY_LOCATION_ID', '').strip()
CYCLE_SECONDS = int(os.getenv('SHOPIFY_INVENTORY_CYCLE_SECONDS', '14400') or '14400')
LORELLI_URL = os.getenv('LORELLI_FEED_URL', sw.DEFAULT_FEEDS[0]['url'])

STOCK_FIELDS = {
    'quantity','qty','stock','stock_quantity','stock_amount','onstock','on_stock',
    'instock','in_stock','inventory_quantity','available_quantity','free_stock',
    'warehouse_stock','stocklevel','stock_level'
}
AVAIL_FIELDS = {
    'availability','availability_status','available','is_available','instock','in_stock','onstock'
}
IN_STATUS = {
    'true','yes','y','1','in stock','instock','available','available for order',
    'да','наличен','налично','в наличност','на склад','има'
}
OUT_STATUS = {
    'false','no','n','0','out of stock','outofstock','unavailable','sold out',
    'не','изчерпан','изчерпано','няма','няма наличност'
}


def norm(v):
    return ' '.join(str(v or '').split())


def low(v):
    return norm(v).lower()


def safe_int(v):
    s = norm(v).replace(',', '.')
    if not s:
        return None
    try:
        return max(0, int(float(s)))
    except Exception:
        m = re.search(r'-?\d+', s)
        return max(0, int(m.group(0))) if m else None


def status_qty(v):
    s = low(v)
    if s in IN_STATUS:
        return 5
    if s in OUT_STATUS:
        return 0
    if any(x in s for x in ('out of stock','изчерпан','няма наличност','unavailable')):
        return 0
    if any(x in s for x in ('in stock','в наличност','на склад','available')):
        return 5
    return None


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


def derive_quantity_map():
    r = requests.get(LORELLI_URL, timeout=(15, 180), allow_redirects=True, headers={'User-Agent':'BGShopping-Inventory-Probe/1.0'})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    by_key = {}
    field_counts = Counter()
    numeric_count = 0
    status_count = 0
    for node in sw.descendants_with_product_shape(root):
        sku = sw.text(node, {'sku','pnumber','code','product_code','item_code','model','model_code','catalog_number'})
        ean = sw.text(node, {'ean','barcode','gtin','ean13','upc'})
        ext_id = sw.text(node, {'id','product_id','item_id','offer_id'})
        keys = [low(x) for x in (sku, ean, ext_id) if low(x)]
        if not keys:
            continue
        q = None
        # Prefer explicit numeric stock/quantity fields.
        for child in node.iter():
            if child is node:
                continue
            tag = sw.tag_name(child)
            if tag in STOCK_FIELDS and child.text:
                field_counts[tag] += 1
                val = safe_int(child.text)
                if val is not None:
                    q = val
                    numeric_count += 1
                    break
            for attr_name, attr_value in child.attrib.items():
                an = str(attr_name).lower().replace('-', '_')
                if an in STOCK_FIELDS:
                    field_counts['@'+an] += 1
                    val = safe_int(attr_value)
                    if val is not None:
                        q = val
                        numeric_count += 1
                        break
            if q is not None:
                break
        # If exact quantity is absent, map explicit availability state to 5/0.
        if q is None:
            for child in node.iter():
                if child is node:
                    continue
                tag = sw.tag_name(child)
                if tag in AVAIL_FIELDS and child.text:
                    field_counts[tag] += 1
                    val = status_qty(child.text)
                    if val is not None:
                        q = val
                        status_count += 1
                        break
                for attr_name, attr_value in child.attrib.items():
                    an = str(attr_name).lower().replace('-', '_')
                    if an in AVAIL_FIELDS:
                        field_counts['@'+an] += 1
                        val = status_qty(attr_value)
                        if val is not None:
                            q = val
                            status_count += 1
                            break
                if q is not None:
                    break
        if q is not None:
            for k in keys:
                by_key[k] = q
    return by_key, {
        'source_bytes': len(r.content),
        'mapped_keys': len(by_key),
        'numeric_products': numeric_count,
        'status_products': status_count,
        'candidate_fields': dict(field_counts.most_common(20)),
    }


def desired_qty(item, qmap):
    q = safe_int(item.get('quantity'))
    if q is not None:
        return q
    for value in (item.get('sku'), item.get('ean'), item.get('external_id')):
        k = low(value)
        if k and k in qmap:
            return qmap[k]
    return None


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
        'state': 'running', 'supplier': 'Lorelli', 'phase': 'inventory_source',
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
        qmap, source_stats = derive_quantity_map()
        status['source'] = source_stats
        status['phase'] = 'inventory_index'
        core.write_status(status)
        with requests.Session() as session:
            variants, op = ensure_index(session, index_name(cycle), status, deadline)
            if variants is None:
                status.update(state='checkpoint_wait', phase='inventory_index')
                core.write_status(status)
                return status
            indexes = build_indexes(variants)
            quantities = []
            for item in rows:
                q = desired_qty(item, qmap)
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

            by_iid = {x['inventoryItemId']: x for x in quantities}
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
