import os, json, time, threading, importlib, requests

STATUS=os.getenv('CATALOG_SYNC_STATUS_PATH','/tmp/catalog-sync-status.json')

def _auth():
    if os.getenv('SHOPIFY_ADMIN_ACCESS_TOKEN','').strip(): return
    shop=os.getenv('SHOPIFY_SHOP_DOMAIN','').strip(); cid=os.getenv('SHOPIFY_CLIENT_ID','').strip(); sec=os.getenv('SHOPIFY_CLIENT_SECRET','').strip()
    r=requests.post(f'https://{shop}/admin/oauth/access_token',headers={'Content-Type':'application/x-www-form-urlencoded'},data={'grant_type':'client_credentials','client_id':cid,'client_secret':sec},timeout=(15,60)); r.raise_for_status()
    os.environ['SHOPIFY_ADMIN_ACCESS_TOKEN']=r.json()['access_token']

def _runner():
    deadline=time.time()+1800
    while time.time()<deadline:
        try:
            if os.path.exists(STATUS):
                with open(STATUS,encoding='utf-8') as f: s=json.load(f)
                if (s.get('sonne') or {}).get('state')=='completed' and (s.get('promotions') or {}).get('state')=='completed':
                    break
        except Exception: pass
        time.sleep(10)
    else: return
    try:
        import playfactory_snapshot_fallback
        playfactory_snapshot_fallback.inject_if_missing()
        _auth()
        import bulk_playfactory_sync
        m=importlib.reload(bulk_playfactory_sync)
        token=os.environ.get('SHOPIFY_ADMIN_ACCESS_TOKEN','').strip(); shop=os.environ.get('SHOPIFY_SHOP_DOMAIN','').strip()
        m.base.core.TOKEN=token; m.base.core.SHOP=shop; m.base.invcore.core.TOKEN=token; m.base.invcore.core.SHOP=shop
        result=m.run()
        print(json.dumps({'playfactory_autorun':result},ensure_ascii=False),flush=True)
    except Exception as e:
        print(json.dumps({'playfactory_autorun_error':f'{type(e).__name__}: {e}'},ensure_ascii=False),flush=True)

def when_ready(server):
    threading.Thread(target=_runner,name='playfactory-once',daemon=True).start()
