import hashlib
import json
import os
import re
import time
from collections import defaultdict

import requests

import bulk_search_discovery_sync as searchsync
import bulk_bgelectronics_sync as base

MAX_WAIT = int(os.getenv('SHOPIFY_BULK_MAX_WAIT_SECONDS', '680') or '680')
VERSION = 'V1'


def norm(value):
    return ' '.join(str(value or '').split()).strip()


def canonical_brand(value):
    raw = norm(value)
    low = raw.casefold()
    if not raw:
        return ''
    if low.startswith('raider'):
        return 'Raider'
    if low in {'kikaboo', 'kikka boo', 'kikka-boo'}:
        return 'KikkaBoo'
    if low in {'loreli', 'lorelli'}:
        return 'Lorelli'
    if low.startswith('metabo'):
        return 'Metabo'
    if low.startswith('kinderkraft'):
        return 'Kinderkraft'
    if low.startswith('topmaster') or low.startswith('top master'):
        return 'TopMaster'
    if low.startswith('topgarden') or low.startswith('top garden'):
        return 'TopGarden'
    if low.startswith('baukraft'):
        return 'Baukraft'
    if low.startswith('detech') or low.startswith('de tech'):
        return 'DeTech'
    if low.startswith('gardenx'):
        return 'GardenX'
    if low.startswith('hisense'):
        return 'Hisense'
    if low.startswith('motorola'):
        return 'Motorola'
    if low.startswith('grundig'):
        return 'Grundig'
    if low.startswith('avionaut'):
        return 'Avionaut'
    if low.startswith('cosatto'):
        return 'Cosatto'
    if low.startswith('nuvita'):
        return 'Nuvita'
    if low.startswith('reer'):
        return 'Reer'
    return raw


UNIT_CODES = {
    'V', 'W', 'KW', 'MM', 'CM', 'M', 'KG', 'G', 'ML', 'L', 'AH', 'MAH',
    'RPM', 'NM', 'HZ', 'DB', 'MPA', 'BAR', 'VOLT', 'WATT'
}


def plausible_model(value):
    token = norm(value).strip('.,;:()[]{}')
    if len(token) < 3 or len(token) > 48:
        return False
    if token.upper() in UNIT_CODES:
        return False
    if re.fullmatch(r'\d+(?:[.,]\d+)?(?:V|W|KW|MM|CM|M|KG|G|ML|L|AH|MAH|RPM|NM|HZ|DB|MPA|BAR)', token, re.I):
        return False
    compact = re.sub(r'[-_.+/\s]', '', token)
    return bool(compact) and any(c.isalpha() for c in token) and any(c.isdigit() for c in token)


def infer_model(item):
    # Prefer explicit supplier fields when they exist.
    for key in ('model', 'mpn', 'manufacturer_part_number', 'manufacturer_code', 'product_code', 'model_code'):
        value = norm(item.get(key))
        if value and plausible_model(value):
            return value[:48]

    # Manufacturer-style model codes in the supplier title are safer than SKU.
    title = norm(item.get('name'))
    for token in re.findall(r'\b[A-Za-zА-Яа-я0-9]+(?:[-_.+/][A-Za-zА-Яа-я0-9]+)+\b', title):
        if plausible_model(token):
            return token[:48]
    for token in re.findall(r'\b[A-Za-z]{1,10}\d[A-Za-z0-9]{1,18}\b', title):
        if plausible_model(token):
            return token[:48]

    # External IDs are accepted only when they look like a manufacturer model.
    external_id = norm(item.get('external_id'))
    if external_id and plausible_model(external_id) and re.search(r'[-_.+/]', external_id):
        return external_id[:48]

    return ''


def operation(phase, digest):
    return f'BGSIdentity{VERSION}{phase}{digest}'


def rows_digest(rows):
    h = hashlib.sha256()
    h.update(VERSION.encode())
    for item in sorted(rows, key=lambda x: (
        base.low(x.get('supplier')), base.low(x.get('sku')),
        base.low(x.get('ean')), base.low(x.get('external_id'))
    )):
        h.update('|'.join([
            base.low(item.get('supplier')), base.low(item.get('sku')),
            base.low(item.get('ean')), base.low(item.get('external_id')),
            base.low(item.get('brand')), base.low(item.get('name')),
            base.low(item.get('model')), base.low(item.get('mpn')),
        ]).encode())
    return h.hexdigest()[:14].upper()


