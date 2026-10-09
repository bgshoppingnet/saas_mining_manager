import json, hashlib, importlib
import bulk_bgelectronics_sync as base


def run():
    mod=importlib.reload(base)

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
        return 'BGSKikkaBooV1'+phase+d

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
        pids={p['id'] for p,v,s in use}
        vids={v['id'] for p,v,s in use if v}
        if len(pids)==1 and len(vids)<=1:
            p=use[0][0]
            v=next((v for pp,v,s in use if v),None)
            if not v and len(p.get('variants_list') or [])==1:
                v=p['variants_list'][0]
            return p,v,False
        return None,None,True

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

    old_write=mod.core.write_status
    def write_status(status):
        if isinstance(status,dict) and status.get('supplier')=='BGElectronics':
            status['supplier']='KikkaBoo'
        old_write(status)

    mod.load_rows=load_rows
    mod.op=op
    mod.product_metafields=product_metafields
    mod.choose=choose
    mod.product_fields=product_fields
    mod.create_vars=create_vars
    mod.core.write_status=write_status
    try:
        result=mod.run()
        if isinstance(result,dict):
            result['supplier']='KikkaBoo'
        return result
    finally:
        mod.core.write_status=old_write


if __name__=='__main__':
    run()
