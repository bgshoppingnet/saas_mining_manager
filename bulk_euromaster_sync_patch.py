def install(mod):
    old_variant_fields = mod.variant_fields
    old_op = mod.op

    def variant_fields(item, vid=None, final=False):
        value = old_variant_fields(item, vid, final)
        # In the current Admin API SKU belongs to InventoryItemInput during
        # productVariantsBulkUpdate, not at ProductVariantsBulkInput top level.
        value.pop('sku', None)
        sku = mod.norm(item.get('sku'))
        if sku:
            value.setdefault('inventoryItem', {})['sku'] = sku
        # Promotions are intentionally disabled for now. Future supplier/product
        # promotion rules will explicitly set compareAtPrice again.
        value['compareAtPrice'] = None
        return value

    def op(phase, digest):
        # Previous Variant operation completed with row-level schema errors.
        # A new operation name guarantees Shopify receives the corrected JSONL.
        if phase == 'Variant':
            return 'BGSEuromasterVariantFix3NoPromo' + digest
        return old_op(phase, digest)

    mod.variant_fields = variant_fields
    mod.op = op
    return mod