def mutation(name):
    return f'''mutation {name}($metafields:[MetafieldsSetInput!]!){{metafieldsSet(metafields:$metafields){{metafields{{id namespace key value}} userErrors{{field message}}}}}}'''


def run():
    started = time.time()
    deadline = started + MAX_WAIT
    status = {
        'state': 'running', 'supplier': 'ALL', 'phase': 'start', 'total': 0,
        'matched': 0, 'brand_products': 0, 'model_products': 0,
        'ambiguous_brand': 0, 'ambiguous_model': 0, 'conflicts': 0,
        'unmatched': 0, 'failed': 0, 'errors': [], 'started_at': started,
    }
    base.core.write_status(status)

    try:
        rows = searchsync.load_rows()
        status['total'] = len(rows)
        if not rows:
            raise RuntimeError('Supplier catalog produced zero rows for identity sync')
        digest = rows_digest(rows)
        status['checkpoint'] = digest
        base.core.write_status(status)

        base.core_patch.install(base.core)
        with requests.Session() as session:
            status['phase'] = 'index'
            opname = operation('Index', digest)
            products, bulk = base.core.ensure_index(session, opname, status, deadline)
            if products is None:
                status['state'] = 'checkpoint_wait'
                base.core.write_status(status)
                return status

            idx = base.build_indexes(products)
            identities = defaultdict(lambda: {'brands': set(), 'models': set()})
            conflicts = unmatched = matched = 0

            for item in rows:
                product, variant, conflict = searchsync.match(item, idx)
                if conflict:
                    conflicts += 1
                    continue
                if not product:
                    unmatched += 1
                    continue
                matched += 1
                brand = canonical_brand(item.get('brand') or item.get('manufacturer'))
                model = infer_model(item)
                if brand:
                    identities[product['id']]['brands'].add(brand)
                if model:
                    identities[product['id']]['models'].add(model)

            metafields = []
            brand_products = model_products = ambiguous_brand = ambiguous_model = 0
            for product_id, values in identities.items():
                brands = {x for x in values['brands'] if x}
                models = {x for x in values['models'] if x}
                if len(brands) == 1:
                    brand = next(iter(brands))
                    metafields.append({
                        'ownerId': product_id,
                        'namespace': 'custom',
                        'key': 'brand',
                        'type': 'single_line_text_field',
                        'value': brand,
                    })
                    brand_products += 1
                elif len(brands) > 1:
                    ambiguous_brand += 1

                if len(models) == 1:
                    model = next(iter(models))
                    metafields.append({
                        'ownerId': product_id,
                        'namespace': 'custom',
                        'key': 'model',
                        'type': 'single_line_text_field',
                        'value': model,
                    })
                    model_products += 1
                elif len(models) > 1:
                    ambiguous_model += 1

            status.update(
                matched=matched, conflicts=conflicts, unmatched=unmatched,
                brand_products=brand_products, model_products=model_products,
                ambiguous_brand=ambiguous_brand, ambiguous_model=ambiguous_model,
                metafields_queued=len(metafields), phase='metafields'
            )
            base.core.write_status(status)

            payloads = [
                {'metafields': metafields[i:i + 25]}
                for i in range(0, len(metafields), 25)
            ]
            opname = operation('Metafields', digest)
            bulk, errors = base.core.run_bulk(
                session, opname, mutation(opname), payloads, status, deadline
            )
            if str((bulk or {}).get('status')) != 'COMPLETED':
                status['state'] = 'checkpoint_wait'
                base.core.write_status(status)
                return status
            if errors:
                status.update(state='completed_with_errors', failed=len(errors), errors=errors[:100])
                base.core.write_status(status)
                return status

        status.update(
            state='completed', phase='done', finished_at=time.time(),
            duration_seconds=round(time.time() - started, 2)
        )
        base.core.write_status(status)
        return status
    except Exception as exc:
        status.update(
            state='failed', fatal_error=f'{type(exc).__name__}: {exc}',
            finished_at=time.time(), duration_seconds=round(time.time() - started, 2)
        )
        base.core.write_status(status)
        return status


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False))
