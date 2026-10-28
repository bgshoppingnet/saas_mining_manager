"""Read BGShopping's public Shopify storefront product catalog, no admin token or Render.

Output a validated JSON-LD source for the existing Pazaruvaj, ShopMania and Google
XML builders. Never writes products back to Shopify or trusts stale supplier data.
"""
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

BASE = "https://bgshopping.net"
OUT = Path(__file__).resolve().parents[1] / "feeds" / "ai-products.json"
PAGE_SIZE = 250
MAX_PAGES = 150
MIN_PRODUCTS = 10000
MIN_AVAILABLE_VARIANTS = 8000
HIDDEN_SUPPLIER_NAMES = {
    "polycomp", "euromaster", "euromasterbg", "bg electronics",
    "bgelectronics", "nuvita supplier", "supplier", "wholesaler",
}

def fetch(page):
    url = f"{BASE}/products.json?limit={PAGE_SIZE}&page={page}"
    for attempt in range(5):
        try:
            req = Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "BGShopping-FeedSync/2.0 (+https://bgshopping.net)",
            })
            with urlopen(req, timeout=75) as response:
                if response.status != 200:
                    raise RuntimeError(f"Unexpected HTTP {response.status}")
                raw = response.read()
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("products"), list):
                raise RuntimeError("Shopify endpoint returned no products array")
            return data["products"]
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as err:
            if attempt == 4:
                raise RuntimeError(f"Shopify page {page} failed: {type(err).__name__}") from err
            time.sleep(min(2 ** attempt, 15))
    raise AssertionError("unreachable")

def clean(value, limit=1700):
    value = html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))
    return " ".join(value.split())[:limit]

def image_urls(product, variant):
    sources = [variant.get("featured_image")] + list(product.get("images") or [])
    sources.append(product.get("image"))
    found = []
    for source in sources:
        src = source.get("src") if isinstance(source, dict) else source
        if not isinstance(src, str):
            continue
        src = src.strip()
        parsed = urlsplit(src)
        if parsed.scheme == "https" and parsed.netloc and src not in found:
            if not re.search(r"placeholder|no[-_]?image|default[-_]?image", src, re.I):
                found.append(src)
        if len(found) >= 3:
            break
    return found

def to_price(value):
    try:
        number = Decimal(str(value).replace(",", "."))
        if not number.is_finite() or not Decimal("0.01") <= number <= Decimal("1000000"):
            return None
        return format(number.quantize(Decimal("0.01")), ".2f")
    except (InvalidOperation, ValueError, TypeError):
        return None

def make_variant(product, variant):
    handle = str(product.get("handle") or "").strip()
    identifier = str(variant.get("id") or "").strip()
    if not handle or not identifier.isdigit() or variant.get("available") is not True:
        return None
    price = to_price(variant.get("price"))
    images = image_urls(product, variant)
    if price is None or not images:
        return None
    title = clean(product.get("title"), 150)
    extra = clean(variant.get("title"), 65)
    if extra and extra.casefold() not in ("default title", "default"):
        title = (title + " - " + extra)[:220]
    if not title:
        return None
    vendor = clean(product.get("vendor"), 120)
    if vendor.casefold() in HIDDEN_SUPPLIER_NAMES:
        vendor = ""
    barcode = str(variant.get("barcode") or "").strip()
    if not (barcode.isdigit() and len(barcode) in (8, 12, 13, 14)):
        barcode = ""
    sku = clean(variant.get("sku"), 100)
    description = clean(product.get("body_html") or product.get("description"), 1800)
    url = f"{BASE}/products/{quote(handle, safe='-')}?variant={identifier}"
    record = {
        "@type": "Product",
        "id": "BG-" + identifier,
        "name": title,
        "url": url,
        "image": images,
        "brand": vendor,
        "category": clean(product.get("product_type"), 150),
        "sku": sku,
        "gtin": barcode,
        "description": description or title,
        "offers": {
            "@type": "Offer",
            "price": price,
            "priceCurrency": "EUR",
            "availability": "https://schema.org/InStock",
            "url": url,
        },
    }
    return record

def run():
    seen_products, seen_variants, output = set(), set(), []
    total_products = 0
    missing_images = 0
    end_seen = False
    for page in range(1, MAX_PAGES + 1):
        batch = fetch(page)
        if not batch:
            end_seen = True
            print(f"Reached catalog end at page={page}", flush=True)
            break
        for product in batch:
            if not isinstance(product, dict):
                continue
            product_id = str(product.get("id") or "")
            if not product_id or product_id in seen_products:
                continue
            seen_products.add(product_id)
            total_products += 1
            for variant in product.get("variants") or []:
                if not isinstance(variant, dict):
                    continue
                identifier = str(variant.get("id") or "")
                if not identifier or identifier in seen_variants:
                    continue
                seen_variants.add(identifier)
                row = make_variant(product, variant)
                if row:
                    output.append(row)
                elif variant.get("available") is True and not image_urls(product, variant):
                    missing_images += 1
        if page % 10 == 0:
            print(f"Shopify pages={page} unique_products={total_products} available_variants={len(output)}", flush=True)
        if len(batch) < PAGE_SIZE:
            end_seen = True
            break
        time.sleep(0.20)
    if not end_seen:
        raise RuntimeError("Shopify catalog exceeded maximum pagination; refusing partial publication")
    if total_products < MIN_PRODUCTS or len(output) < MIN_AVAILABLE_VARIANTS:
        raise RuntimeError(
            f"Catalog incomplete: products={total_products} variants={len(output)} "
            f"(minimum {MIN_PRODUCTS} products, {MIN_AVAILABLE_VARIANTS} variants)"
        )
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = {
        "@context": "https://schema.org",
        "generated_at": generated_at,
        "currency": "EUR",
        "source": "BGShopping.net direct Shopify storefront catalog",
        "products": output,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    json.loads(temporary.read_text(encoding="utf-8"))
    temporary.replace(OUT)
    print(json.dumps({
        "status": "success", "generated_at": generated_at, "products": total_products,
        "available_variants_with_images": len(output), "missing_image_variants": missing_images
    }, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise
