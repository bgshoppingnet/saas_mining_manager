from flask import Flask,Response,jsonify
import os,re,html,json,time,threading,requests
from urllib.parse import quote
from run_catalog_sync_v3 import run as run_catalog_sync
from run_catalog_sync import ensure_shopify_access_token
app=Flask(__name__)
PUBLIC_SHOP_URL=os.getenv('PUBLIC_SHOP_URL','https://bgshopping.net').rstrip('/')
VAT_RATE=float(os.getenv('VAT_RATE','0.20')); REFRESH_SECONDS=int(os.getenv('REFRESH_SECONDS','14400'))
ENABLE_CATALOG_SYNC=os.getenv('ENABLE_CATALOG_SYNC','true').lower() in ('1','true','yes','on')
SHOPIFY_API_VERSION=os.getenv('SHOPIFY_API_VERSION','2026-10').strip()
PAZARUVAJ_PATH='/tmp/pazaruvaj.xml'; GOOGLE_PATH='/tmp/google-shopping.xml'; AI_PATH='/tmp/ai-products.json'
CATALOG_STATUS_PATH=os.getenv('CATALOG_SYNC_STATUS_PATH','/tmp/catalog-sync-status.json')
SUPPLIER_STATUS_PATH=os.getenv('SUPPLIER_STATUS_PATH','/tmp/supplier-status.json')
SHOPIFY_STATUS_PATH=os.getenv('SHOPIFY_SYNC_STATUS_PATH','/tmp/shopify-sync-status.json')
state={'building':False,'last_ok':None,'last_error':None,'products':0,'variants':0,'bytes':0,'pages':0,'google_items':0,'google_bytes':0,'ai_items':0,'ai_bytes':0}; lock=threading.Lock()
PRODUCTS_QUERY='''query FeedProducts($cursor:String){products(first:100,after:$cursor,query:"status:active",sortKey:ID){pageInfo{hasNextPage endCursor}nodes{id title handle vendor productType descriptionHtml featuredImage{url} images(first:3){nodes{url}} brand:metafield(namespace:"custom",key:"brand"){value} model:metafield(namespace:"custom",key:"model"){value} measurements:metafield(namespace:"custom",key:"measurements"){value} ingredients:metafield(namespace:"custom",key:"ingredients"){value} material:metafield(namespace:"custom",key:"material"){value} variants(first:20){nodes{id title sku barcode price availableForSale image{url}}}}}}}'''
LABELS={'weight':'Тегло','length':'Дължина','width':'Ширина','height':'Височина','depth':'Дълбочина','diameter':'Диаметър','volume':'Обем','capacity':'Вместимост','voltage':'Напрежение','power':'Мощност','battery_capacity':'Капацитет на батерията','torque':'Въртящ момент','pressure':'Налягане','frequency':'Честота','speed':'Обороти','unit_mentions':'Технически стойности'}
def esc(v): return html.escape(str(v or ''),quote=False)
def strip_html(s): return ' '.join(html.unescape(re.sub(r'<[^>]+>',' ',s or '')).split())
def mf(p,k): return str(((p.get(k) or {}).get('value')) or '').strip()
def measures(raw):
    try: v=json.loads(raw) if raw else {}
    except Exception: return {}
    if not isinstance(v,dict): return {}
    out={}
    for k,x in v.items():
        if isinstance(x,list):
            z=[' '.join(str(i).split()) for i in x if str(i).strip()]
            if z: out[str(k)]=z[:24]
        elif isinstance(x,(str,int,float)):
            z=' '.join(str(x).split())
            if z: out[str(k)]=z[:160]
    return out
def read_status(path):
    try:
        with open(path,encoding='utf-8') as f:return json.load(f)
    except FileNotFoundError:return None
    except Exception as e:return {'state':'status_read_error','error':f'{type(e).__name__}: {e}'}
def config():
    raw=os.getenv('SUPPLIER_FEEDS_JSON','').strip(); n=1
    if raw:
        try:n=len([x for x in json.loads(raw) if x.get('enabled',True) and x.get('url')])
        except Exception:n=None
    return {'enabled':ENABLE_CATALOG_SYNC,'refresh_seconds':REFRESH_SECONDS,'supplier_feed_count':n,'shopify_write_enabled':os.getenv('SHOPIFY_WRITE_ENABLED','false').lower() in ('1','true','yes','on'),'shopify_domain_configured':bool(os.getenv('SHOPIFY_SHOP_DOMAIN','').strip()),'shopify_token_configured':bool(os.getenv('SHOPIFY_ADMIN_ACCESS_TOKEN','').strip()),'shopify_client_id_configured':bool(os.getenv('SHOPIFY_CLIENT_ID','').strip()),'shopify_client_secret_configured':bool(os.getenv('SHOPIFY_CLIENT_SECRET','').strip()),'shopify_location_configured':bool(os.getenv('SHOPIFY_LOCATION_ID','').strip())}
def gql(session,q,vars=None):
    shop=os.getenv('SHOPIFY_SHOP_DOMAIN','').strip(); token=os.getenv('SHOPIFY_ADMIN_ACCESS_TOKEN','').strip()
    if not shop or not token: raise RuntimeError('Shopify Admin API is not configured after authentication')
    url=f'https://{shop}/admin/api/{SHOPIFY_API_VERSION}/graphql.json'
    for attempt in range(1,11):
        try:
            r=session.post(url,headers={'X-Shopify-Access-Token':token,'Content-Type':'application/json','User-Agent':'BGShopping-Commerce-Feeds/9.0'},json={'query':q,'variables':vars or {}},timeout=(20,120))
            if r.status_code==429: time.sleep(min(int(r.headers.get('Retry-After') or '2'),15)); continue
            r.raise_for_status(); data=r.json(); errs=data.get('errors') or []
            if errs:
                if any((e.get('extensions') or {}).get('code')=='THROTTLED' for e in errs if isinstance(e,dict)): time.sleep(min(2*attempt,15)); continue
                raise RuntimeError('Shopify GraphQL: '+json.dumps(errs,ensure_ascii=False))
            return data.get('data') or {}
        except requests.RequestException:
            if attempt>=10: raise
            time.sleep(min(2*attempt,15))
    raise RuntimeError('Shopify GraphQL retries exhausted')
def base_data(p):
    h=(p.get('handle') or '').strip()
    if not h:return None
    imgs=[]
    for x in ((p.get('images') or {}).get('nodes') or [])[:3]:
        u=((x or {}).get('url') or '').strip()
        if u and u not in imgs:imgs.append(u)
    f=((p.get('featuredImage') or {}).get('url') or '').strip()
    if f and f not in imgs:imgs.insert(0,f)
    return {'handle':h,'brand':mf(p,'brand') or (p.get('vendor') or '').strip(),'model':mf(p,'model'),'measurements':measures(mf(p,'measurements')),'ingredients':mf(p,'ingredients'),'material':mf(p,'material'),'product_type':(p.get('productType') or '').strip(),'description':strip_html(p.get('descriptionHtml') or ''),'title':(p.get('title') or '').strip(),'images':imgs[:3]}
def variant(base,v):
    if not base:return None
    try: price=float(v.get('price') or 0)
    except Exception:return None
    if price<=0:return None
    vid=str(v.get('id') or '').rsplit('/',1)[-1]; sku=(v.get('sku') or '').strip(); barcode=(v.get('barcode') or '').strip(); ident=sku or barcode or vid
    if not ident:return None
    title=base['title']; vt=(v.get('title') or '').strip()
    if vt and vt!='Default Title':title=f'{title} - {vt}'
    link=f"{PUBLIC_SHOP_URL}/products/{quote(base['handle'])}"+(f'?variant={vid}' if vid else '')
    imgs=[]; vi=(((v.get('image') or {}).get('url')) or '').strip()
    if vi:imgs.append(vi)
    for x in base['images']:
        if x and x not in imgs:imgs.append(x)
        if len(imgs)>=3:break
    return {**base,'id':ident,'variant_id':vid,'sku':sku,'barcode':barcode,'title':title,'link':link,'price':price,'net_price':price/(1+VAT_RATE),'available':bool(v.get('availableForSale',False)),'images':imgs[:3]}
def attr(n,v):
    if isinstance(v,list):v=', '.join(str(x) for x in v if str(x).strip())
    v=' '.join(str(v or '').split())
    return '' if not v else f'<Attribute><Attribute_Name>{esc(n)}</Attribute_Name><Attribute_Value>{esc(v[:1200])}</Attribute_Value></Attribute>'
def pazar(d):
    if not d or not d['available']:return ''
    p=['<product>',f"<identifier>{esc(d['id'])}</identifier>",f"<manufacturer>{esc(d['brand'])}</manufacturer>",f"<name>{esc(d['title'])}</name>",f"<category>{esc(d['product_type'])}</category>",f"<product_url>{esc(d['link'])}</product_url>",f"<price>{d['price']:.2f}</price>",f"<net_price>{d['net_price']:.2f}</net_price>"]
    if d['sku']:p.append(f"<sku>{esc(d['sku'])}</sku>")
    if d['barcode']:p.append(f"<ean>{esc(d['barcode'])}</ean>")
    if d['description']:p.append(f"<description>{esc(d['description'])}</description>")
    for n,v in [('Модел',d['model']),('Материал',d['material']),('Съставки',d['ingredients'])]:
        a=attr(n,v)
        if a:p.append(a)
    for k,v in d['measurements'].items():
        a=attr(LABELS.get(k,k.replace('_',' ').title()),v)
        if a:p.append(a)
    for i,u in enumerate(d['images'],1):
        t='Image_url' if i==1 else 'Image_url_'+str(i); p.append(f'<{t}>{esc(u)}</{t}>')
    p+=['<delivery_time>1</delivery_time>','</product>']; return '\n'.join(p)+'\n'
