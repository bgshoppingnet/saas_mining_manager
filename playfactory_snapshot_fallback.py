import os, json, struct, requests
from io import BytesIO
from PIL import Image

CATALOG_PATH=os.getenv('SUPPLIER_OUT_PATH','/tmp/supplier-catalog.jsonl')
PAYLOAD_URL=os.getenv('PLAYFACTORY_SNAPSHOT_IMAGE_URL','')

def rows_from_snapshot():
    r=requests.get(PAYLOAD_URL,timeout=(15,120)); r.raise_for_status()
    raw=Image.open(BytesIO(r.content)).convert('RGB').tobytes()
    n=struct.unpack('>I',raw[:4])[0]
    return [json.loads(x) for x in raw[4:4+n].decode('utf-8').splitlines() if x.strip()]

def inject_if_missing():
    keep=[]; found=0
    if os.path.exists(CATALOG_PATH):
        with open(CATALOG_PATH,encoding='utf-8') as f:
            for line in f:
                if not line.strip(): continue
                x=json.loads(line)
                if str(x.get('supplier') or '').lower()=='playfactory': found+=1
                else: keep.append(x)
    if found: return {'injected':0,'reason':'already_present','existing_playfactory':found}
    rows=rows_from_snapshot()
    tmp=CATALOG_PATH+'.pf.tmp'
    with open(tmp,'w',encoding='utf-8') as f:
        for x in keep+rows: f.write(json.dumps(x,ensure_ascii=False)+'\n')
    os.replace(tmp,CATALOG_PATH)
    return {'injected':len(rows),'reason':'shopify_cdn_snapshot'}
