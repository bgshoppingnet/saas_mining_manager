"""Build validated shopping feeds from the canonical Shopify snapshot."""
import copy
import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1] / 'feeds'
G = 'http://base.google.com/ns/1.0'
ET.register_namespace('g', G)


def text(value, limit=1500):
    return ' '.join(html.unescape(re.sub(r'<[^>]*>', ' ', str(value or ''))).split())[:limit]


def clean_collection_years(name, brand='', url=''):
    """Remove collection years, retaining model/compatibility years and units."""
    context = f'{name} {brand} {url}'
    if not re.search(r'kikkaboo|kinderkraft|avionaut', context, re.I):
        return name
    if re.search(r'протектор|мобилен телефон|слушалки|каталог', name, re.I):
        return name
    return re.sub(r'\s+', ' ', re.sub(r'(?<![\w-])20[12]\d(?![\w-])(?:\s*г\.)?', '', name)).strip(' -–')


def specific_category(row):
    """Map the reported mixed product families to verified Pazaruvaj paths."""
    name, sku = row.get('name', ''), str(row.get('sku') or '')
    category = row.get('category') or ''
    if sku == 'PAT44943' or (category in ('Общи продукти', 'Играчки') and re.search(r'спално бельо|спален комплект', name, re.I)):
        return 'Мебели, обзавеждане > Домашен текстил > Спално бельо, спален комплект'
    if sku == 'PAT61850' or (category in ('Общи продукти', 'Играчки') and re.search(r'детск\w* китар', name, re.I)):
        return 'Играчки > Образователни игри > Детски музикални инструменти'
    if sku == '14780' or (category in ('Общи продукти', 'Играчки', 'Детски велосипеди') and re.search(r'детск\w* велосипед', name, re.I)):
        return 'Спорт и фитнес > Велосипедизъм > Велосипеди'
    return category


def stable_id(url):
    part = urlsplit(url)
    vid = parse_qs(part.query).get('variant', [''])[0]
    if part.hostname not in ('bgshopping.net', 'www.bgshopping.net') or not part.path.startswith('/products/') or not vid.isdigit():
        raise ValueError('Invalid canonical variant URL')
    return 'BG-' + vid


def prepare(source):
    rows, excluded, seen = [], Counter(), set()
    for original in source['products']:
        row = copy.deepcopy(original)
        try:
            row['id'] = stable_id(row.get('url', ''))
            offers = row.get('offers') or {}
            price = Decimal(str(offers.get('price', '0')))
            if not price.is_finite() or price <= 0 or offers.get('priceCurrency') != 'EUR':
                raise ValueError('Invalid EUR price')
            images = row.get('image') or []
            if isinstance(images, str):
                images = [images]
            images = list(dict.fromkeys(u for u in images if isinstance(u, str) and urlsplit(u).scheme == 'https' and urlsplit(u).netloc and not re.search(r'no[-_]?image|placeholder|default[-_]?image', u, re.I)))
            if not images:
                excluded['missing_image'] += 1
                continue
            if row['id'] in seen:
                excluded['duplicate_variant'] += 1
                continue
            brand_value = row.get('brand') or ''
            brand_value = brand_value.get('name', '') if isinstance(brand_value, dict) else str(brand_value)
            row['name'] = clean_collection_years(text(row.get('name'), 250), brand_value, row.get('url', ''))
            row['category'] = specific_category(row)
            if not row['name']:
                raise ValueError('Missing name')
            row['image'] = images[:3]
            row['description'] = text(row.get('description'), 550)
            row['offers']['price'] = format(price.quantize(Decimal('.01')), '.2f')
            brand = row.get('brand') or ''
            row['brand'] = text(brand.get('name') if isinstance(brand, dict) else brand, 150)
            seen.add(row['id'])
            rows.append(row)
        except (ValueError, InvalidOperation, TypeError, KeyError):
            excluded['invalid_record'] += 1
    return rows, dict(excluded)


def add(parent, tag, value):
    ET.SubElement(parent, tag).text = str(value or '')


def write_xml(path, root):
    # Compact output keeps full catalog within Shopify Files 20 MB limit.
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)


