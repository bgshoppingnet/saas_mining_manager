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
    missing = [name for name, value in (
        ('SHOPIFY_SHOP_DOMAIN', shop_domain), ('SHOPIFY_CLIENT_ID', client_id), ('SHOPIFY_CLIENT_SECRET', client_secret)
    ) if not value]
    if missing:
        raise RuntimeError('Missing Shopify authentication settings: ' + ', '.join(missing))
    response = requests.post(
        f'https://{shop_domain}/admin/oauth/access_token',
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        data={'grant_type':'client_credentials','client_id':client_id,'client_secret':client_secret},
        timeout=(15, 60),
    )
    response.raise_for_status()
    payload = response.json()
    token = str(payload.get('access_token') or '').strip()
    if not token:
        raise RuntimeError('Shopify did not return an access token')
    os.environ['SHOPIFY_ADMIN_ACCESS_TOKEN'] = token
    return {'mode':'client_credentials','expires_in':payload.get('expires_in'),'scope':payload.get('scope')}


def run():
    started = time.time()
    result = {
        'state':'running','mode':'shopify_bulk','started_at':started,
        'suppliers':None,'shopify_auth':None,
        'lorelli':None,'inventory':None,'duplicates':None,
        'euromaster':None,
        'bge_snapshot':None,'bgelectronics':None,
        'sonne_snapshot':None,'sonne':None,
        'promotions':None,'fatal_error':None,
    }
    write_status(result)
    try:
        result['suppliers'] = run_suppliers()
        write_status(result)
        supplier_state = (result['suppliers'] or {}).get('state', '')
        if supplier_state == 'failed':
            raise RuntimeError('Supplier parse failed')

        import bge_snapshot_fallback
        result['bge_snapshot'] = bge_snapshot_fallback.inject_if_missing()
        write_status(result)

        import sonne_snapshot_fallback
        result['sonne_snapshot'] = sonne_snapshot_fallback.inject_if_missing()
        write_status(result)

        result['shopify_auth'] = ensure_shopify_access_token()
        write_status(result)

        import importlib
        import bulk_shopify_sync
        import bulk_shopify_sync_patch
        import bulk_inventory_sync
        import bulk_duplicate_cleanup
        import bulk_euromaster_sync
        import bulk_euromaster_sync_patch
        import bulk_bgelectronics_sync
        import bulk_bgelectronics_patch
        import bulk_sonne_sync
        import bulk_clear_promotions

        bulk_shopify_sync = importlib.reload(bulk_shopify_sync)
        bulk_shopify_sync_patch = importlib.reload(bulk_shopify_sync_patch)
        bulk_shopify_sync_patch.install(bulk_shopify_sync)
        bulk_shopify_sync.TOKEN = os.environ.get('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()
        bulk_shopify_sync.SHOP = os.environ.get('SHOPIFY_SHOP_DOMAIN', '').strip()

        result['lorelli'] = bulk_shopify_sync.run()
        lorelli_state = (result['lorelli'] or {}).get('state', '')
        write_status(result)

        if lorelli_state not in ('failed','checkpoint_wait'):
            bulk_inventory_sync = importlib.reload(bulk_inventory_sync)
            bulk_inventory_sync.core.TOKEN = os.environ.get('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()
            bulk_inventory_sync.core.SHOP = os.environ.get('SHOPIFY_SHOP_DOMAIN', '').strip()
            result['inventory'] = bulk_inventory_sync.run()
            write_status(result)
        inventory_state = (result['inventory'] or {}).get('state', '') if result['inventory'] else ''

        if lorelli_state not in ('failed','checkpoint_wait') and inventory_state not in ('failed','checkpoint_wait'):
            bulk_duplicate_cleanup = importlib.reload(bulk_duplicate_cleanup)
            bulk_duplicate_cleanup.core.TOKEN = os.environ.get('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()
            bulk_duplicate_cleanup.core.SHOP = os.environ.get('SHOPIFY_SHOP_DOMAIN', '').strip()
            result['duplicates'] = bulk_duplicate_cleanup.run()
            write_status(result)
        duplicate_state = (result['duplicates'] or {}).get('state', '') if result['duplicates'] else ''

        if all(x not in ('failed','checkpoint_wait') for x in (lorelli_state, inventory_state, duplicate_state)):
            bulk_euromaster_sync = importlib.reload(bulk_euromaster_sync)
            bulk_euromaster_sync_patch = importlib.reload(bulk_euromaster_sync_patch)
            bulk_euromaster_sync_patch.install(bulk_euromaster_sync)
            token = os.environ.get('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()
            shop = os.environ.get('SHOPIFY_SHOP_DOMAIN', '').strip()
            bulk_euromaster_sync.core.TOKEN = token
            bulk_euromaster_sync.core.SHOP = shop
            bulk_euromaster_sync.invcore.core.TOKEN = token
            bulk_euromaster_sync.invcore.core.SHOP = shop
            result['euromaster'] = bulk_euromaster_sync.run()
            write_status(result)
        euromaster_state = (result['euromaster'] or {}).get('state', '') if result['euromaster'] else ''

        if euromaster_state == 'completed':
            bulk_bgelectronics_sync = importlib.reload(bulk_bgelectronics_sync)
            bulk_bgelectronics_patch = importlib.reload(bulk_bgelectronics_patch)
            bulk_bgelectronics_patch.install(bulk_bgelectronics_sync)
            token = os.environ.get('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()
            shop = os.environ.get('SHOPIFY_SHOP_DOMAIN', '').strip()
            bulk_bgelectronics_sync.core.TOKEN = token
            bulk_bgelectronics_sync.core.SHOP = shop
            bulk_bgelectronics_sync.invcore.core.TOKEN = token
            bulk_bgelectronics_sync.invcore.core.SHOP = shop
            result['bgelectronics'] = bulk_bgelectronics_sync.run()
            write_status(result)
        bge_state = (result['bgelectronics'] or {}).get('state', '') if result['bgelectronics'] else ''

        if bge_state == 'completed':
            bulk_sonne_sync = importlib.reload(bulk_sonne_sync)
            token = os.environ.get('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()
            shop = os.environ.get('SHOPIFY_SHOP_DOMAIN', '').strip()
            bulk_sonne_sync.base.core.TOKEN = token
            bulk_sonne_sync.base.core.SHOP = shop
            bulk_sonne_sync.base.invcore.core.TOKEN = token
            bulk_sonne_sync.base.invcore.core.SHOP = shop
            result['sonne'] = bulk_sonne_sync.run()
            write_status(result)
        sonne_state = (result['sonne'] or {}).get('state', '') if result['sonne'] else ''

        if sonne_state == 'completed':
            bulk_clear_promotions = importlib.reload(bulk_clear_promotions)
            bulk_clear_promotions.core.TOKEN = os.environ.get('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()
            bulk_clear_promotions.core.SHOP = os.environ.get('SHOPIFY_SHOP_DOMAIN', '').strip()
            result['promotions'] = bulk_clear_promotions.run()
            write_status(result)
        promotion_state = (result['promotions'] or {}).get('state', '') if result['promotions'] else ''

        states=[lorelli_state,inventory_state,duplicate_state,euromaster_state,bge_state,sonne_state,promotion_state]
        if 'failed' in states:
            result['state']='failed'
        elif 'checkpoint_wait' in states:
            result['state']='checkpoint_wait'
        elif 'completed_with_errors' in states or 'errors' in supplier_state:
            result['state']='completed_with_errors'
        else:
            result['state']='completed'
    except Exception as e:
        result['state']='failed'
        result['fatal_error']=f'{type(e).__name__}: {e}'
    result['finished_at']=time.time()
    result['duration_seconds']=round(result['finished_at']-started,2)
    write_status(result)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return result


if __name__ == '__main__':
    run()
