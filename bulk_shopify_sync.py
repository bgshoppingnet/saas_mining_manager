import os, json, time, hashlib, html, re
from decimal import Decimal, InvalidOperation
from collections import defaultdict
import requests

CATALOG_PATH = os.getenv('SUPPLIER_OUT_PATH', '/tmp/supplier-catalog.jsonl')
STATUS_PATH = os.getenv('SHOPIFY_SYNC_STATUS_PATH', '/tmp/shopify-sync-status.json')
SHOP = os.getenv('SHOPIFY_SHOP_DOMAIN', '').strip()
TOKEN = os.getenv('SHOPIFY_ADMIN_ACCESS_TOKEN', '').strip()
API = os.getenv('SHOPIFY_API_VERSION', '2026-10').strip()
LOCATION_ID = os.getenv('SHOPIFY_LOCATION_ID', '').strip()
POLL = int(os.getenv('SHOPIFY_BULK_POLL_SECONDS', '6') or '6')
MAX_WAIT = int(os.getenv('SHOPIFY_BULK_MAX_WAIT_SECONDS', '680') or '680')
WRITE = os.getenv('SHOPIFY_WRITE_ENABLED', 'false').lower() in ('1','true','yes','on')

START_QUERY = '''mutation StartBulkIndex($query:String!){bulkOperationRunQuery(query:$query,groupObjects:false){bulkOperation{id status type} userErrors{field message}}}'''
STAGE = '''mutation StageBulk($input:[StagedUploadInput!]!){stagedUploadsCreate(input:$input){stagedTargets{url resourceUrl parameters{name value}} userErrors{field message}}}'''
START_MUT = '''mutation StartBulkMutation($mutation:String!,$path:String!,$client:String!){bulkOperationRunMutation(mutation:$mutation,stagedUploadPath:$path,clientIdentifier:$client){bulkOperation{id status type} userErrors{field message}}}'''
STATUS_Q = '''query BulkStatus($id:ID!){bulkOperation(id:$id){id status type query objectCount rootObjectCount errorCode url partialDataUrl createdAt completedAt}}'''
RECENT_Q = '''query RecentBulks($q:String!){bulkOperations(first:20,query:$q){nodes{id status type query objectCount rootObjectCount errorCode url partialDataUrl createdAt completedAt}}}'''


def norm(v): return ' '.join(html.unescape(str(v or '')).split())
def key(v): return norm(v).lower()
def safe_int(v):
    m=re.search(r'-?\d+', norm(v)); return max(0,int(m.group())) if m else None

def money(v):
    try:
        s=re.sub(r'[^0-9.\-]','',str(v or '').replace(',','.').replace(' ',''))
        if not s: return None
        d=Decimal(s)
        return format(d.quantize(Decimal('0.01')),'f') if d>=0 else None
    except (InvalidOperation,ValueError): return None

def strip_html(v): return norm(re.sub(r'<[^>]+>',' ',str(v or '')))
def slug(v): return re.sub(r'[^a-z0-9]+','-',norm(v).lower()).strip('-')[:180]

def fp(item):
    payload={k:item.get(k) for k in ('supplier','key','external_id','sku','ean','name','price','currency','quantity','description','brand','category','image','images')}
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def write_status(s):
    try:
        tmp=STATUS_PATH+'.tmp'
        with open(tmp,'w',encoding='utf-8') as f:
            json.dump(s,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,STATUS_PATH)
    except Exception: pass
    print(json.dumps(s,ensure_ascii=False),flush=True)

def gql(session,q,variables=None):
    r=session.post(f'https://{SHOP}/admin/api/{API}/graphql.json',headers={'X-Shopify-Access-Token':TOKEN,'Content-Type':'application/json'},json={'query':q,'variables':variables or {}},timeout=(20,120))
    r.raise_for_status(); d=r.json()
    if d.get('errors'): raise RuntimeError(json.dumps(d['errors'],ensure_ascii=False))
    return d.get('data') or {}

def load_catalog():
    rows=[]
    with open(CATALOG_PATH,encoding='utf-8') as f:
        for line in f:
            if not line.strip(): continue
            x=json.loads(line)
            if key(x.get('supplier'))!='lorelli': continue
            x['_fp']=fp(x); rows.append(x)
    return rows

