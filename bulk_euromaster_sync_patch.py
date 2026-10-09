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

    def op(phase, digest):
        # Pass 2 deliberately uses new operation names for EVERY phase.
        # The first pass created 1,151 products and updated 5,413, so reusing
        # its completed index or mutations would classify against stale data.
        # Shopify bulk operations remain idempotent through this pass-specific
        # name and the catalog fingerprint/checkpoint.
        return 'BGSEuromasterReconcileV2' + phase + digest

    mod.variant_fields = variant_fields
    mod.op = op
    return mod
