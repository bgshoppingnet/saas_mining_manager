import os, json, time, hashlib
import requests
import bulk_shopify_sync as core

MAX_WAIT = int(os.getenv('SHOPIFY_BULK_MAX_WAIT_SECONDS', '680') or '680')


def op(name):
    return 'BGSClearPromotions' + name

INDEX_QUERY = '''query BGSClearPromotionsIndex {
  products(query:"is_price_reduced:true status:active") {
    edges {
      node {
        __typename id title handle
        variants { edges { node { __typename id price compareAtPrice } } }
      }
    }
  }
}'''


def parse(url):
    r=requests.get(url,timeout=(20,180)); r.raise_for_status()
    products={}; variants=[]
    for line in r.text.splitlines():
        if not line.strip(): continue
        o=json.loads(line); parent=o.get('__parentId'); typ=o.get('__typename')
        if typ=='Product' or (not parent and str(o.get('id','')).startswith('gid://shopify/Product/')):
            products[o['id']]=o
        elif typ=='ProductVariant' or str(o.get('id','')).startswith('gid://shopify/ProductVariant/'):
            if o.get('compareAtPrice') is not None:
                variants.append(o)
    return variants


def mutation(name):
    return f'''mutation {name}($productId:ID!,$variants:[ProductVariantsBulkInput!]!){{productVariantsBulkUpdate(productId:$productId,variants:$variants,allowPartialUpdates:false){{productVariants{{id price compareAtPrice}} userErrors{{field message}}}}}}'''


def run():
    started=time.time(); deadline=started+MAX_WAIT
    status={'state':'running','phase':'index','variants_found':0,'products_queued':0,'failed':0,'errors':[],'started_at':started}
    core.write_status(status)
    with requests.Session() as session:
        name=op('IndexV1')
        bulk=core.recent(session,name,'query')
        if not bulk or str(bulk.get('status')) in ('FAILED','CANCELED','EXPIRED'):
            out=core.gql(session,core.START_QUERY,{'query':'query '+name+' { products(query:"is_price_reduced:true status:active") { edges { node { __typename id title handle variants { edges { node { __typename id price compareAtPrice } } } } } } }'}).get('bulkOperationRunQuery') or {}
            if out.get('userErrors'): raise RuntimeError(str(out['userErrors']))
            bulk=out.get('bulkOperation') or {}
        bulk=core.wait(session,bulk,status,deadline)
        if str((bulk or {}).get('status'))!='COMPLETED' or not bulk.get('url'):
            status['state']='checkpoint_wait'; core.write_status(status); return status
        rows=parse(bulk['url']); status['variants_found']=len(rows)

        by_product={}
        # parent ID is not kept after parse, so run a compact query result that includes parent in JSONL.
        r=requests.get(bulk['url'],timeout=(20,180)); r.raise_for_status()
        for line in r.text.splitlines():
            if not line.strip(): continue
            o=json.loads(line)
            if str(o.get('id','')).startswith('gid://shopify/ProductVariant/') and o.get('compareAtPrice') is not None and o.get('__parentId'):
                by_product.setdefault(o['__parentId'],[]).append(o)

        payload=[]
        for pid,vars_ in by_product.items():
            updates=[]
            for v in vars_:
                # Restore regular price from compare-at, then clear compare-at.
                updates.append({'id':v['id'],'price':str(v['compareAtPrice']),'compareAtPrice':None})
            payload.append({'productId':pid,'variants':updates})
        status['products_queued']=len(payload); status['phase']='write'; core.write_status(status)
        name=op('WriteV1')
        bulk,errs=core.run_bulk(session,name,mutation(name),payload,status,deadline)
        if str((bulk or {}).get('status'))!='COMPLETED':
            status['state']='checkpoint_wait'; core.write_status(status); return status
        if errs:
            status['state']='completed_with_errors'; status['failed']=len(errs); status['errors']=errs; core.write_status(status); return status
    status.update(state='completed',phase='done',finished_at=time.time(),duration_seconds=round(time.time()-started,2)); core.write_status(status); return status


if __name__=='__main__': run()