def catalog_digest(rows):
    h=hashlib.sha256()
    for x in sorted(rows,key=lambda z:(key(z.get('sku')),key(z.get('ean')),key(z.get('key')))):
        h.update(x['_fp'].encode())
    return h.hexdigest()[:14].upper()

def operation_name(phase,digest): return f'BGSLorelli{phase}{digest}'

def recent(session,name,optype):
    nodes=(gql(session,RECENT_Q,{'q':f'operation_type:{optype}'}).get('bulkOperations') or {}).get('nodes') or []
    for op in nodes:
        if name in str(op.get('query') or ''): return op
    return None

def wait(session,op,status,deadline):
    while op and time.time()<deadline:
        state=str(op.get('status') or '')
        status['bulk']={'id':op.get('id'),'status':state,'type':op.get('type'),'objectCount':op.get('objectCount'),'rootObjectCount':op.get('rootObjectCount'),'errorCode':op.get('errorCode')}; write_status(status)
        if state in ('COMPLETED','FAILED','CANCELED','EXPIRED'): return op
        time.sleep(POLL); op=(gql(session,STATUS_Q,{'id':op['id']}).get('bulkOperation') or {})
    return op

def stage_rows(session,rows,name):
    body=''.join(json.dumps(r,ensure_ascii=False,separators=(',',':'))+'\n' for r in rows).encode()
    out=gql(session,STAGE,{'input':[{'resource':'BULK_MUTATION_VARIABLES','filename':name+'.jsonl','mimeType':'text/jsonl','httpMethod':'POST'}]}).get('stagedUploadsCreate') or {}
    if out.get('userErrors'): raise RuntimeError(str(out['userErrors']))
    t=(out.get('stagedTargets') or [None])[0]
    if not t: raise RuntimeError('No staged upload target')
    form={p['name']:p['value'] for p in t.get('parameters') or []}
    rr=requests.post(t['url'],data=form,files={'file':(name+'.jsonl',body,'text/jsonl')},timeout=(20,180)); rr.raise_for_status()
    if not form.get('key'): raise RuntimeError('Missing stagedUploadPath')
    return form['key']

def bulk_errors(op):
    url=(op or {}).get('url') or (op or {}).get('partialDataUrl')
    if not url: return []
    r=requests.get(url,timeout=(20,180)); r.raise_for_status(); errors=[]
    def walk(v):
        if isinstance(v,dict):
            if isinstance(v.get('userErrors'),list) and v['userErrors']:
                errors.extend(v['userErrors'])
            if isinstance(v.get('errors'),list) and v['errors']:
                errors.extend(v['errors'])
            for z in v.values(): walk(z)
        elif isinstance(v,list):
            for z in v: walk(z)
    for line in r.text.splitlines():
        if line.strip():
            try: walk(json.loads(line))
            except Exception as e: errors.append({'message':'result parse: '+str(e)})
    return errors[:100]

def run_bulk(session,name,mutation,rows,status,deadline):
    if not rows: return {'status':'COMPLETED','url':None},[]
    op=recent(session,name,'mutation')
    if not op or str(op.get('status')) in ('FAILED','CANCELED','EXPIRED'):
        path=stage_rows(session,rows,name)
        out=gql(session,START_MUT,{'mutation':mutation,'path':path,'client':name}).get('bulkOperationRunMutation') or {}
        if out.get('userErrors'): raise RuntimeError(str(out['userErrors']))
        op=out.get('bulkOperation') or {}
    op=wait(session,op,status,deadline)
    if str((op or {}).get('status'))!='COMPLETED': return op,[]
    return op,bulk_errors(op)

def index_query(name):
    return f'''query {name}{{products{{edges{{node{{__typename id title handle vendor productType tags metafields(first:10,namespace:"supplier_sync"){{edges{{node{{__typename id namespace key value type}}}}}} variants{{edges{{node{{__typename id sku barcode price inventoryItem{{id tracked}} metafields(first:10,namespace:"supplier_sync"){{edges{{node{{__typename id namespace key value type}}}}}}}}}}}}}}}}}}'''

