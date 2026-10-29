"""Produce an optional Meta Commerce Manager RSS catalog from the validated Shopify Google XML.

This does not activate a second Meta catalog source: native Shopify Facebook/Instagram
continues to be the primary channel. URL is available if a supplemental feed is needed.
"""
import xml.etree.ElementTree as ET
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "feeds"
G = "http://base.google.com/ns/1.0"
ET.register_namespace("g", G)
src = ROOT / "google-shopping.xml"
dst = ROOT / "meta-catalog.xml"
tree = ET.parse(src)
channel = tree.getroot().find("channel")
if channel is None:
    raise RuntimeError("Google Shopping feed has no RSS channel")
items = channel.findall("item")
if len(items) < 8000:
    raise RuntimeError(f"Refusing incomplete Meta feed: {len(items)} items")
for item in items:
    availability = item.find(f"{{{G}}}availability")
    if availability is not None:
        availability.text = "in stock" if availability.text == "in_stock" else "out of stock"
    if not item.findtext(f"{{{G}}}image_link", '').startswith('https://'):
        raise RuntimeError("Missing product image in source XML")
    if item.find(f"{{{G}}}price") is None:
        raise RuntimeError("Missing EUR price in source XML")
title = channel.find("title")
if title is not None:
    title.text = "BGShopping.net Meta Catalog"
tree.write(dst, encoding="utf-8", xml_declaration=True)
ET.parse(dst)
status_path = ROOT / 'feed-status.json'
if status_path.exists():
    status = json.loads(status_path.read_text(encoding='utf-8'))
    status.setdefault('image_fields', {})['meta-catalog.xml'] = {'offers': len(items), 'with_image': len(items), 'missing_image': 0}
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
print(f"Meta XML validated: {len(items)} items, {dst.stat().st_size} bytes", flush=True)
