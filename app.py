from flask import Flask, Response, jsonify
import os, time, html, requests
from urllib.parse import quote

app = Flask(__name__)
SHOP_URL = os.getenv('SHOP_URL','https://bgshopping.net').rstrip('/')
CACHE_SECONDS = int(os.getenv('CACHE_SECONDS','14400'))
VAT_RATE = float(os.getenv('VAT_RATE','0.20'))
_cache = {'ts':0,'xml':None,'count':0}

def esc(v):
    return html.escape(str(v or ''), quote=False)

def strip_html(s):
    import re
    s = re.sub(r'<[^>]+>', ' ', s or '')
    return ' '.join(html.unescape(s).split())

def fetch_products():
    products=[]
    page=1
    while True:
        url=f"{SHOP_URL}/products.json?limit=250&page={page}"
        r=requests.get(url,timeout=30,headers={'User-Agent':'BGShopping-Pazaruvaj-Feed/1.0'})
        r.raise_for_status()
        batch=r.json().get('products',[])
        if not batch: break
        products.extend(batch)
        if len(batch)<250: break
        page+=1
        if page>500: break
    return products

def build_xml():
    products=fetch_products()
    out=['<?xml version="1.0" encoding="UTF-8"?>','<products>']
    count=0
    for p in products:
        handle=p.get('handle','')
        vendor=p.get('vendor','')
        ptype=p.get('product_type','')
        desc=strip_html(p.get('body_html',''))
        images=p.get('images') or []
        for v in p.get('variants') or []:
            if not v.get('available',True):
                continue
            vid=v.get('id','')
            title=p.get('title','')
            vtitle=v.get('title','')
            if vtitle and vtitle!='Default Title':
                title=f"{title} - {vtitle}"
            price=v.get('price') or '0'
            try:
                gross=float(price)
                net=gross/(1+VAT_RATE)
            except Exception:
                net=0
            link=f"{SHOP_URL}/products/{quote(handle)}?variant={vid}"
            sku=v.get('sku','')
            barcode=v.get('barcode') or ''
            out.append('<product>')
            out.append(f'<identifier>{esc(vid)}</identifier>')
            out.append(f'<manufacturer>{esc(vendor)}</manufacturer>')
            out.append(f'<name>{esc(title)}</name>')
            out.append(f'<category>{esc(ptype)}</category>')
            out.append(f'<product_url>{esc(link)}</product_url>')
            out.append(f'<price>{esc(price)}</price>')
            out.append(f'<net_price>{net:.2f}</net_price>')
            if sku: out.append(f'<sku>{esc(sku)}</sku>')
            if barcode: out.append(f'<ean>{esc(barcode)}</ean>')
            if desc: out.append(f'<description>{esc(desc)}</description>')
            for i,img in enumerate(images[:3],start=1):
                src=img.get('src') or ''
                if src: out.append(f'<image{i}>{esc(src)}</image{i}>')
            out.append('<delivery_time>1</delivery_time>')
            out.append('</product>')
            count+=1
    out.append('</products>')
    return '\n'.join(out), count

def current_xml():
    now=time.time()
    if _cache['xml'] and now-_cache['ts']<CACHE_SECONDS:
        return _cache['xml'], _cache['count']
    xml,count=build_xml()
    _cache.update(ts=now,xml=xml,count=count)
    return xml,count

@app.get('/')
def home():
    return jsonify(service='BGShopping Pazaruvaj Feed', feed='/pazaruvaj.xml', health='/health')

@app.get('/health')
def health():
    return jsonify(ok=True, shop=SHOP_URL)

@app.get('/pazaruvaj.xml')
def pazaruvaj():
    xml,count=current_xml()
    return Response(xml, content_type='application/xml; charset=utf-8', headers={'X-Product-Count':str(count),'Cache-Control':f'public, max-age={CACHE_SECONDS}'})

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
