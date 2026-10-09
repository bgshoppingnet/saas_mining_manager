def install(mod):
    old_variant_fields = mod.variant_fields

    def variant_fields(item, vid=None, final=False):
        value = old_variant_fields(item, vid, final)
        value.pop('sku', None)
        sku = mod.norm(item.get('sku'))
        if sku:
            value.setdefault('inventoryItem', {})['sku'] = sku
        value['compareAtPrice'] = None
        return value

    def _dedup(rows):
        return {(p['id'], (v or {}).get('id')): (p, v, supp) for p, v, supp in rows}

    def _prefer_supplier(rows):
        tagged = [x for x in rows if x[2] == 'euromaster']
        return tagged if tagged else rows

    def match(item, idx):
        source, sku, ean = idx

        sid = mod.low(mod.source_id(item))
        if sid and source.get(sid):
            p, v, conf = mod.choose(_prefer_supplier(source[sid]))
            if p and not conf:
                return p, v, False

        s = mod.low(item.get('sku'))
        e = mod.low(item.get('ean'))
        sku_rows = list(_dedup(sku.get(s, [])).values()) if s and sku.get(s) else []
        ean_rows = list(_dedup(ean.get(e, [])).values()) if e and ean.get(e) else []

        # When both identifiers exist, use only candidates that match both.
        # This safely resolves legacy duplicates that share just one identifier.
        if sku_rows and ean_rows:
            sku_map = _dedup(sku_rows)
            ean_map = _dedup(ean_rows)
            common_keys = set(sku_map).intersection(ean_map)
            common = [sku_map[k] for k in common_keys]
            if common:
                p, v, conf = mod.choose(_prefer_supplier(common))
                if p and not conf:
                    return p, v, False

            # Product-level intersection can still be unique when variant IDs
            # differ because one legacy record has incomplete variant metadata.
            sku_products = {p['id'] for p, v, supp in sku_rows}
            ean_products = {p['id'] for p, v, supp in ean_rows}
            common_products = sku_products.intersection(ean_products)
            if len(common_products) == 1:
                pid = next(iter(common_products))
                rows = [x for x in (sku_rows + ean_rows) if x[0]['id'] == pid]
                p, v, conf = mod.choose(_prefer_supplier(rows))
                if p and not conf:
                    return p, v, False

        # Final fallback is intentionally conservative.
        fallback = sku_rows or ean_rows
        return mod.choose(_prefer_supplier(fallback))

    def op(phase, digest):
        return 'BGSEuromasterReconcileV4' + phase + digest

    mod.variant_fields = variant_fields
    mod.match = match
    mod.op = op
    return mod
