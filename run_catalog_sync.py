import json, os, time
from supplier_worker import run as run_suppliers
import shopify_sync

STATUS_PATH = os.getenv('CATALOG_SYNC_STATUS_PATH', '/tmp/catalog-sync-status.json')


def write_status(data):
    tmp = STATUS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATUS_PATH)


def install_location_fallback():
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
        # Do not block product writes if Shopify location discovery is temporarily unavailable.
        # A synthetic marker lets the main sync continue; inventory writes are skipped below.
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
        'shopify': None,
        'fatal_error': None,
    }
    write_status(result)
    try:
        result['suppliers'] = run_suppliers()
        write_status(result)
        install_location_fallback()
        result['shopify'] = shopify_sync.run()
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