def google(d):
    if not d:return ''
    p=['<item>',f"<g:id>{esc(d['id'])}</g:id>",f"<title>{esc(d['title'])}</title>",f"<link>{esc(d['link'])}</link>",f"<g:availability>{'in_stock' if d['available'] else 'out_of_stock'}</g:availability>",'<g:condition>new</g:condition>',f"<g:price>{d['price']:.2f} EUR</g:price>"]
    if d['description']:p.append(f"<description>{esc(d['description'])}</description>")
    if d['brand']:p.append(f"<g:brand>{esc(d['brand'])}</g:brand>")
    if d['product_type']:p.append(f"<g:product_type>{esc(d['product_type'])}</g:product_type>")
    if d['barcode']:p.append(f"<g:gtin>{esc(d['barcode'])}</g:gtin>")
    if d['model']:p.append(f"<g:mpn>{esc(d['model'])}</g:mpn>")
    elif not d['barcode']:p.append('<g:identifier_exists>false</g:identifier_exists>')
    if d['images']:
        p.append(f"<g:image_link>{esc(d['images'][0])}</g:image_link>")
        for u in d['images'][1:]:p.append(f"<g:additional_image_link>{esc(u)}</g:additional_image_link>")
    p.append('</item>'); return '\n'.join(p)+'\n'
def prop(n,v):
    if isinstance(v,list):v=', '.join(str(x) for x in v if str(x).strip())
    v=' '.join(str(v or '').split()); return {'@type':'PropertyValue','name':n,'value':v} if v else None
def ai(d):
    props=[prop(LABELS.get(k,k.replace('_',' ').title()),v) for k,v in d['measurements'].items()]
    props += [prop('Материал',d['material']),prop('Съставки',d['ingredients'])]; props=[x for x in props if x]
    x={'@type':'Product','id':d['id'],'name':d['title'],'url':d['link'],'image':d['images'],'brand':{'@type':'Brand','name':d['brand']} if d['brand'] else None,'category':d['product_type'] or None,'model':d['model'] or None,'mpn':d['model'] or None,'sku':d['sku'] or None,'gtin':d['barcode'] or None,'description':d['description'] or None,'material':d['material'] or None,'additionalProperty':props or None,'offers':{'@type':'Offer','price':f"{d['price']:.2f}",'priceCurrency':'EUR','availability':'https://schema.org/InStock' if d['available'] else 'https://schema.org/OutOfStock','url':d['link']}}
    return {k:v for k,v in x.items() if v is not None}
