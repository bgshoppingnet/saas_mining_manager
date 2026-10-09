import re


BG_MAP = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ж':'zh','з':'z','и':'i','й':'y',
    'к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u',
    'ф':'f','х':'h','ц':'ts','ч':'ch','ш':'sh','щ':'sht','ъ':'a','ь':'','ю':'yu','я':'ya'
}

EN_TERMS = {
    'играчки':'toys','играчка':'toy','бебе':'baby','бебешки':'baby','бебешка':'baby',
    'количка':'stroller','колички':'strollers','столче':'car seat','столчета':'car seats',
    'инструменти':'tools','инструмент':'tool','градина':'garden','градински':'garden',
    'резачка':'chainsaw','косачка':'lawn mower','винтоверт':'drill driver','бормашина':'drill',
    'перфоратор':'rotary hammer','пъзел':'puzzle','пъзели':'puzzles','дом':'home',
    'декор':'decor','електроуреди':'appliances','конзола':'console','слушалки':'headset',
    'машина':'machine','комплект':'set','детски':'kids','детска':'kids','дървени':'wooden',
    'образователни':'educational','акумулаторен':'cordless','акумулаторна':'cordless'
}

STOP = {'и','за','с','в','на','от','до','по','the','a','an','with','for'}


def norm(v):
    return ' '.join(str(v or '').split()).strip()


def translit(text):
    out=[]
    for ch in norm(text):
        lo=ch.lower()
        val=BG_MAP.get(lo,ch)
        if ch.isupper() and val:
            val=val[0].upper()+val[1:]
        out.append(val)
    return ''.join(out)


def shlyokavitsa(text):
    s=translit(text).lower()
    s=s.replace('sht','6t').replace('sh','6').replace('ch','4').replace('zh','j')
    return s


def english_hint(text):
    words=re.findall(r'[A-Za-zА-Яа-я0-9+.-]+', norm(text).lower())
    out=[]
    for w in words:
        out.append(EN_TERMS.get(w,w if re.search(r'[a-z0-9]',w) else ''))
    return ' '.join(x for x in out if x)


def short_phrase(text,limit=4):
    words=[w for w in re.findall(r'[A-Za-zА-Яа-я0-9+.-]+',norm(text)) if w.lower() not in STOP]
    return ' '.join(words[:limit])


def search_tags(item,supplier=''):
    name=norm(item.get('name'))
    brand=norm(item.get('brand')) or norm(supplier)
    sku=norm(item.get('sku'))
    ean=norm(item.get('ean'))
    category=norm(item.get('category'))
    source=norm(item.get('external_id'))
    candidates=[]
    if name:
        candidates += [translit(name), shlyokavitsa(name), short_phrase(name,3)]
    if brand and sku:
        candidates.append(f'{brand} {sku}')
    elif sku:
        candidates.append(sku)
    if category:
        eng=english_hint(category)
        if eng:
            candidates.append(' '.join(x for x in (eng,brand,sku) if x))
        candidates.append(' '.join(x for x in (short_phrase(category,2),brand) if x))
    if ean:
        candidates.append(ean)
    if source and source not in (sku,ean):
        candidates.append(source)
    out=[]; seen=set()
    for value in candidates:
        value=norm(value)[:200]
        key=value.lower()
        if value and key not in seen and key != name.lower():
            seen.add(key); out.append(value)
        if len(out)>=8:
            break
    return out


def seo(item):
    name=norm(item.get('name')) or norm(item.get('sku')) or 'Продукт'
    brand=norm(item.get('brand'))
    sku=norm(item.get('sku'))
    category=norm(item.get('category'))
    title=name
    if brand and brand.lower() not in title.lower():
        title=f'{title} {brand}'
    if sku and sku.lower() not in title.lower() and len(title)<56:
        title=f'{title} {sku}'
    title=title[:70].rstrip(' -|,')
    parts=[name]
    if category:
        parts.append(f'Категория: {category}')
    if brand:
        parts.append(f'Марка: {brand}')
    if sku:
        parts.append(f'Модел/SKU: {sku}')
    parts.append('Поръчайте онлайн от BGShopping.net с доставка в България и Европа.')
    desc='. '.join(p.rstrip('. ') for p in parts if p)[:320]
    return {'title':title,'description':desc}
