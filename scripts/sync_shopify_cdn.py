"""Publish already validated XML/JSON exports to Shopify Files, never Render.

Run only after the Github feed snapshot is validated and committed. Requires a
merchant-authorized SHOPIFY_FEED_TOKEN in GitHub Actions Secrets with write_files
and write_online_store_navigation. Never log tokens or store them in repository.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

SHOP = "kynvva-gx.myshopify.com"
BASE = Path(__file__).resolve().parents[1] / "feeds"
API = f"https://{SHOP}/admin/api/2026-10/graphql.json"
GITHUB = "https://raw.githubusercontent.com/bgshoppingnet/saas_mining_manager/feed-cdn/feeds/"
CONFIG = Path(__file__).resolve().parent / "shopify-cdn-assets.json"
TOKEN = os.environ.get("SHOPIFY_FEED_TOKEN", "").strip()
FILES = ["pazaruvaj.xml", "shopmania.xml", "google-shopping.xml",
         "meta-catalog.xml", "ai-products.json", "feed-status.json"]
REDIRECTS = {
    "/pazaruvaj.xml": "pazaruvaj.xml",
    "/pazarovaj.xml": "pazaruvaj.xml",
    "/shopmania.xml": "shopmania.xml",
    "/google-shopping.xml": "google-shopping.xml",
    "/meta-catalog.xml": "meta-catalog.xml",
    "/ai-products.json": "ai-products.json",
    "/feed-status.json": "feed-status.json",
}
MIMES = {".xml": "application/xml", ".json": "application/json"}

def gql(query, variables):
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = Request(API, data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": TOKEN,
        "User-Agent": "BGShopping-Own-Shopify-Feeds/1.0"
    })
    with urlopen(req, timeout=95) as response:
        d = json.load(response)
    if d.get("errors"):
        raise RuntimeError("Shopify API returned GraphQL errors: " + json.dumps(d["errors"])[:800])
    return d["data"]

def verified_source():
    report = json.loads((BASE / "feed-status.json").read_text(encoding="utf-8"))
    stamp = datetime.fromisoformat(report["source_generated_at"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if age < 0 or age > 3600:
        raise RuntimeError("Refusing outdated or future-dated source feed")
    if min(int(report["pazaruvaj_products"]), int(report["google_products"]),
           int(report["shopmania_products"])) < 8000:
        raise RuntimeError("Incomplete product feed")
    for name in FILES:
        file = BASE / name
        if not file.exists() or not 100 <= file.stat().st_size < 19_000_000:
            raise RuntimeError(f"Missing/oversized Shopify Files input: {name}")
    return report

def load_config():
    d = json.loads(CONFIG.read_text(encoding="utf-8"))
    files = d["files"]
    redirects = d["redirects"]
    if sorted(files) != sorted(FILES) or sorted(redirects) != sorted(REDIRECTS):
        raise RuntimeError("Shopify file and redirect ID mapping incomplete")
    for x in [*files.values(), *redirects.values()]:
        if not isinstance(x,str) or not x.startswith("gid://shopify/"):
            raise RuntimeError("Bad Shopify resource ID")
    return files, redirects

def get_files(ids):
    query = """query ReadFiles($ids:[ID!]!){
      nodes(ids:$ids){... on GenericFile{
        id fileStatus mimeType url originalFileSize fileErrors{code message}
      }}
    }"""
    result = gql(query, {"ids": list(ids)})
    return {f["id"]:f for f in result["nodes"] if f is not None}

def get_redirect(id):
    query = """query Redirect($id:ID!){urlRedirect(id:$id){id path target}}"""
    return gql(query, {"id": id})["urlRedirect"]

def inspect_shopify_file(file):
    if file.get("fileStatus") != "READY" or file.get("fileErrors"):
        return False
    url = file.get("url") or ""
    if urlsplit(url).hostname not in ("cdn.shopify.com", "cdn.shopifycdn.net"):
        raise RuntimeError("Unexpected Shopify file host")
    return True

def main():
    if not TOKEN:
        raise RuntimeError("SHOPIFY_FEED_TOKEN secret missing")
    report = verified_source()
    ids, redirect_ids = load_config()
    prior = get_files(ids.values())
    if len(prior) != len(FILES):
        raise RuntimeError("Some Shopify CDN files are missing")
    for name,id in ids.items():
        if not inspect_shopify_file(prior[id]):
            raise RuntimeError("Shopify CDN file not READY: "+name)

    # Update file contents in place; no duplicate products or files created.
    mutation = """mutation ReplaceFeed($files:[FileUpdateInput!]!){
      fileUpdate(files:$files){
        files{id fileStatus ... on GenericFile{url mimeType originalFileSize}}
        userErrors{field message code}
      }
    }"""
    for name in FILES:
        url = GITHUB + name
        result = gql(mutation, {"files":[{"id":ids[name], "originalSource":url}]})["fileUpdate"]
        if result["userErrors"]:
            raise RuntimeError("Shopify file update failed for "+name+": "+str(result["userErrors"]))
        print("Submitted Shopify file update:",name,flush=True)

    # Poll file availability and sizes. Only switch public links after ALL files
    # have been successfully validated, never after a partial upload.
    expected = {x:(BASE / x).stat().st_size for x in FILES}
    final = None
    for attempt in range(45):
        files = get_files(ids.values())
        complete = all(
            f_id in files and inspect_shopify_file(files[f_id]) and
            files[f_id].get("originalFileSize") == expected[name] and
            files[f_id].get("mimeType") == MIMES[Path(name).suffix]
            for name, f_id in ids.items()
        )
        if complete:
            final = files
            break
        time.sleep(5)
    if final is None:
        raise RuntimeError("Shopify file processing incomplete; public redirects remain unchanged")

    redirect_mutation = """mutation UpdateFeedRedirect($id:ID!,$input:UrlRedirectInput!){
      urlRedirectUpdate(id:$id,urlRedirect:$input){
        urlRedirect{id path target} userErrors{field message}
      }
    }"""
    for path, name in REDIRECTS.items():
        id = redirect_ids[path]
        old = get_redirect(id)
        if old is None or old["path"] != path:
            raise RuntimeError("Redirect identity mismatch: "+path)
        dest = final[ids[name]]["url"]
        if old["target"] == dest:
            continue
        result = gql(redirect_mutation, {"id":id, "input":{"path":path,"target":dest}})
        obj = result["urlRedirectUpdate"]
        if obj["userErrors"] or not obj.get("urlRedirect") or obj["urlRedirect"]["target"] != dest:
            raise RuntimeError("Redirect update failed: "+path+" "+str(obj["userErrors"]))
        print("Now hosted by Shopify CDN:",path,flush=True)
    print(json.dumps({
       "result":"ok", "shop":SHOP, "products":report["google_products"],
       "source_timestamp":report["source_generated_at"], "files":len(FILES),
       "redirects":len(REDIRECTS)
    }),flush=True)

if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR: Shopify CDN feed sync failed safely:",str(exc)[:500],file=sys.stderr,flush=True)
        raise
