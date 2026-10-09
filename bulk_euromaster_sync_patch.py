def install(mod):
    old_variant_fields = mod.variant_fields

    def variant_fields(item, vid=None, final=False):
        value = old_variant_fields(item, vid, final)
        # In the current Admin API SKU belongs to InventoryItemInput during
        # productVariantsBulkUpdate, not at ProductVariantsBulkInput top level.
        value.pop('sku', None)
        sku = mod.norm(item.get('sku'))
        if sku:
            value.setdefault('inventoryItem', {})['sku'] = sku
        # Promotions stay disabled until supplier/product promo rules are ready.
        value['compareAtPrice'] = None
        return value

    def match(item, idx):
        source, sku, ean = idx

        # Strongest key first. After the first Euromaster migration every
        # matched/created canonical product carries supplier_sync.source_key.
        # If that exact source key identifies one Euromaster product, use it
        # immediately and do not let legacy duplicate SKU/EAN rows turn the
        # otherwise exact match into a conflict.
        sid = mod.low(mod.source_id(item))
        if sid and source.get(sid):
            tagged = [x for x in source[sid] if x[2] == 'euromaster']
            exact = tagged if tagged else source[sid]
            p, v, conf = mod.choose(exact)
            if p and not conf:
                return p, v, False

        # Fallback only when no unique source-key canonical record exists.
        checks = []
        s = mod.low(item.get('sku'))
        if s and sku.get(s):
            checks.extend(sku[s])
        e = mod.low(item.get('ean'))
        if e and ean.get(e):
            checks.extend(ean[e])
        dedup = {(p['id'], (v or {}).get('id')): (p, v, supp) for p, v, supp in checks}
        return mod.choose(list(dedup.values()))

    def op(phase, digest):
        # New operation family forces a fresh Shopify index and fresh bulk jobs
        # after the source-key reconciliation logic changed.
        return 'BGSEuromasterReconcileV3' + phase + digest

    mod.variant_fields = variant_fields
    mod.match = match
    mod.op = op
    return mod