def ensure_index(session,name,status,deadline):
    op=recent(session,name,'query')
    if not op or str(op.get('status')) in ('FAILED','CANCELED','EXPIRED'):
        out=gql(session,START_QUERY,{'query':index_query(name)}).get('bulkOperationRunQuery') or {}
        if out.get('userErrors'): raise RuntimeError(str(out['userErrors']))
        op=out.get('bulkOperation') or {}
    op=wait(session,op,status,deadline)
    if str((op or {}).get('status'))!='COMPLETED' or not op.get('url'): return None,op
    return parse_index(op['url']),op

def parse_index(url):
    r=requests.get(url,timeout=(20,180)); r.raise_for_status(); products={}; variants={}
    for line in r.text.splitlines():
        if not line.strip(): continue
        o=json.loads(line); typ=o.get('__typename'); parent=o.get('__parentId')
        if typ=='Product' or (not parent and str(o.get('id','')).startswith('gid://shopify/Product/')):
            o['variants_list']=[]; o['mf']={}; products[o['id']]=o
        elif typ=='ProductVariant' or str(o.get('id','')).startswith('gid://shopify/ProductVariant/'):
            o['mf']={}; variants[o['id']]=o
            if parent in products: products[parent]['variants_list'].append(o)
        elif typ=='Metafield' and parent:
            target=products.get(parent) or variants.get(parent)
            if target is not None: target.setdefault('mf',{})[o.get('key')]=o.get('value') or ''
    return list(products.values())

def choose(cands):
    if not cands: return None,None,False
    pids={p['id'] for p,v in cands}
    if len(pids)==1:
        p=cands[0][0]; vids={v['id'] for pp,v in cands if v}
        return (p,next((v for pp,v in cands if v),None),False) if len(vids)<=1 else (None,None,True)
    canon=[(p,v) for p,v in cands if str(p.get('handle') or '').lower().startswith('lorelli-')]
    if len({p['id'] for p,v in canon})==1:
        p=canon[0][0]; vids={v['id'] for pp,v in canon if v}
        if len(vids)<=1: return p,next((v for pp,v in canon if v),None),False
    return None,None,True

def indexes(products):
    sku=defaultdict(list); ean=defaultdict(list); source=defaultdict(list)
    for p in products:
        sk=key((p.get('mf') or {}).get('source_key'))
        if sk: source[sk].append((p,None))
        for v in p.get('variants_list') or []:
            if key(v.get('sku')): sku[key(v['sku'])].append((p,v))
            if key(v.get('barcode')): ean[key(v['barcode'])].append((p,v))
            vk=key((v.get('mf') or {}).get('source_key'))
            if vk: source[vk].append((p,v))
    return sku,ean,source

def match(item,idx):
    sku,ean,source=idx; candidates=[]
    for table,val in ((sku,key(item.get('sku'))),(ean,key(item.get('ean'))),(source,key(item.get('key') or item.get('external_id')))):
        if val and table.get(val): candidates.extend(table[val])
    if not candidates: return None,None,False
    # If SKU/EAN point at different products, quarantine instead of guessing.
    return choose(candidates)

def product_fields(item,pid=None):
    name=norm(item.get('name')) or norm(item.get('sku')) or 'Lorelli'
    d=strip_html(item.get('description'))
    out={'title':name[:255],'descriptionHtml':('<p>'+html.escape(d)+'</p>') if d else '<p>'+html.escape(name)+'</p>','vendor':norm(item.get('brand')) or 'Lorelli','seo':{'title':(name+' | Lorelli')[:70],'description':(name+' от Lorelli. Поръчайте онлайн от BGShopping.net. Доставка в България и Европа.')[:320]},'metafields':[{'namespace':'supplier_sync','key':'supplier','type':'single_line_text_field','value':'Lorelli'}]}
    if pid: out['id']=pid
    if norm(item.get('category')): out['productType']=norm(item.get('category'))[:255]
    return out

def variant_fields(item,vid=None,include_fp=False):
    out={'inventoryPolicy':'CONTINUE','inventoryItem':{'tracked':True,'requiresShipping':True}}
    if vid: out['id']=vid
    if norm(item.get('sku')): out['sku']=norm(item['sku']); out['inventoryItem']['sku']=norm(item['sku'])
    if norm(item.get('ean')): out['barcode']=norm(item['ean'])
    if money(item.get('price')) is not None: out['price']=money(item['price'])
    m=[{'namespace':'supplier_sync','key':'source_key','type':'single_line_text_field','value':norm(item.get('key') or item.get('external_id') or item.get('sku') or item.get('ean'))}]
    if include_fp: m.append({'namespace':'supplier_sync','key':'fingerprint','type':'single_line_text_field','value':item['_fp']})
    out['metafields']=m
    return out

