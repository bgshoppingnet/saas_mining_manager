import json, hashlib, importlib
import bulk_bgelectronics_sync as base


def run():
    mod=importlib.reload(base)
    matched_variant_ids={}

    def load_rows():
        out=[]
        with open(mod.CATALOG_PATH,encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                x=json.loads(line)
                if mod.low(x.get('supplier'))!='kikkaboo':
                    continue
                x['_fp']=str(x.get('fingerprint') or '') or hashlib.sha256(json.dumps(x,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                out.append(x)
        return out

    def op(phase,d):
        return 'BGSKikkaBooV2'+phase+d

    def product_metafields(item):
        fields=[
            {'namespace':'supplier_sync','key':'supplier','type':'single_line_text_field','value':'KikkaBoo'},
            {'namespace':'supplier_sync','key':'source_key','type':'single_line_text_field','value':mod.source_id(item)},
        ]
        if str(item.get('url') or '').startswith('http'):
            fields.append({'namespace':'supplier_sync','key':'source_url','type':'url','value':str(item['url'])[:2048]})
        return fields

    def choose(cands):
        if not cands:
            return None,None,False
        tagged=[x for x in cands if x[2]=='kikkaboo']
        use=tagged if tagged else cands
        dedup={(p['id'],(v or {}).get('id')):(p,v,s) for p,v,s in use}
        use=list(dedup.values())
        pids={p['id'] for p,v,s in use}
        vids={v['id'] for p,v,s in use if v}
        if len(pids)==1 and len(vids)<=1:
            p=use[0][0]
            v=next((v for pp,v,s in use if v),None)
            if not v and len(p.get('variants_list') or [])==1:
                v=p['variants_list'][0]
            return p,v,False
        return None,None,True

    def remember(item,v):
        if not v or not v.get('id'):
            return
        for raw in (mod.source_id(item),item.get('sku'),item.get('ean'),item.get('key')):
            k=mod.low(raw)
            if k:
                matched_variant_ids[k]=v['id']

    def exact_one(cands):
        if not cands:
            return None,None,False
        p,v,conf=choose(cands)
        if p and not conf:
            return p,v,False
        dedup={(p['id'],(v or {}).get('id')):(p,v,s) for p,v,s in cands}
        if len(dedup)==1:
            p,v,s=next(iter(dedup.values()))
            if not v and len(p.get('variants_list') or [])==1:
                v=p['variants_list'][0]
            return p,v,False
        return None,None,True

    def smart_match(item,idx):
        source,sku,ean=idx
        sid=mod.low(mod.source_id(item))
        s=mod.low(item.get('sku'))
        e=mod.low(item.get('ean'))

        if sid and source.get(sid):
            p,v,conf=exact_one(source[sid])
            if p and not conf:
                remember(item,v)
                return p,v,False

        sc=list(sku.get(s,[])) if s else []
        ec=list(ean.get(e,[])) if e else []

        if sc and ec:
            sm={(p['id'],(v or {}).get('id')):(p,v,supp) for p,v,supp in sc}
            em={(p['id'],(v or {}).get('id')):(p,v,supp) for p,v,supp in ec}
            common=[sm[k] for k in sm.keys() & em.keys()]
            p,v,conf=exact_one(common)
            if p and not conf:
                remember(item,v)
                return p,v,False

        if ec:
            p,v,conf=exact_one(ec)
            if p and not conf:
                remember(item,v)
                return p,v,False

        if sc:
            p,v,conf=exact_one(sc)
            if p and not conf:
                remember(item,v)
                return p,v,False

        union={(p['id'],(v or {}).get('id')):(p,v,supp) for p,v,supp in sc+ec}
        if union:
            return None,None,True
        return None,None,False

    def product_fields(item,pid=None):
        name=mod.norm(item.get('name')) or mod.norm(item.get('sku')) or 'KikkaBoo продукт'
        out={
            'title':name[:255],
            'descriptionHtml':mod.description_html(item),
            'vendor':mod.norm(item.get('brand')) or 'KikkaBoo',
            'seo':mod.seo(item),
            'metafields':product_metafields(item),
        }
        leaf=mod.category_leaf(item)
        if leaf:
            out['productType']=leaf
        if pid:
            out['id']=pid
        return out

    def create_vars(item):
        sid=mod.source_id(item)
        if not sid:
            return None
        p=product_fields(item)
        p['handle']=mod.slug('kikkaboo-'+sid)
        p['status']='ACTIVE'
        p['tags']=['supplier:KikkaBoo','market:BG','market:EU']
        p['productOptions']=[{'name':'Title','position':1,'values':[{'name':'Default Title'}]}]
        v=mod.variant_fields(item,final=True)
        v['optionValues']=[{'optionName':'Title','name':'Default Title'}]
        q=mod.qty(item.get('quantity'))
        if q is not None and mod.LOCATION_ID:
            v['inventoryQuantities']=[{'locationId':mod.LOCATION_ID,'name':'available','quantity':q}]
        p['variants']=[v]
        pics=mod.images(item)
        if pics:
            p['files']=[{'originalSource':u,'contentType':'IMAGE','alt':mod.norm(item.get('name'))[:512],'duplicateResolutionMode':'APPEND_UUID'} for u in pics]
        return {'identifier':{'handle':p['handle']},'input':p}

    old_inventory_match=mod.invcore.match
    def smart_inventory_match(item,indexes):
        sku_idx,ean_idx=indexes
        wanted=set()
        for raw in (mod.source_id(item),item.get('sku'),item.get('ean'),item.get('key')):
            k=mod.low(raw)
            if k and matched_variant_ids.get(k):
                wanted.add(matched_variant_ids[k])
        candidates=[]
        s=mod.low(item.get('sku')); e=mod.low(item.get('ean'))
        if s:
            candidates.extend(sku_idx.get(s,[]))
        if e:
            candidates.extend(ean_idx.get(e,[]))
        dedup={x['id']:x for x in candidates}
        if wanted:
            exact=[v for vid,v in dedup.items() if vid in wanted]
            if len(exact)==1:
                return exact[0],False
        if e:
            ec={x['id']:x for x in ean_idx.get(e,[])}
            if len(ec)==1:
                return next(iter(ec.values())),False
        if s:
            sc={x['id']:x for x in sku_idx.get(s,[])}
            if len(sc)==1:
                return next(iter(sc.values())),False
        if len(dedup)==1:
            return next(iter(dedup.values())),False
        return (None,True) if dedup else (None,False)

    old_write=mod.core.write_status
    def write_status(status):
        if isinstance(status,dict) and status.get('supplier')=='BGElectronics':
            status['supplier']='KikkaBoo'
        old_write(status)

    mod.load_rows=load_rows
    mod.op=op
    mod.product_metafields=product_metafields
    mod.choose=choose
    mod.match=smart_match
    mod.product_fields=product_fields
    mod.create_vars=create_vars
    mod.invcore.match=smart_inventory_match
    mod.core.write_status=write_status
    try:
        result=mod.run()
        if isinstance(result,dict):
            result['supplier']='KikkaBoo'
        return result
    finally:
        mod.invcore.match=old_inventory_match
        mod.core.write_status=old_write


if __name__=='__main__':
    run()