def build(source, output=ROOT):
    output.mkdir(parents=True, exist_ok=True)
    rows, excluded = prepare(source)
    if not rows:
        raise ValueError('Refusing to publish empty feeds')
    available = [p for p in rows if p['offers'].get('availability') == 'https://schema.org/InStock']
    available_ids = {p['id'] for p in available}
    if not available:
        raise ValueError('Refusing to publish empty available feed')
    pazar = ET.Element('products')
    for p in available:
        node = ET.SubElement(pazar, 'product')
        for tag, value in [('identifier', p['id']), ('manufacturer', p['brand']), ('name', p['name']), ('category', p.get('category')), ('product_url', p['url']), ('price', p['offers']['price']), ('net_price', format((Decimal(p['offers']['price']) / Decimal('1.20')).quantize(Decimal('.01')), '.2f')), ('sku', p.get('sku')), ('ean', p.get('gtin')), ('description', p['description'])]:
            if value:
                add(node, tag, value)
        for i, image in enumerate(p['image']):
            add(node, 'Image_url' if i == 0 else f'Image_url_{i+1}', image)
        for name, value in [('Модел', p.get('model') or p.get('mpn')), ('Материал', p.get('material'))]:
            if value:
                attribute = ET.SubElement(node, 'Attribute')
                add(attribute, 'Attribute_Name', name)
                add(attribute, 'Attribute_Value', value)
        for prop in p.get('additionalProperty') or []:
            if prop.get('name') and prop.get('value'):
                attribute = ET.SubElement(node, 'Attribute')
                add(attribute, 'Attribute_Name', prop['name'])
                add(attribute, 'Attribute_Value', prop['value'])
    write_xml(output / 'pazaruvaj.xml', pazar)

    rss = ET.Element('rss', version='2.0')
    channel = ET.SubElement(rss, 'channel')
    add(channel, 'title', 'BGShopping product catalog')
    add(channel, 'link', 'https://bgshopping.net/')
    add(channel, 'description', 'BGShopping Shopify product snapshot')
    for p in rows:
        node = ET.SubElement(channel, 'item')
        for tag, value in [('id', p['id']), ('title', p['name']), ('description', p['description'] or p['name']), ('link', p['url']), ('image_link', p['image'][0]), ('price', p['offers']['price'] + ' EUR'), ('availability', 'in_stock' if p['id'] in available_ids else 'out_of_stock'), ('condition', 'new'), ('brand', p['brand']), ('product_type', p.get('category')), ('gtin', p.get('gtin')), ('mpn', p.get('mpn') or p.get('model'))]:
            if value:
                add(node, '{' + G + '}' + tag, value)
        if not p.get('gtin') and not p.get('mpn') and not p.get('model'):
            add(node, '{' + G + '}identifier_exists', 'false')
        for image in p['image'][1:]:
            add(node, '{' + G + '}additional_image_link', image)
    write_xml(output / 'google-shopping.xml', rss)

    store = ET.Element('store')
    products = ET.SubElement(store, 'products')
    shop_rows = sorted(available, key=lambda p: p['id'])[:10000]
    for p in shop_rows:
        node = ET.SubElement(products, 'Product')
        for tag, value in [('MPC', p['id']), ('Category', p.get('category')), ('Manufacturer', p['brand']), ('MPN', p.get('mpn') or p.get('model')), ('Name', p['name']), ('Description', p['description'] or p['name']), ('URL', p['url']), ('Image', p['image'][0]), ('Price', p['offers']['price']), ('Currency', 'EUR'), ('Availability', 'In stock'), ('GTIN', p.get('gtin'))]:
            if value:
                add(node, tag, value)
    write_xml(output / 'shopmania.xml', store)
    clean_source = {**source, 'products': rows}
    (output / 'ai-products.json').write_text(json.dumps(clean_source, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    report = {'source_generated_at': source.get('generated_at'), 'currency': 'EUR', 'source_rows': len(source['products']), 'pazaruvaj_products': len(available), 'google_products': len(rows), 'ai_products': len(rows), 'shopmania_products': len(shop_rows), 'shopmania_limit': 10000, 'excluded': excluded}
    for filename in ['pazaruvaj.xml', 'google-shopping.xml', 'shopmania.xml']:
        ET.parse(output / filename)
    for filename in ['pazaruvaj.xml', 'shopmania.xml']:
        if (output / filename).stat().st_size > (20_000_000 if filename == 'shopmania.xml' else 100_000_000):
            raise ValueError('Feed exceeds supported file size')
    (output / 'feed-status.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'index.html').write_text('<!doctype html><meta charset="utf-8"><title>BGShopping product feeds</title><h1>BGShopping product feeds</h1><ul>' + ''.join(f'<li><a href="/{p}">{p}</a></li>' for p in ['pazaruvaj.xml', 'shopmania.xml', 'google-shopping.xml', 'ai-products.json', 'feed-status.json']) + '</ul>', encoding='utf-8')
    return report


if __name__ == '__main__':
    snapshot = json.loads((ROOT / 'ai-products.json').read_text(encoding='utf-8'))
    print(json.dumps(build(snapshot), ensure_ascii=False))