def create_input(item):
    base=norm(item.get('sku') or item.get('ean') or item.get('key') or item.get('external_id'))
    if not base: return None
    p=product_fields(item); p['handle']=slug('lorelli-'+base); p['status']='ACTIVE'; p['tags']=['Lorelli','supplier:Lorelli','market:BG','market:EU']
    p['productOptions']=[{'name':'Title','position':1,'values':[{'name':'Default Title'}]}]
    v=variant_fields(item,include_fp=True); v['optionValues']=[{'optionName':'Title','name':'Default Title'}]
    q=safe_int(item.get('quantity'))
    if q is not None and LOCATION_ID: v['inventoryQuantities']=[{'locationId':LOCATION_ID,'name':'available','quantity':q}]
    p['variants']=[v]
    images=[]
    for u in [item.get('image')]+list(item.get('images') or []):
        u=str(u or '').strip()
        if u.startswith(('http://','https://')) and u not in images: images.append(u)
    if images: p['files']=[{'originalSource':u,'contentType':'IMAGE','alt':norm(item.get('name'))[:512],'duplicateResolutionMode':'APPEND_UUID'} for u in images[:10]]
    return {'identifier':{'handle':p['handle']},'input':p}

def mutation(opname,kind):
    if kind=='create': return f'''mutation {opname}($identifier:ProductSetIdentifiers,$input:ProductSetInput!){{productSet(identifier:$identifier,input:$input,synchronous:true){{product{{id handle}} userErrors{{field message}}}}}}'''
    if kind=='product': return f'''mutation {opname}($product:ProductUpdateInput!){{productUpdate(product:$product){{product{{id handle}} userErrors{{field message}}}}}}'''
    if kind=='variant': return f'''mutation {opname}($productId:ID!,$variants:[ProductVariantsBulkInput!]!){{productVariantsBulkUpdate(productId:$productId,variants:$variants,allowPartialUpdates:false){{productVariants{{id sku barcode price}} userErrors{{field message}}}}}}'''
    if kind=='inventory': return f'''mutation {opname}($input:InventorySetQuantitiesInput!,$idempotencyKey:String!){{inventorySetQuantities(input:$input) @idempotent(key:$idempotencyKey){{inventoryAdjustmentGroup{{createdAt}} userErrors{{field message}}}}}}'''
    return f'''mutation {opname}($metafields:[MetafieldsSetInput!]!){{metafieldsSet(metafields:$metafields){{metafields{{id key value}} userErrors{{field message}}}}}}'''

