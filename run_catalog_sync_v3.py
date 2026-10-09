from run_catalog_sync_v2 import *


def run():
    import importlib, os, time, json
    import kikkaboo_snapshot_fallback
    import bulk_kikkaboo_sync
    import bulk_clear_promotions

    started=time.time()
    base_result=__import__('run_catalog_sync_v2').run()
    if (base_result or {}).get('state') not in ('completed','completed_with_errors'):
        return base_result

    result=dict(base_result)
    result['kikkaboo_snapshot']=kikkaboo_snapshot_fallback.inject_if_missing()
    write_status(result)

    try:
        bulk_kikkaboo_sync=importlib.reload(bulk_kikkaboo_sync)
        token=os.environ.get('SHOPIFY_ADMIN_ACCESS_TOKEN','').strip()
        shop=os.environ.get('SHOPIFY_SHOP_DOMAIN','').strip()
        bulk_kikkaboo_sync.base.core.TOKEN=token
        bulk_kikkaboo_sync.base.core.SHOP=shop
        bulk_kikkaboo_sync.base.invcore.core.TOKEN=token
        bulk_kikkaboo_sync.base.invcore.core.SHOP=shop
        result['kikkaboo']=bulk_kikkaboo_sync.run()
        write_status(result)

        if (result['kikkaboo'] or {}).get('state')=='completed':
            bulk_clear_promotions=importlib.reload(bulk_clear_promotions)
            bulk_clear_promotions.core.TOKEN=token
            bulk_clear_promotions.core.SHOP=shop
            result['promotions_after_kikkaboo']=bulk_clear_promotions.run()
        else:
            result['state']=(result['kikkaboo'] or {}).get('state') or 'failed'

        result['finished_at']=time.time()
        result['duration_seconds']=round(result['finished_at']-started,2)
        write_status(result)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return result
    except Exception as e:
        result['state']='failed'
        result['fatal_error']=f'KikkaBoo {type(e).__name__}: {e}'
        result['finished_at']=time.time()
        result['duration_seconds']=round(result['finished_at']-started,2)
        write_status(result)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return result
