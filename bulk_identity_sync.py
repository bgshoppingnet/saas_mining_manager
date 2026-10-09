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
VERSION = 'V3'


def norm(value):
    return ' '.join(str(value or '').split()).strip()


def canonical_brand(value):
    raw = norm(value)
    low = raw.casefold()
    if not raw:
        return ''
    if low.startswith('raider pro') or low in {'raiderpro', 'raider pro tools'}:
        return 'Raider Pro'
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
    for key in ('model', 'mpn', 'manufacturer_part_number', 'manufacturer_code', 'product_code', 'model_code'):
        value = norm(item.get(key))
        if value and plausible_model(value):
            return value[:48]

    title = norm(item.get('name'))
    for token in re.findall(r'\b[A-Za-zА-Яа-я0-9]+(?:[-_.+/][A-Za-zА-Яа-я0-9]+)+\b', title):
        if plausible_model(token):
            return token[:48]
    for token in re.findall(r'\b[A-Za-z]{1,10}\d[A-Za-z0-9]{1,18}\b', title):
        if plausible_model(token):
            return token[:48]

    external_id = norm(item.get('external_id'))
    if external_id and plausible_model(external_id) and re.search(r'[-_.+/]', external_id):
        return external_id[:48]
    return ''


FIELD_UNITS = {
    'weight_g': 'g', 'weight_kg': 'kg', 'net_weight_g': 'g', 'net_weight_kg': 'kg',
    'gross_weight_g': 'g', 'gross_weight_kg': 'kg',
    'length_mm': 'mm', 'length_cm': 'cm', 'length_m': 'm',
    'width_mm': 'mm', 'width_cm': 'cm', 'width_m': 'm',
    'height_mm': 'mm', 'height_cm': 'cm', 'height_m': 'm',
    'depth_mm': 'mm', 'depth_cm': 'cm', 'depth_m': 'm',
    'diameter_mm': 'mm', 'diameter_cm': 'cm',
    'volume_ml': 'ml', 'volume_l': 'l', 'capacity_ml': 'ml', 'capacity_l': 'l',
    'voltage_v': 'V', 'power_w': 'W', 'power_kw': 'kW',
    'battery_capacity_ah': 'Ah', 'battery_capacity_mah': 'mAh',
    'torque_nm': 'Nm', 'pressure_bar': 'bar', 'frequency_hz': 'Hz', 'speed_rpm': 'rpm',
}

MEASURE_ALIASES = {
    'weight': ('weight', 'net_weight', 'gross_weight', 'тегло', 'нето тегло', 'бруто тегло'),
    'length': ('length', 'дължина'),
    'width': ('width', 'ширина'),
    'height': ('height', 'височина'),
    'depth': ('depth', 'дълбочина'),
    'diameter': ('diameter', 'диаметър'),
    'volume': ('volume', 'обем'),
    'capacity': ('capacity', 'вместимост'),
    'voltage': ('voltage', 'напрежение'),
    'power': ('power', 'мощност'),
    'battery_capacity': ('battery capacity', 'battery_capacity', 'капацитет на батерията'),
    'torque': ('torque', 'въртящ момент'),
    'pressure': ('pressure', 'налягане'),
    'frequency': ('frequency', 'честота'),
    'speed': ('rpm', 'speed', 'обороти'),
}

UNIT_RE = re.compile(r'(?<![\w])\d+(?:[.,]\d+)?\s?(?:mm|cm|kg|g|ml|l|m|kW|W|V|Ah|mAh|Nm|bar|MPa|rpm|Hz)(?![\w])', re.I)


def scalar_text(value):
    if value is None:
        return ''
    if isinstance(value, (str, int, float)):
        return norm(value)
    return ''


def add_measure(out, key, value, unit=''):
    raw = scalar_text(value)
    if not raw:
        return
    if unit and re.fullmatch(r'[-+]?\d+(?:[.,]\d+)?', raw):
        raw = f'{raw} {unit}'
    if len(raw) > 80:
        return
    out[key] = raw


def extract_measurements(item):
    out = {}
    for key, unit in FIELD_UNITS.items():
        if key in item:
            base_key = re.sub(r'_(?:kg|g|mm|cm|m|ml|l|v|w|kw|ah|mah|nm|bar|hz|rpm)$', '', key, flags=re.I)
            add_measure(out, base_key, item.get(key), unit)

    for canonical, aliases in MEASURE_ALIASES.items():
        if canonical in out:
            continue
        for alias in aliases:
            if alias in item:
                add_measure(out, canonical, item.get(alias))
                if canonical in out:
                    break

    attrs = item.get('attributes') or item.get('specifications') or item.get('specs')
    pairs = []
    if isinstance(attrs, dict):
        pairs = list(attrs.items())
    elif isinstance(attrs, list):
        for x in attrs:
            if isinstance(x, dict):
                k = x.get('name') or x.get('key') or x.get('label')
                v = x.get('value') or x.get('text')
                if k is not None and v is not None:
                    pairs.append((k, v))

    for key, value in pairs:
        kl = norm(key).casefold()
        for canonical, aliases in MEASURE_ALIASES.items():
            if canonical in out:
                continue
            if any(a.casefold() in kl for a in aliases):
                add_measure(out, canonical, value)

    mentions = []
    for field in ('name', 'description', 'short_description'):
        text = scalar_text(item.get(field))
        if not text:
            continue
        for match in UNIT_RE.findall(text):
            val = norm(match)
            if val and val.casefold() not in {m.casefold() for m in mentions}:
                mentions.append(val)
            if len(mentions) >= 24:
                break
        if len(mentions) >= 24:
            break
    if mentions:
        out['unit_mentions'] = mentions
    return out


