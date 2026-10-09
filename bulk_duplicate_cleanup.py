import os, json, time
from collections import defaultdict
import requests
import bulk_shopify_sync as core

CYCLE_SECONDS = int(os.getenv('LORELLI_DUPLICATE_CYCLE_SECONDS', '86400') or '86400')


def norm(v):
    return ' '.join(str(v or '').split())


def low(v):
    return norm(v).lower()


def cycle_id():
    return str(int(time.time() // CYCLE_SECONDS))


def index_name(cycle):
    return 'BGSLorelliDuplicateIndex' + cycle


def archive_name(cycle):
    return 'BGSLorelliArchiveLegacy' + cycle


def index_query(name):
    return f'''query {name} {{
  products {{
    edges {{
      node {{
        __typename
        id
        handle
        title
        status
        vendor
        variants {{
          edges {{
            node {{
              __typename
              id
              sku
              barcode
            }}
          }}
        }}
      }}
    }}
  }}
}}'''


def archive_mutation(name):
    return f'''mutation {name}($product: ProductUpdateInput!) {{
  productUpdate(product: $product) {{
    product {{ id status handle }}
    userErrors {{ field message }}
  }}
}}'''


def parse_index(url):
    r = requests.get(url, timeout=(20, 180))
    r.raise_for_status()
    products = {}
    pending = defaultdict(list)
    for line in r.text.splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        oid = str(o.get('id') or '')
        parent = o.get('__parentId')
        if oid.startswith('gid://shopify/Product/') and not oid.startswith('gid://shopify/ProductVariant/'):
            products[oid] = {
                'id': oid,
                'handle': o.get('handle') or '',
                'title': o.get('title') or '',
                'status': o.get('status') or '',
                'vendor': o.get('vendor') or '',
                'variants': [],
            }
        elif oid.startswith('gid://shopify/ProductVariant/') and parent:
            pending[parent].append({
                'id': oid,
                'sku': norm(o.get('sku')),
                'barcode': norm(o.get('barcode')),
            })
    for pid, variants in pending.items():
        if pid in products:
            products[pid]['variants'].extend(variants)
    return list(products.values())


def ensure_index(session, name, status, deadline):
    op = core.recent(session, name, 'query')
    if not op or str(op.get('status')) in ('FAILED', 'CANCELED', 'EXPIRED'):
        out = core.gql(session, core.START_QUERY, {'query': index_query(name)}).get('bulkOperationRunQuery') or {}
        if out.get('userErrors'):
            raise RuntimeError(str(out['userErrors']))
        op = out.get('bulkOperation') or {}
    op = core.wait(session, op, status, deadline)
    if str((op or {}).get('status')) != 'COMPLETED' or not op.get('url'):
        return None, op
    return parse_index(op['url']), op


def canonical_maps(products):
    sku_map = defaultdict(set)
    ean_map = defaultdict(set)
    canonical_ids = set()
    for p in products:
        if not low(p.get('handle')).startswith('lorelli-'):
            continue
        canonical_ids.add(p['id'])
        for v in p.get('variants') or []:
            s = low(v.get('sku'))
            e = low(v.get('barcode'))
            if s:
                sku_map[s].add(p['id'])
            if e:
                ean_map[e].add(p['id'])
    return canonical_ids, sku_map, ean_map


def variant_coverage(v, sku_map, ean_map):
    s = low(v.get('sku'))
    e = low(v.get('barcode'))
    if not s and not e:
        return False, 'no_identifier'

    sets = []
    if s:
        hits = set(sku_map.get(s, set()))
        if not hits:
            return False, 'sku_uncovered'
        sets.append(hits)
    if e:
        hits = set(ean_map.get(e, set()))
        if not hits:
            return False, 'ean_uncovered'
        sets.append(hits)

    common = sets[0]
    for x in sets[1:]:
        common = common & x
    if len(common) != 1:
        return False, 'ambiguous_or_conflicting'
    return True, next(iter(common))


def classify(products):
    canonical_ids, sku_map, ean_map = canonical_maps(products)
    safe = []
    unresolved = []
    candidates = 0

    for p in products:
        if p['id'] in canonical_ids:
            continue
        if str(p.get('status') or '').upper() == 'ARCHIVED':
            continue
        variants = p.get('variants') or []
        if not variants:
            continue

        any_overlap = False
        all_covered = True
        reasons = []
        for v in variants:
            s = low(v.get('sku'))
            e = low(v.get('barcode'))
            if (s and sku_map.get(s)) or (e and ean_map.get(e)):
                any_overlap = True
            ok, reason = variant_coverage(v, sku_map, ean_map)
            if not ok:
                all_covered = False
                reasons.append({'variantId': v.get('id'), 'sku': v.get('sku'), 'barcode': v.get('barcode'), 'reason': reason})

        if not any_overlap:
            continue
        candidates += 1
        if all_covered:
            safe.append(p)
        else:
            unresolved.append({'id': p['id'], 'handle': p.get('handle'), 'title': p.get('title'), 'reasons': reasons[:10]})

    return canonical_ids, safe, unresolved, candidates


def run():
    started = time.time()
    deadline = started + int(os.getenv('SHOPIFY_BULK_MAX_WAIT_SECONDS', '680') or '680')
    cycle = cycle_id()
    status = {
        'state': 'running',
        'supplier': 'Lorelli',
        'phase': 'duplicate_index',
        'cycle': cycle,
        'scanned': 0,
        'canonical': 0,
        'duplicate_candidates': 0,
        'archive_queued': 0,
        'archived': 0,
        'unresolved': 0,
        'failed': 0,
        'errors': [],
        'started_at': started,
    }
    core.write_status(status)
    try:
        with requests.Session() as session:
            products, op = ensure_index(session, index_name(cycle), status, deadline)
            if products is None:
                status.update(state='checkpoint_wait', phase='duplicate_index')
                core.write_status(status)
                return status

            canonical_ids, safe, unresolved, candidates = classify(products)
            status.update(
                scanned=len(products),
                canonical=len(canonical_ids),
                duplicate_candidates=candidates,
                archive_queued=len(safe),
                unresolved=len(unresolved),
            )
            status['unresolved_examples'] = unresolved[:20]
            status['phase'] = 'archive'
            core.write_status(status)

            rows = [{'product': {'id': p['id'], 'status': 'ARCHIVED'}} for p in safe]
            name = archive_name(cycle)
            op, errors = core.run_bulk(session, name, archive_mutation(name), rows, status, deadline)
            if str((op or {}).get('status')) != 'COMPLETED':
                status.update(state='checkpoint_wait', phase='archive')
                core.write_status(status)
                return status

            if errors:
                status['failed'] = len(errors)
                status['errors'] = errors
                status['state'] = 'completed_with_errors'
            else:
                status['archived'] = len(safe)
                status['state'] = 'completed'
            status['phase'] = 'done'
            status['finished_at'] = time.time()
            status['duration_seconds'] = round(status['finished_at'] - started, 2)
            core.write_status(status)
            return status
    except Exception as e:
        status.update(
            state='failed',
            fatal_error=f'{type(e).__name__}: {e}',
            finished_at=time.time(),
        )
        status['duration_seconds'] = round(status['finished_at'] - started, 2)
        core.write_status(status)
        return status


if __name__ == '__main__':
    run()
