"""Prepare conservative corrections; never write prices or inventory."""
import collections
import difflib
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

TASK = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()


def plain(value):
    return ' '.join(html.unescape(re.sub('<[^>]+>', ' ', value or '')).split())


def norm(value):
    return re.sub(r'\W+', ' ', html.unescape(value or '').casefold()).strip()


def similarity(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def classify(title, description):
    t = norm(title)
    rules = [
        (r'^автостол\b', 'Столчета за кола'),
        (r'^сет\b', 'Спални комплекти'),
        (r'^подложка cosy travel\b', 'Аксесоари за бебешки колички'),
        (r'\b(?:вложка|ключ|клещи|нитачка|шпакловане|тресчотка|отвертка|ножовка|секач|длето)\b', 'Ръчни инструменти'),
        (r'\b(?:анкер|болт|винт|гайка|дюбел)\b', 'Крепежни елементи'),
        (r'\b(?:маркуч|полив|съединител за маркуч)\b', 'Поливни системи'),
        (r'\b(?:лопата|мотика|гребло|ножица за клони|градински трион|пръскачка)\b', 'Градински инструменти'),
        (r'\bработни обувки\b', 'Работно облекло и обувки'),
        (r'\b(?:прахосмукачка|перфоратор|бормашина|ъглошлайф|винтоверт|полирмашина|електрически трион)\b', 'Електроинструменти'),
        (r'\b(?:батерия|зарядно|накрайник|свредло|диск|плот|патронник|адаптер|шлаух)\b', 'Аксесоари за инструменти'),
    ]
    for pattern, category in rules:
        if re.search(pattern, t):
            if category == 'Спални комплекти' and not all(w in norm(description) for w in ['завивка', 'чаршаф']):
                continue
            return category
    return None


def main():
    products, variants = {}, collections.defaultdict(list)
    for line in (TASK / 'catalog-full.jsonl').open():
        d = json.loads(line)
        if d['id'].startswith('gid://shopify/Product/'):
            products[d['id']] = d
        else:
            variants[d['__parentId']].append(d)
    em = list(ET.parse(TASK / 'supplier-reference/productfeedshops.xml').getroot().find('items'))
    bg = list(ET.parse(TASK / 'supplier-reference/bgshopping.net/indexbgelectron.xml').getroot())
    em_sku, em_id = collections.defaultdict(list), {}
    for x in em:
        em_sku[x.findtext('sku')].append(x)
        em_id[x.findtext('product_id')] = x
    bg_id = {x.findtext('product_id'): x for x in bg}
    by_name = collections.defaultdict(list)
    for p in products.values():
        by_name[(norm(p['vendor']), norm(p['title']))].append(p)
    updates, sku_updates, image_candidates, reasons = [], [], [], collections.Counter()
    evidence = []
    for pid, p in products.items():
        if pid == 'gid://shopify/Product/16685363986777':
            continue
        vs = variants[pid]
        patch = {'id': pid}
        source, kind = None, None
        if p['handle'].startswith('euromaster-'):
            candidate = em_id.get(p['handle'].split('euromaster-', 1)[1])
            if candidate is not None and similarity(p['title'], candidate.findtext('name_bg')) >= .76:
                source, kind = candidate, 'euromaster'
        if source is None:
            em_candidates = [x for v in vs for x in em_sku.get(v.get('sku'), []) if similarity(p['title'], x.findtext('name_bg')) >= .83]
            em_candidates = {x.findtext('product_id'): x for x in em_candidates}
            if len(em_candidates) == 1:
                source, kind = next(iter(em_candidates.values())), 'euromaster'
        if source is None:
            for v in vs:
                sku = v.get('sku') or ''
                if sku.startswith('BGE-'):
                    candidate = bg_id.get(sku[4:])
                    if candidate is not None and similarity(p['title'], candidate.findtext('product_name')) >= .80:
                        source, kind = candidate, 'bgelectronics'
                        break
        donors = [d for d in by_name[(norm(p['vendor']), norm(p['title']))] if d['id'] != pid]
        if not p['productType']:
            categories = {d['productType'] for d in donors if d['productType']}
            if len(categories) == 1:
                patch['productType'] = next(iter(categories))
            elif source is not None:
                path = source.findtext('categories') if kind == 'euromaster' else source.findtext('category_full')
                parts = [s.strip() for s in re.split(r'[,|>]', path or '') if s.strip()]
                if parts:
                    patch['productType'] = parts[-1]
            if not patch.get('productType'):
                category = classify(p['title'], p['descriptionHtml'])
                if category:
                    patch['productType'] = category
            if patch.get('productType'):
                reasons['category'] += 1
        body = p['descriptionHtml']
        original = plain(body)
        if source is not None and (len(original) < 120 or norm(original) == norm(p['title'])):
            rich = source.findtext('description_bg') if kind == 'euromaster' else source.findtext('product_description')
            if rich and len(plain(rich)) > len(original) + 50:
                rich = re.sub(r'<(?:script|iframe|style)\b[^>]*>[\s\S]*?</(?:script|iframe|style)>', '', rich, flags=re.I)
                rich = re.sub(r'\s+on\w+\s*=\s*([\"\']).*?\1', '', rich, flags=re.I)
                rich = re.sub(r'(href|src)\s*=\s*([\"\'])javascript:[^\"\']*\2', '', rich, flags=re.I)
                body = rich
                reasons['supplier_description'] += 1
        elif not original:
            donor_texts = {d['descriptionHtml'] for d in donors if plain(d['descriptionHtml'])}
            if len(donor_texts) == 1:
                body = next(iter(donor_texts))
                reasons['exact_description'] += 1
        if source is not None and kind == 'bgelectronics':
            fixed, n = re.subn(r'(href\s*=\s*[\"\'])(/download/[^\"\']+)', r'\1https://www.bgelectronics.eu\2', body, flags=re.I)
            if n:
                body = fixed
                reasons['supplier_document_links'] += n
        if body != p['descriptionHtml']:
            patch['descriptionHtml'] = body
        if not p.get('seo', {}).get('title') or re.search(r'decko\.bg|baulpro\.com|patilanci', str(p.get('seo')), re.I):
            title = plain(p['title'])
            if len(title) <= 62:
                title += ' | BGShopping'
            seo = {'title': title[:255], 'description': p.get('seo', {}).get('description') or plain(p['title'])}
            if re.search(r'decko\.bg|baulpro\.com|patilanci', str(p.get('seo')), re.I):
                seo['description'] = (plain(p['title']) + '. Разгледайте характеристиките, цената и наличността в BGShopping.net.')[:320]
            patch['seo'] = seo
            reasons['seo'] += 1
        if not p.get('featuredMedia'):
            urls = set()
            for donor in donors:
                url = ((donor.get('featuredMedia') or {}).get('preview') or {}).get('image') or {}
                if url.get('url'):
                    urls.add(url['url'])
            image = next(iter(urls)) if len(urls) == 1 else None
            if not image and source is not None:
                image = source.findtext('image') if kind == 'euromaster' else source.findtext('images/image')
            if image and image.startswith('https://') and not re.search(r'no[-_]?image|placeholder|default[-_]?image|logo', image, re.I):
                image_candidates.append({'id': pid, 'url': image, 'title': p['title'], 'source': kind or 'exact_title_vendor'})
        if len(vs) == 1 and not vs[0].get('sku') and source is not None:
            sku = source.findtext('sku') if kind == 'euromaster' else 'BGE-' + source.findtext('product_id')
            if sku:
                sku_updates.append({'productId': pid, 'variants': [{'id': vs[0]['id'], 'inventoryItem': {'sku': sku}}]})
                reasons['sku'] += 1
        if len(patch) > 1:
            updates.append({'product': patch})
            evidence.append({'id': pid, 'source': kind, 'source_id': source.findtext('product_id') if source is not None else None, 'fields': list(patch)[1:]})
    for name, rows in [('catalog-product-repair.jsonl', updates), ('catalog-sku-repair.jsonl', sku_updates)]:
        with (TASK / name).open('w') as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
    (TASK / 'catalog-image-candidates.json').write_text(json.dumps(image_candidates, ensure_ascii=False))
    report = {'scanned_products': len(products), 'product_updates': len(updates), 'sku_updates': len(sku_updates), 'image_candidates': len(image_candidates), 'changes': dict(reasons), 'evidence': evidence}
    (TASK / 'catalog-repair-plan.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != 'evidence'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
