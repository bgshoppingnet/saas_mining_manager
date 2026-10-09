from run_catalog_sync import *


def run():
    import importlib, os, time, json
    import playfactory_snapshot_fallback
    import bulk_playfactory_sync
    started=time.time()
    base_result=__import__('run_catalog_sync').run()
    if (base_result or {}).get('state') not in ('completed','completed_with_errors'):
        return base_result
    result=dict(base_result)
    result['playfactory_snapshot']=playfactory_snapshot_fallback.inject_if_missing()
    write_status(result)
    try:
        bulk_playfactory_sync=importlib.reload(bulk_playfactory_sync)
        token=os.environ.get('SHOPIFY_ADMIN_ACCESS_TOKEN','').strip()
        shop=os.environ.get('SHOPIFY_SHOP_DOMAIN','').strip()
        bulk_playfactory_sync.base.core.TOKEN=token
        bulk_playfactory_sync.base.core.SHOP=shop
        bulk_playfactory_sync.base.invcore.core.TOKEN=token
        bulk_playfactory_sync.base.invcore.core.SHOP=shop
        result['playfactory']=bulk_playfactory_sync.run()
        if (result['playfactory'] or {}).get('state')!='completed':
            result['state']=(result['playfactory'] or {}).get('state') or 'failed'
        result['finished_at']=time.time()
        result['duration_seconds']=round(result['finished_at']-started,2)
        write_status(result)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return result
    except Exception as e:
        result['state']='failed'
        result['fatal_error']=f'Playfactory {type(e).__name__}: {e}'
        result['finished_at']=time.time()
        result['duration_seconds']=round(result['finished_at']-started,2)
        write_status(result)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return result
