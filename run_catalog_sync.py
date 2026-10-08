import json, os, time, hashlib
import requests
from supplier_worker import run as run_suppliers

STATUS_PATH = os.getenv('CATALOG_SYNC_STATUS_PATH', '/tmp/catalog-sync-status.json')
CATALOG_PATH = os.getenv('SUPPLIER_OUT_PATH', '/tmp/supplier-catalog.jsonl')


def write_status(data):
    tmp = STATUS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATUS_PATH)


def filter_catalog_for_sync():
    require_image = os.getenv('SHOPIFY_REQUIRE_IMAGE', 'false').lower() in ('1','true','yes','on')
    max_items = int(os.getenv('SHOPIFY_MAX_ITEMS', '0') or '0')
    if not require_image and max_items <= 0:
        return {'enabled': False}
    if not os.path.exists(CATALOG_PATH):
        raise RuntimeError('Supplier catalog is missing before Shopify filter')

    kept = []
    total = 0
    skipped_no_image = 0
    with open(CATALOG_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            item = json.loads(line)
            image = str(item.get('image') or '').strip()
            if require_image and not image.startswith(('http://', 'https://')):
                skipped_no_image += 1
                continue
            kept.append(item)
            if max_items > 0 and len(kept) >= max_items:
                break

    tmp = CATALOG_PATH + '.filtered.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for item in kept:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CATALOG_PATH)
    return {
        'enabled': True,
        'require_image': require_image,
        'max_items': max_items,
        'scanned': total,
        'kept': len(kept),
        'skipped_no_image': skipped_no_image,
    }


def ensure_shopify_access_token():
    current = os.getenv('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()
    if current:
        return {'mode': 'existing_token'}

    shop_domain = os.getenv('SHOPIFY_SHOP_DOMAIN', '').strip()
    client_id = os.getenv('SHOPIFY_CLIENT_ID', '').strip()
    client_secret = os.getenv('SHOPIFY_CLIENT_SECRET', '').strip()
    missing = [
        name for name, value in (
            ('SHOPIFY_SHOP_DOMAIN', shop_domain),
            ('SHOPIFY_CLIENT_ID', client_id),
            ('SHOPIFY_CLIENT_SECRET', client_secret),
        ) if not value
    ]
    if missing:
        raise RuntimeError('Missing Shopify authentication settings: ' + ', '.join(missing))

    response = requests.post(
        f'https://{shop_domain}/admin/oauth/access_token',
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        data={
            'grant_type': 'client_credentials',
            'client_id': client_id,
            'client_secret': client_secret,
        },
        timeout=(15, 60),
    )
    response.raise_for_status()
    payload = response.json()
    token = str(payload.get('access_token') or '').strip()
    if not token:
        raise RuntimeError('Shopify did not return an access token')

    os.environ['SHOPIFY_ADMIN_ACCESS_TOKEN'] = token
    return {
        'mode': 'client_credentials',
        'expires_in': payload.get('expires_in'),
        'scope': payload.get('scope'),
    }


def install_shopify_api_compat(shopify_sync):
    shopify_sync.INVENTORY_SET = '''
mutation SetInventory($input: InventorySetQuantitiesInput!, $idempotencyKey: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $idempotencyKey) {
    inventoryAdjustmentGroup { createdAt reason referenceDocumentUri }
    userErrors { field message }
  }
}
'''
    original_gql = shopify_sync.gql

    def gql_compat(session, query, variables):
        variables = dict(variables or {})
        if 'inventorySetQuantities' in query and '@idempotent' in query and 'idempotencyKey' not in variables:
            raw = json.dumps(variables.get('input') or {}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            variables['idempotencyKey'] = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        return original_gql(session, query, variables)

    shopify_sync.gql = gql_compat
    return {'inventory_idempotency': True}


def install_canonical_duplicate_selection(shopify_sync):
    """When Lorelli EAN/SKU exists in both new canonical and legacy products, prefer exactly one lorelli-* product."""
    original_exact_matches = shopify_sync.exact_matches
    stats = {'resolved': 0, 'unresolved': 0}

    def exact_matches_prefer_canonical(nodes, item):
        matches = original_exact_matches(nodes, item)
        if len(matches) <= 1:
            return matches

        supplier = str(item.get('supplier') or '').strip().lower()
        if supplier == 'lorelli':
            preferred = [
                m for m in matches
                if str(((m.get('product') or {}).get('handle')) or '').strip().lower().startswith('lorelli-')
            ]
            if len(preferred) == 1:
                stats['resolved'] += 1
                return preferred

        stats['unresolved'] += 1
        return matches

    shopify_sync.exact_matches = exact_matches_prefer_canonical
    return stats


def install_location_fallback(shopify_sync):
    original_locations = shopify_sync.discover_primary_locations
    original_inventory = shopify_sync.set_inventory
    fallback_active = {'value': False}

    def discover_with_fallback(session):
        try:
            locations = original_locations(session)
        except Exception:
            locations = []
        if locations:
            fallback_active['value'] = False
            return locations
        location_id = os.getenv('SHOPIFY_LOCATION_ID', '').strip()
        if location_id:
            fallback_active['value'] = False
            return [{
                'id': location_id,
                'name': os.getenv('SHOPIFY_LOCATION_NAME', 'Bulgaria primary location'),
                'isActive': True,
                'fulfillsOnlineOrders': True,
                'hasActiveInventory': True,
                'address': {
                    'city': os.getenv('SHOPIFY_LOCATION_CITY', 'Plovdiv'),
                    'country': 'Bulgaria',
                    'countryCode': os.getenv('SHOPIFY_PRIMARY_COUNTRY_CODE', 'BG').strip().upper() or 'BG',
                    'province': None,
                    'zip': '',
                },
            }]
        fallback_active['value'] = True
        return [{
            'id': 'location-unavailable',
            'name': 'Inventory update pending',
            'isActive': True,
            'fulfillsOnlineOrders': True,
            'hasActiveInventory': False,
            'address': {
                'city': 'Plovdiv',
                'country': 'Bulgaria',
                'countryCode': 'BG',
                'province': None,
                'zip': '',
            },
        }]

    def inventory_with_fallback(session, item, inventory_item_id, locations):
        if fallback_active['value']:
            return 0
        return original_inventory(session, item, inventory_item_id, locations)

    shopify_sync.discover_primary_locations = discover_with_fallback
    shopify_sync.set_inventory = inventory_with_fallback


def run():
    started = time.time()
    result = {
        'state': 'running',
        'started_at': started,
        'suppliers': None,
        'catalog_filter': None,
        'shopify_auth': None,
        'shopify_compat': None,
        'duplicate_selection': None,
        'shopify': None,
        'fatal_error': None,
    }
    write_status(result)
    try:
        result['suppliers'] = run_suppliers()
        write_status(result)

        result['catalog_filter'] = filter_catalog_for_sync()
        write_status(result)

        result['shopify_auth'] = ensure_shopify_access_token()
        write_status(result)

        import shopify_sync
        result['shopify_compat'] = install_shopify_api_compat(shopify_sync)
        duplicate_stats = install_canonical_duplicate_selection(shopify_sync)
        result['duplicate_selection'] = duplicate_stats
        install_location_fallback(shopify_sync)
        result['shopify'] = shopify_sync.run()
        result['duplicate_selection'] = duplicate_stats

        supplier_state = (result['suppliers'] or {}).get('state', '')
        shopify_state = (result['shopify'] or {}).get('state', '')
        if supplier_state == 'failed' or shopify_state == 'failed':
            result['state'] = 'failed'
        elif 'errors' in supplier_state or 'errors' in shopify_state:
            result['state'] = 'completed_with_errors'
        else:
            result['state'] = 'completed'
    except Exception as e:
        result['state'] = 'failed'
        result['fatal_error'] = f'{type(e).__name__}: {e}'
    result['finished_at'] = time.time()
    result['duration_seconds'] = round(result['finished_at'] - started, 2)
    write_status(result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


if __name__ == '__main__':
    run()