def find_named_value(item, direct_keys, labels):
    for key in direct_keys:
        val = item.get(key)
        if isinstance(val, list):
            text = ', '.join(norm(x) for x in val if norm(x))
        else:
            text = scalar_text(val)
        if text:
            return text[:5000]

    attrs = item.get('attributes') or item.get('specifications') or item.get('specs')
    pairs = []
    if isinstance(attrs, dict):
        pairs = list(attrs.items())
    elif isinstance(attrs, list):
        for x in attrs:
            if isinstance(x, dict):
                k = x.get('name') or x.get('key') or x.get('label')
                v = x.get('value') or x.get('text')
                if k is not None and v is not None:
                    pairs.append((k, v))
    for key, value in pairs:
        kl = norm(key).casefold()
        if any(label in kl for label in labels):
            text = scalar_text(value)
            if text:
                return text[:5000]
    return ''


def infer_ingredients(item):
    return find_named_value(
        item,
        ('ingredients', 'ingredients_text', 'inci', 'sastavki', 'ingredient_list'),
        ('съставки', 'ingredients', 'ingredient list', 'inci')
    )


def infer_material(item):
    value = find_named_value(
        item,
        ('material', 'materials', 'product_material'),
        ('материал', 'material')
    )
    return value[:255]


def operation(phase, digest):
    return f'BGSIdentity{VERSION}{phase}{digest}'


def rows_digest(rows):
    h = hashlib.sha256()
    h.update(VERSION.encode())
    keys = (
        'supplier','sku','ean','external_id','brand','name','model','mpn','description',
        'weight','weight_g','weight_kg','length','width','height','depth','diameter',
        'volume','capacity','voltage','power','battery_capacity','torque','pressure',
        'frequency','speed','ingredients','ingredients_text','inci','material','materials'
    )
    for item in sorted(rows, key=lambda x: (
        base.low(x.get('supplier')), base.low(x.get('sku')),
        base.low(x.get('ean')), base.low(x.get('external_id'))
    )):
        h.update('|'.join(base.low(item.get(k)) for k in keys).encode())
    return h.hexdigest()[:14].upper()


def mutation(name):
    return f'''mutation {name}($metafields:[MetafieldsSetInput!]!){{metafieldsSet(metafields:$metafields){{metafields{{id namespace key value}} userErrors{{field message}}}}}}'''


def run():
    started = time.time()
    deadline = started + MAX_WAIT
    status = {
        'state': 'running', 'supplier': 'ALL', 'phase': 'start', 'total': 0,
        'matched': 0, 'brand_products': 0, 'model_products': 0,
        'measurement_products': 0, 'ingredient_products': 0, 'material_products': 0,
        'ambiguous_brand': 0, 'ambiguous_model': 0, 'ambiguous_measurements': 0,
        'ambiguous_ingredients': 0, 'ambiguous_material': 0,
        'conflicts': 0, 'unmatched': 0, 'failed': 0, 'errors': [], 'started_at': started,
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
            identities = defaultdict(lambda: {
                'brands': set(), 'models': set(), 'measurements': set(),
                'ingredients': set(), 'materials': set()
            })
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
                bucket = identities[product['id']]
                brand = canonical_brand(item.get('brand') or item.get('manufacturer'))
                model = infer_model(item)
                measurements = extract_measurements(item)
                ingredients = infer_ingredients(item)
                material = infer_material(item)
                if brand:
                    bucket['brands'].add(brand)
                if model:
                    bucket['models'].add(model)
                if measurements:
                    bucket['measurements'].add(json.dumps(measurements, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
                if ingredients:
                    bucket['ingredients'].add(ingredients)
                if material:
                    bucket['materials'].add(material)

            metafields = []
            counters = defaultdict(int)
            for product_id, values in identities.items():
                mapping = [
                    ('brands', 'brand', 'single_line_text_field', 'brand_products', 'ambiguous_brand'),
                    ('models', 'model', 'single_line_text_field', 'model_products', 'ambiguous_model'),
                    ('measurements', 'measurements', 'json', 'measurement_products', 'ambiguous_measurements'),
                    ('ingredients', 'ingredients', 'multi_line_text_field', 'ingredient_products', 'ambiguous_ingredients'),
                    ('materials', 'material', 'single_line_text_field', 'material_products', 'ambiguous_material'),
                ]
                for source_key, mf_key, mf_type, ok_key, amb_key in mapping:
                    vals = {x for x in values[source_key] if x}
                    if len(vals) == 1:
                        metafields.append({
                            'ownerId': product_id,
                            'namespace': 'custom',
                            'key': mf_key,
                            'type': mf_type,
                            'value': next(iter(vals)),
                        })
                        counters[ok_key] += 1
                    elif len(vals) > 1:
                        counters[amb_key] += 1

            status.update(
                matched=matched, conflicts=conflicts, unmatched=unmatched,
                metafields_queued=len(metafields), phase='metafields', **counters
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
