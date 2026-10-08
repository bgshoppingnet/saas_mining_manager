import json, os, time
import requests
from supplier_worker import run as run_suppliers

STATUS_PATH = os.getenv('CATALOG_SYNC_STATUS_PATH', '/tmp/catalog-sync-status.json')


def write_status(data):
    tmp = STATUS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATUS_PATH)


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


def run():
    started = time.time()
    result = {
        'state': 'running',
        'mode': 'shopify_bulk',
        'started_at': started,
        'suppliers': None,
        'shopify_auth': None,
        'shopify': None,
        'fatal_error': None,
    }
    write_status(result)
    try:
        # Parse the full supplier catalog first. Do not apply the legacy global
        # image filter here: existing products without images still need price,
        # stock and metadata updates. Creation safety is handled by the bulk sync.
        result['suppliers'] = run_suppliers()
        write_status(result)

        supplier_state = (result['suppliers'] or {}).get('state', '')
        if supplier_state == 'failed':
            raise RuntimeError('Supplier parse failed')

        result['shopify_auth'] = ensure_shopify_access_token()
        write_status(result)

        # Import after auth so module-level credentials see the fresh token.
        import importlib
        import bulk_shopify_sync
        bulk_shopify_sync = importlib.reload(bulk_shopify_sync)
        bulk_shopify_sync.TOKEN = os.environ.get('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()
        bulk_shopify_sync.SHOP = os.environ.get('SHOPIFY_SHOP_DOMAIN', '').strip()

        result['shopify'] = bulk_shopify_sync.run()
        shopify_state = (result['shopify'] or {}).get('state', '')

        if shopify_state == 'failed':
            result['state'] = 'failed'
        elif shopify_state == 'checkpoint_wait':
            result['state'] = 'checkpoint_wait'
        elif shopify_state == 'completed_with_errors' or 'errors' in supplier_state:
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