def run():
    started=time.time(); deadline=started+MAX_WAIT
    s={'state':'running','mode':'shopify_bulk','supplier':'Lorelli','phase':'start','total':0,'matched':0,'unchanged':0,'create_queued':0,'update_queued':0,'conflicts':0,'failed':0,'errors':[],'started_at':started}; write_status(s)
    try:
        if not WRITE: raise RuntimeError('SHOPIFY_WRITE_ENABLED is false')
        if not SHOP or not TOKEN: raise RuntimeError('Missing Shopify credentials')
        rows=load_catalog(); s['total']=len(rows); digest=catalog_digest(rows); s['checkpoint']=digest; write_status(s)
        with requests.Session() as session:
            name=operation_name('Index',digest); s['phase']='index'; products,op=ensure_index(session,name,s,deadline)
            if products is None: s['state']='checkpoint_wait'; write_status(s); return s
            idx=indexes(products); creates=[]; updates=[]; unchanged=[]; conflicts=[]
            for item in rows:
                p,v,conf=match(item,idx)
                if conf: conflicts.append(item); continue
                if not p: creates.append(item); continue
                existing=(v.get('mf') or {}).get('fingerprint') if v else None
                existing=existing or (p.get('mf') or {}).get('fingerprint')
                if existing==item['_fp']: unchanged.append((item,p,v))
                else: updates.append((item,p,v))
            s.update(matched=len(updates)+len(unchanged),unchanged=len(unchanged),create_queued=len(creates),update_queued=len(updates),conflicts=len(conflicts)); write_status(s)

            # CREATE missing items with productSet. Each feed SKU/EAN gets a unique handle.
            create_rows=[x for x in (create_input(i) for i in creates) if x]
            opname=operation_name('Create',digest); s['phase']='create'; op,errs=run_bulk(session,opname,mutation(opname,'create'),create_rows,s,deadline)
            if str((op or {}).get('status'))!='COMPLETED': s['state']='checkpoint_wait'; write_status(s); return s
            if errs: s['state']='completed_with_errors'; s['failed']+=len(errs); s['errors']+=errs; write_status(s); return s

            # One product-level update per product to avoid races on multi-variant Lorelli products.
            by_product=defaultdict(list)
            for t in updates: by_product[t[1]['id']].append(t)
            product_rows=[{'product':product_fields(group[0][0],pid)} for pid,group in by_product.items()]
            opname=operation_name('Product',digest); s['phase']='product_update'; op,errs=run_bulk(session,opname,mutation(opname,'product'),product_rows,s,deadline)
            if str((op or {}).get('status'))!='COMPLETED': s['state']='checkpoint_wait'; write_status(s); return s
            if errs: s['state']='completed_with_errors'; s['failed']+=len(errs); s['errors']+=errs; write_status(s); return s

            variant_rows=[]
            for pid,group in by_product.items():
                vs=[variant_fields(item,v['id'],False) for item,p,v in group if v]
                if vs: variant_rows.append({'productId':pid,'variants':vs})
            opname=operation_name('Variant',digest); s['phase']='variant_update'; op,errs=run_bulk(session,opname,mutation(opname,'variant'),variant_rows,s,deadline)
            if str((op or {}).get('status'))!='COMPLETED': s['state']='checkpoint_wait'; write_status(s); return s
            if errs: s['state']='completed_with_errors'; s['failed']+=len(errs); s['errors']+=errs; write_status(s); return s

            # Absolute inventory, grouped in chunks to keep each input bounded.
            inv=[]
            for item,p,v in updates:
                q=safe_int(item.get('quantity')); iid=((v or {}).get('inventoryItem') or {}).get('id')
                if q is not None and iid and LOCATION_ID: inv.append({'inventoryItemId':iid,'locationId':LOCATION_ID,'quantity':q,'changeFromQuantity':None})
            inventory_rows=[]
            for n in range(0,len(inv),200):
                chunk=inv[n:n+200]; idem=hashlib.sha256((digest+json.dumps(chunk,sort_keys=True)).encode()).hexdigest()
                inventory_rows.append({'input':{'name':'available','reason':'correction','referenceDocumentUri':'gid://bgshopping/LorelliBulk/'+digest,'quantities':chunk},'idempotencyKey':idem})
            opname=operation_name('Inventory',digest); s['phase']='inventory'; op,errs=run_bulk(session,opname,mutation(opname,'inventory'),inventory_rows,s,deadline)
            if str((op or {}).get('status'))!='COMPLETED': s['state']='checkpoint_wait'; write_status(s); return s
            if errs: s['state']='completed_with_errors'; s['failed']+=len(errs); s['errors']+=errs; write_status(s); return s

            # Fingerprint is written only after product, variant and inventory phases succeeded.
            mf=[]
            for item,p,v in updates:
                owner=(v or {}).get('id')
                if owner: mf.append({'ownerId':owner,'namespace':'supplier_sync','key':'fingerprint','type':'single_line_text_field','value':item['_fp']})
            final_rows=[{'metafields':mf[n:n+25]} for n in range(0,len(mf),25)]
            opname=operation_name('Finalize',digest); s['phase']='finalize'; op,errs=run_bulk(session,opname,mutation(opname,'finalize'),final_rows,s,deadline)
            if str((op or {}).get('status'))!='COMPLETED': s['state']='checkpoint_wait'; write_status(s); return s
            if errs: s['state']='completed_with_errors'; s['failed']+=len(errs); s['errors']+=errs; write_status(s); return s

        s.update(state='completed',phase='done',created=len(create_rows),updated=len(updates),finished_at=time.time(),duration_seconds=round(time.time()-started,2)); write_status(s); return s
    except Exception as e:
        s.update(state='failed',fatal_error=f'{type(e).__name__}: {e}',finished_at=time.time(),duration_seconds=round(time.time()-started,2)); write_status(s); return s

if __name__=='__main__': run()
