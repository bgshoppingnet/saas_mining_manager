from urllib.parse import urlsplit, urlunsplit, quote


def _safe_url(value):
    u = str(value or '').strip()
    if not u.startswith(('http://','https://')):
        return ''
    try:
        p = urlsplit(u)
        path = quote(p.path, safe='/%:@-._~!$&\'()*+,;=')
        query = quote(p.query, safe='=&%:@/?-._~!$\'()*+,;')
        return urlunsplit((p.scheme, p.netloc, path, query, p.fragment))
    except Exception:
        return ''


def install(mod):
    def images(item):
        out=[]
        for raw in [item.get('image')] + list(item.get('images') or []):
            u=_safe_url(raw)
            if u and u not in out:
                out.append(u)
        return out[:10]
    mod.images = images
    return mod
