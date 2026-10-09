import os, json, time, hashlib, html, re
from collections import defaultdict
import requests
import bulk_shopify_sync as core
import bulk_shopify_sync_patch as core_patch
import bulk_inventory_sync as invcore

LOCATION_ID = os.getenv('SHOPIFY_LOCATION_ID', '').strip()
CATALOG_PATH = os.getenv('SUPPLIER_OUT_PATH', '/tmp/supplier-catalog.jsonl')
MAX_WAIT = int(os.getenv('SHOPIFY_BULK_MAX_WAIT_SECONDS', '680') or '680')


def norm(v): return ' '.join(str(v or '').split())
def low(v): return norm(v).lower()
def slug(v): return re.sub(r'[^a-z0-9]+','-',norm(v).lower()).strip('-')[:180]

def qty(v):
    try: return max(0, int(float(str(v).strip().replace(',', '.'))))
    except Exception: return None


def load_rows():
    out=[]
    with open(CATALOG_PATH,encoding='utf-8') as f:
        for line in f:
            if not line.strip(): continue
            x=json.loads(line)
            if low(x.get('supplier'))!='bgelectronics': continue
            x['_fp']=str(x.get('fingerprint') or '') or hashlib.sha256(json.dumps(x,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            out.append(x)
    return out


def digest(rows):
    h=hashlib.sha256()
    for x in sorted(rows,key=lambda z:(low(z.get('sku')),low(z.get('ean')),low(z.get('external_id')))):
        h.update(x['_fp'].encode())
    return h.hexdigest()[:14].upper()


def op(phase,d): return 'BGSBGElectronics'+phase+d

def source_id(item): return norm(item.get('external_id') or item.get('sku') or item.get('ean') or item.get('key'))

def category_leaf(item):
    v=norm(item.get('category'))
    if not v: return ''
    for sep in ('|',',','>'):
        if sep in v: v=v.split(sep)[-1].strip()
    return v[:255]


def product_metafields(item):
    fields=[
        {'namespace':'supplier_sync','key':'supplier','type':'single_line_text_field','value':'BGElectronics'},
        {'namespace':'supplier_sync','key':'source_key','type':'single_line_text_field','value':source_id(item)},
    ]
    if str(item.get('url') or '').startswith('http'):
        fields.append({'namespace':'supplier_sync','key':'source_url','type':'url','value':str(item['url'])[:2048]})
    return fields


def variant_metafields(item,final=False):
    fields=[{'namespace':'supplier_sync','key':'source_key','type':'single_line_text_field','value':source_id(item)}]
    if final:
        fields.append({'namespace':'supplier_sync','key':'fingerprint','type':'single_line_text_field','value':item['_fp']})
    return fields


def seo(item):
    name=norm(item.get('name')) or norm(item.get('sku')) or 'Продукт'
    brand=norm(item.get('brand'))
    title=(name if (brand and brand.lower() in name.lower()) else (name+' '+brand).strip())[:70]
    desc=(name+'. Онлайн поръчка от BGShopping.net. Доставка в България и Европа.')[:320]
    return {'title':title,'description':desc}


def description_html(item):
    d=str(item.get('description') or '').strip()
    if d:
        if '<' in d and '>' in d: return d[:64000]
        return '<p>'+html.escape(d)+'</p>'
    return '<p>'+html.escape(norm(item.get('name')) or 'BGElectronics продукт')+'</p>'


def images(item):
    out=[]
    for u in [item.get('image')]+list(item.get('images') or []):
        u=str(u or '').strip()
        if u.startswith(('http://','https://')) and u not in out: out.append(u)
    return out[:10]


def build_indexes(products):
    source=defaultdict(list); sku=defaultdict(list); ean=defaultdict(list)
    for p in products:
        supp=low((p.get('mf') or {}).get('supplier'))
        sk=low((p.get('mf') or {}).get('source_key'))
        if sk: source[sk].append((p,None,supp))
        for v in p.get('variants_list') or []:
            if low(v.get('sku')): sku[low(v['sku'])].append((p,v,supp))
            if low(v.get('barcode')): ean[low(v['barcode'])].append((p,v,supp))
            vk=low((v.get('mf') or {}).get('source_key'))
            if vk: source[vk].append((p,v,supp))
    return source,sku,ean


def choose(cands):
    if not cands: return None,None,False
    tagged=[x for x in cands if x[2]=='bgelectronics']
    use=tagged if tagged else cands
    pids={p['id'] for p,v,s in use}; vids={v['id'] for p,v,s in use if v}
    if len(pids)==1 and len(vids)<=1:
        p=use[0][0]
        v=next((v for pp,v,s in use if v),None)
        if not v and len(p.get('variants_list') or [])==1: v=p['variants_list'][0]
        return p,v,False
    return None,None,True


def match(item,idx):
    source,sku,ean=idx
    sid=low(source_id(item))
    if sid and source.get(sid):
        p,v,conf=choose(source[sid])
        if p and not conf: return p,v,False
    checks=[]
    s=low(item.get('sku')); e=low(item.get('ean'))
    if s and sku.get(s): checks.extend(sku[s])
    if e and ean.get(e): checks.extend(ean[e])
    dedup={(p['id'],(v or {}).get('id')):(p,v,supp) for p,v,supp in checks}
    return choose(list(dedup.values()))


def product_fields(item,pid=None):
    name=norm(item.get('name')) or norm(item.get('sku')) or 'BGElectronics продукт'
    out={'title':name[:255],'descriptionHtml':description_html(item),'vendor':norm(item.get('brand')) or 'BGElectronics','seo':seo(item),'metafields':product_metafields(item)}
    leaf=category_leaf(item)
    if leaf: out['productType']=leaf
    if pid: out['id']=pid
    return out


def variant_fields(item,vid=None,final=False):
    v={'inventoryPolicy':'DENY','compareAtPrice':None,'inventoryItem':{'tracked':True,'requiresShipping':True},'metafields':variant_metafields(item,final)}
    if vid: v['id']=vid
    sku=norm(item.get('sku'))
    if sku: v['inventoryItem']['sku']=sku
    if norm(item.get('ean')): v['barcode']=norm(item['ean'])
    if str(item.get('price') or '').strip(): v['price']=str(item['price'])
    return v


def create_vars(item):
    sid=source_id(item)
    if not sid: return None
    p=product_fields(item)
    p['handle']=slug('bgelectronics-'+sid)
    p['status']='ACTIVE'
    p['tags']=['supplier:BGElectronics','market:BG','market:EU']
    p['productOptions']=[{'name':'Title','position':1,'values':[{'name':'Default Title'}]}]
    v=variant_fields(item,final=True)
    v['optionValues']=[{'optionName':'Title','name':'Default Title'}]
    q=qty(item.get('quantity'))
    if q is not None and LOCATION_ID:
        v['inventoryQuantities']=[{'locationId':LOCATION_ID,'name':'available','quantity':q}]
    p['variants']=[v]
    pics=images(item)
    if pics:
        p['files']=[{'originalSource':u,'contentType':'IMAGE','alt':norm(item.get('name'))[:512],'duplicateResolutionMode':'APPEND_UUID'} for u in pics]
    return {'identifier':{'handle':p['handle']},'input':p}


def mutation(name,kind):
    if kind=='create': return f'''mutation {name}($identifier:ProductSetIdentifiers,$input:ProductSetInput!){{productSet(identifier:$identifier,input:$input,synchronous:true){{product{{id handle}} userErrors{{field message}}}}}}'''
    if kind=='product': return f'''mutation {name}($product:ProductUpdateInput!){{productUpdate(product:$product){{product{{id handle}} userErrors{{field message}}}}}}'''
    if kind=='variant': return f'''mutation {name}($productId:ID!,$variants:[ProductVariantsBulkInput!]!){{productVariantsBulkUpdate(productId:$productId,variants:$variants,allowPartialUpdates:false){{productVariants{{id sku barcode price}} userErrors{{field message}}}}}}'''
    if kind=='inventory': return f'''mutation {name}($input:InventorySetQuantitiesInput!,$idempotencyKey:String!){{inventorySetQuantities(input:$input) @idempotent(key:$idempotencyKey){{inventoryAdjustmentGroup{{createdAt}} userErrors{{field message}}}}}}'''
    return f'''mutation {name}($metafields:[MetafieldsSetInput!]!){{metafieldsSet(metafields:$metafields){{metafields{{id key value}} userErrors{{field message}}}}}}'''


def run():
    started=time.time(); deadline=started+MAX_WAIT
    status={'state':'running','supplier':'BGElectronics','phase':'start','total':0,'matched':0,'unchanged':0,'create_queued':0,'update_queued':0,'conflicts':0,'failed':0,'errors':[],'started_at':started}
    core.write_status(status)
    try:
        rows=load_rows(); status['total']=len(rows)
        if not rows: raise RuntimeError('BGElectronics feed produced zero items')
        d=digest(rows); status['checkpoint']=d; core.write_status(status)
        core_patch.install(core)
        with requests.Session() as session:
            status['phase']='index'; name=op('Index',d)
            products,bulk=core.ensure_index(session,name,status,deadline)
            if products is None: status['state']='checkpoint_wait'; core.write_status(status); return status
            idx=build_indexes(products); creates=[]; updates=[]; unchanged=[]; conflicts=[]
            for item in rows:
                p,v,conf=match(item,idx)
                if conf: conflicts.append(item); continue
                if not p: creates.append(item); continue
                existing=((v or {}).get('mf') or {}).get('fingerprint') or (p.get('mf') or {}).get('fingerprint')
                if existing==item['_fp']: unchanged.append((item,p,v))
                else: updates.append((item,p,v))
            status.update(matched=len(updates)+len(unchanged),unchanged=len(unchanged),create_queued=len(creates),update_queued=len(updates),conflicts=len(conflicts)); core.write_status(status)

            create_rows=[x for x in (create_vars(i) for i in creates) if x]
            status['phase']='create'; name=op('Create',d)
            bulk,errs=core.run_bulk(session,name,mutation(name,'create'),create_rows,status,deadline)
            if str((bulk or {}).get('status'))!='COMPLETED': status['state']='checkpoint_wait'; core.write_status(status); return status
            if errs: status.update(state='completed_with_errors',failed=len(errs),errors=errs); core.write_status(status); return status

            by_product=defaultdict(list)
            for t in updates: by_product[t[1]['id']].append(t)
            product_rows=[{'product':product_fields(group[0][0],pid)} for pid,group in by_product.items()]
            status['phase']='product_update'; name=op('Product',d)
            bulk,errs=core.run_bulk(session,name,mutation(name,'product'),product_rows,status,deadline)
            if str((bulk or {}).get('status'))!='COMPLETED': status['state']='checkpoint_wait'; core.write_status(status); return status
            if errs: status.update(state='completed_with_errors',failed=len(errs),errors=errs); core.write_status(status); return status

            variant_rows=[]
            for pid,group in by_product.items():
                variants=[variant_fields(i,v['id'],False) for i,p,v in group if v]
                if variants: variant_rows.append({'productId':pid,'variants':variants})
            status['phase']='variant_update'; name=op('Variant',d)
            bulk,errs=core.run_bulk(session,name,mutation(name,'variant'),variant_rows,status,deadline)
            if str((bulk or {}).get('status'))!='COMPLETED': status['state']='checkpoint_wait'; core.write_status(status); return status
            if errs: status.update(state='completed_with_errors',failed=len(errs),errors=errs); core.write_status(status); return status

            status['phase']='inventory_index'; invname=op('InventoryIndex',d)
            variants,bulk=invcore.ensure_index(session,invname,status,deadline)
            if variants is None: status['state']='checkpoint_wait'; core.write_status(status); return status
            inv_idx=invcore.build_indexes(variants); quantities=[]
            for item in rows:
                q=qty(item.get('quantity'))
                if q is None: continue
                v,conf=invcore.match(item,inv_idx)
                if conf or not v or not v.get('inventoryItemId'): continue
                quantities.append({'inventoryItemId':v['inventoryItemId'],'locationId':LOCATION_ID,'quantity':q,'changeFromQuantity':None})
            quantities=list({x['inventoryItemId']:x for x in quantities}.values())
            inventory_rows=[]
            for n in range(0,len(quantities),200):
                chunk=quantities[n:n+200]; idem=hashlib.sha256((d+json.dumps(chunk,sort_keys=True)).encode()).hexdigest()
                inventory_rows.append({'input':{'name':'available','reason':'correction','referenceDocumentUri':'gid://bgshopping/BGElectronics/'+d,'quantities':chunk},'idempotencyKey':idem})
            status['inventory_queued']=len(quantities); status['phase']='inventory'; core.write_status(status)
            name=op('Inventory',d); bulk,errs=core.run_bulk(session,name,mutation(name,'inventory'),inventory_rows,status,deadline)
            if str((bulk or {}).get('status'))!='COMPLETED': status['state']='checkpoint_wait'; core.write_status(status); return status
            if errs: status.update(state='completed_with_errors',failed=len(errs),errors=errs); core.write_status(status); return status

            mf=[]
            for item,p,v in updates:
                if v: mf.append({'ownerId':v['id'],'namespace':'supplier_sync','key':'fingerprint','type':'single_line_text_field','value':item['_fp']})
            final_rows=[{'metafields':mf[n:n+25]} for n in range(0,len(mf),25)]
            status['phase']='finalize'; name=op('Finalize',d)
            bulk,errs=core.run_bulk(session,name,mutation(name,'finalize'),final_rows,status,deadline)
            if str((bulk or {}).get('status'))!='COMPLETED': status['state']='checkpoint_wait'; core.write_status(status); return status
            if errs: status.update(state='completed_with_errors',failed=len(errs),errors=errs); core.write_status(status); return status

        status.update(state='completed',phase='done',created=len(create_rows),updated=len(updates),inventory_updated=len(quantities),finished_at=time.time(),duration_seconds=round(time.time()-started,2)); core.write_status(status); return status
    except Exception as e:
        status.update(state='failed',fatal_error=f'{type(e).__name__}: {e}',finished_at=time.time(),duration_seconds=round(time.time()-started,2)); core.write_status(status); return status


if __name__=='__main__': run()