def build():
    with lock:
        if state['building']:return False
        state['building']=True; state['last_error']=None; state['pages']=0
    tmp=[PAZARUVAJ_PATH+'.tmp',GOOGLE_PATH+'.tmp',AI_PATH+'.tmp']
    try:
        auth=ensure_shopify_access_token(); s=requests.Session(); pp=pv=gi=aii=page=0; cursor=None; first=True
        with open(tmp[0],'w',encoding='utf-8',newline='\n') as pf,open(tmp[1],'w',encoding='utf-8',newline='\n') as gf,open(tmp[2],'w',encoding='utf-8',newline='\n') as af:
            pf.write('<?xml version="1.0" encoding="UTF-8"?>\n<products>\n'); gf.write('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">\n<channel>\n<title>BGShopping.net Product Feed</title>\n'+f'<link>{esc(PUBLIC_SHOP_URL)}</link>\n<description>BGShopping.net product catalog for shopping channels</description>\n'); af.write('{"@context":"https://schema.org","generated_at":'+json.dumps(time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))+',"currency":"EUR","products":[\n')
            while True:
                page+=1; conn=(gql(s,PRODUCTS_QUERY,{'cursor':cursor}).get('products') or {})
                for p in conn.get('nodes') or []:
                    b=base_data(p); wrote=False
                    for v in ((p.get('variants') or {}).get('nodes') or []):
                        d=variant(b,v)
                        if not d:continue
                        z=pazar(d)
                        if z:pf.write(z); pv+=1; wrote=True
                        z=google(d)
                        if z:gf.write(z); gi+=1
                        z=ai(d)
                        if z:
                            if not first:af.write(',\n')
                            af.write(json.dumps(z,ensure_ascii=False,separators=(',',':'))); first=False; aii+=1
                    if wrote:pp+=1
                with lock:state.update(pages=page,products=pp,variants=pv,google_items=gi,ai_items=aii)
                pi=conn.get('pageInfo') or {}
                if not pi.get('hasNextPage'):break
                cursor=pi.get('endCursor')
                if not cursor or page>=1000:break
            pf.write('</products>\n'); gf.write('</channel>\n</rss>\n'); af.write('\n]}\n')
            for f in (pf,gf,af):f.flush();os.fsync(f.fileno())
        sizes=[os.path.getsize(x) for x in tmp]
        if pp<=0 or pv<=0 or sizes[0]<1000:raise RuntimeError('Pazaruvaj feed integrity failed')
        if gi<=0 or sizes[1]<1000:raise RuntimeError('Google feed integrity failed')
        if aii<=0 or sizes[2]<1000:raise RuntimeError('AI feed integrity failed')
        for a,b in zip(tmp,[PAZARUVAJ_PATH,GOOGLE_PATH,AI_PATH]):os.replace(a,b)
        with lock:state.update(last_ok=time.time(),products=pp,variants=pv,bytes=sizes[0],pages=page,google_items=gi,google_bytes=sizes[1],ai_items=aii,ai_bytes=sizes[2],last_error=None)
        print(json.dumps({'feed_build':'completed','products':pp,'variants':pv,'google_items':gi,'ai_items':aii,'source':'Shopify Admin GraphQL + canonical metafields','auth_mode':(auth or {}).get('mode')},ensure_ascii=False),flush=True); return True
    except Exception as e:
        for x in tmp:
            try:os.remove(x)
            except Exception:pass
        with lock:state['last_error']=f'{type(e).__name__}: {e}'
        print(json.dumps({'feed_build':'failed','error':state['last_error']},ensure_ascii=False),flush=True); return False
    finally:
        with lock:state['building']=False
def loop():
    while True:
        if ENABLE_CATALOG_SYNC:
            try:
                r=run_catalog_sync(); print(json.dumps({'catalog_sync':'finished','state':(r or {}).get('state')},ensure_ascii=False),flush=True)
            except Exception as e:print(json.dumps({'catalog_sync_error':f'{type(e).__name__}: {e}'},ensure_ascii=False),flush=True)
        build(); time.sleep(REFRESH_SECONDS)
def serve(path,ctype):
    if not os.path.exists(path) or os.path.getsize(path)<=1000:return Response('Feed is being generated.\n',503,content_type='text/plain; charset=utf-8',headers={'Retry-After':'60','Cache-Control':'no-store'})
    with open(path,'rb') as f:payload=f.read()
    r=Response(payload,200,content_type=ctype);r.headers['Cache-Control']='public, max-age=900, stale-while-revalidate=3600';r.headers['Content-Length']=str(len(payload));r.add_etag();return r
@app.get('/')
def home():return jsonify(service='BGShopping Commerce Feeds',version='9.0',pazaruvaj='/pazaruvaj.xml',google_shopping='/google-shopping.xml',ai_products='/ai-products.json',feed_status='/feed-status',catalog_status='/catalog-status',supplier_status='/supplier-status',shopify_status='/shopify-sync-status')
@app.get('/health')
def health():return jsonify(ok=True,version='9.0')
@app.get('/feed-status')
def feed_status():
    with lock:d=dict(state)
    d.update(pazaruvaj_ready=os.path.exists(PAZARUVAJ_PATH) and os.path.getsize(PAZARUVAJ_PATH)>1000,google_ready=os.path.exists(GOOGLE_PATH) and os.path.getsize(GOOGLE_PATH)>1000,ai_ready=os.path.exists(AI_PATH) and os.path.getsize(AI_PATH)>1000)
    if d['last_ok']:d['last_ok_iso']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(d['last_ok']))
    return jsonify(d)
@app.get('/catalog-status')
def catalog_status():return jsonify(config=config(),status=read_status(CATALOG_STATUS_PATH))
@app.get('/supplier-status')
def supplier_status():return jsonify(status=read_status(SUPPLIER_STATUS_PATH))
@app.get('/shopify-sync-status')
def shopify_status():return jsonify(config=config(),status=read_status(SHOPIFY_STATUS_PATH))
@app.get('/pazaruvaj.xml')
def pazaruvaj():return serve(PAZARUVAJ_PATH,'application/xml; charset=utf-8')
@app.get('/google-shopping.xml')
def google_shopping():return serve(GOOGLE_PATH,'application/xml; charset=utf-8')
@app.get('/ai-products.json')
def ai_products():return serve(AI_PATH,'application/ld+json; charset=utf-8')
threading.Thread(target=loop,name='bgshopping-catalog-and-feeds',daemon=True).start()
if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
