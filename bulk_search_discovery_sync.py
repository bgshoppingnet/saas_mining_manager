import os, json, time, hashlib
from collections import defaultdict
import requests
import bulk_bgelectronics_sync as base
import seo_multilingual as searchseo

CATALOG_PATH=os.getenv('SUPPLIER_OUT_PATH','/tmp/supplier-catalog.jsonl')
MAX_WAIT=int(os.getenv('SHOPIFY_BULK_MAX_WAIT_SECONDS','680') or '680')
VERSION='V1'
MARKER='search-discovery:v1'


def load_rows():
    out=[]
    with open(CATALOG_PATH,encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            x=json.loads(line)
            if not (searchseo.norm(x.get('name')) or searchseo.norm(x.get('sku')) or searchseo.norm(x.get('ean'))):
                continue
            out.append(x)
    return out


def op(phase,d):
    return 'BGSSearchDiscovery'+VERSION+phase+d


def digest(rows):
    h=hashlib.sha256()
    h.update(VERSION.encode())
    for x in sorted(rows,key=lambda z:(base.low(z.get('supplier')),base.low(z.get('sku')),base.low(z.get('ean')),base.low(z.get('external_id')))):
        h.update('|'.join([
            base.low(x.get('supplier')),base.low(x.get('sku')),base.low(x.get('ean')),
            base.low(x.get('external_id')),base.low(x.get('name')),base.low(x.get('category')),
            base.low(x.get('brand'))
        ]).encode())
    return h.hexdigest()[:14].upper()


def dedup_candidates(cands):
    return {(p['id'],(v or {}).get('id')):(p,v,s) for p,v,s in cands}


def one_product(cands):
    vals=list(dedup_candidates(cands).values())
    if not vals:
        return None,None,False
    pids={p['id'] for p,v,s in vals}
    if len(pids)!=1:
        return None,None,True
    p=vals[0][0]
    variants=[v for pp,v,s in vals if v]
    vids={v['id'] for v in variants}
    v=variants[0] if len(vids)==1 else (p.get('variants_list') or [None])[0] if len(p.get('variants_list') or [])==1 else None
    return p,v,False


def match(item,idx):
    source,sku,ean=idx
    supplier=base.low(item.get('supplier'))
    sid=base.low(base.source_id(item))
    s=base.low(item.get('sku'))
    e=base.low(item.get('ean'))

    if sid and source.get(sid):
        tagged=[c for c in source[sid] if c[2]==supplier]
        p,v,conf=one_product(tagged)
        if p and not conf:
            return p,v,False

    sc=list(sku.get(s,[])) if s else []
    ec=list(ean.get(e,[])) if e else []
    if sc and ec:
        sm=dedup_candidates(sc); em=dedup_candidates(ec)
        common=[sm[k] for k in sm.keys() & em.keys()]
        p,v,conf=one_product(common)
        if p and not conf:
            return p,v,False

    if ec:
        p,v,conf=one_product(ec)
        if p and not conf:
            return p,v,False
    if sc:
        p,v,conf=one_product(sc)
        if p and not conf:
            return p,v,False

    union=list(dedup_candidates(sc+ec).values())
    return (None,None,True) if union else (None,None,False)


def tag_mutation(name):
    return f'''mutation {name}($id:ID!,$tags:[String!]!){{tagsAdd(id:$id,tags:$tags){{node{{id}} userErrors{{field message}}}}}}'''


def seo_mutation(name):
    return f'''mutation {name}($product:ProductUpdateInput!){{productUpdate(product:$product){{product{{id}} userErrors{{field message}}}}}}'''


def run():
    started=time.time(); deadline=started+MAX_WAIT
    status={'state':'running','supplier':'ALL','phase':'start','total':0,'matched':0,'optimized':0,'unchanged':0,'conflicts':0,'unmatched':0,'failed':0,'errors':[],'started_at':started}
    base.core.write_status(status)
    try:
        rows=load_rows(); status['total']=len(rows)
        d=digest(rows); status['checkpoint']=d; base.core.write_status(status)
        base.core_patch.install(base.core)
        with requests.Session() as session:
            status['phase']='index'; name=op('Index',d)
            products,bulk=base.core.ensure_index(session,name,status,deadline)
            if products is None:
                status.update(state='checkpoint_wait'); base.core.write_status(status); return status
            idx=base.build_indexes(products)
            chosen={}; conflicts=0; unmatched=0
            for item in rows:
                p,v,conf=match(item,idx)
                if conf:
                    conflicts+=1; continue
                if not p:
                    unmatched+=1; continue
                chosen[p['id']] = (item,p)
            status.update(matched=len(chosen),conflicts=conflicts,unmatched=unmatched); base.core.write_status(status)

            tag_rows=[]; seo_rows=[]; unchanged=0
            for pid,(item,p) in chosen.items():
                aliases=searchseo.search_tags(item,item.get('supplier'))
                current=[str(x) for x in (p.get('tags') or [])]
                current_lower={x.lower() for x in current}
                missing=[x for x in aliases if x.lower() not in current_lower]
                if MARKER.lower() not in current_lower:
                    missing.append(MARKER)
                room=max(0,240-len(current))
                missing=missing[:room]
                if not missing:
                    unchanged+=1
                    continue
                tag_rows.append({'id':pid,'tags':missing})
                seo_rows.append({'product':{'id':pid,'seo':searchseo.seo(item)}})

            status.update(optimized=len(tag_rows),unchanged=unchanged,phase='tags'); base.core.write_status(status)
            name=op('Tags',d)
            bulk,errs=base.core.run_bulk(session,name,tag_mutation(name),tag_rows,status,deadline)
            if str((bulk or {}).get('status'))!='COMPLETED':
                status.update(state='checkpoint_wait'); base.core.write_status(status); return status
            if errs:
                status.update(state='completed_with_errors',failed=len(errs),errors=errs[:100]); base.core.write_status(status); return status

            status['phase']='seo'; base.core.write_status(status)
            name=op('Seo',d)
            bulk,errs=base.core.run_bulk(session,name,seo_mutation(name),seo_rows,status,deadline)
            if str((bulk or {}).get('status'))!='COMPLETED':
                status.update(state='checkpoint_wait'); base.core.write_status(status); return status
            if errs:
                status.update(state='completed_with_errors',failed=len(errs),errors=errs[:100]); base.core.write_status(status); return status

        status.update(state='completed',phase='done',finished_at=time.time(),duration_seconds=round(time.time()-started,2))
        base.core.write_status(status)
        return status
    except Exception as e:
        status.update(state='failed',fatal_error=f'{type(e).__name__}: {e}',finished_at=time.time(),duration_seconds=round(time.time()-started,2))
        base.core.write_status(status)
        return status


if __name__=='__main__':
    run()
