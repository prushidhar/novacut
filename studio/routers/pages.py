"""Public and page-serving routes."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import config, db
from ._helpers import LEGAL, WEB, _LANDING, _LEGAL_HTML, _admin, _page

router = APIRouter()


@router.get("/")
def landing():
    f = WEB / "landing.html"
    m = f.stat().st_mtime
    if _LANDING.get("m") != m:
        html = f.read_text(encoding="utf-8").replace("{{SITE}}", config.PUBLIC_URL)
        _LANDING.update(m=m, html=html, etag='"' + hashlib.md5(html.encode()).hexdigest() + '"')
    return HTMLResponse(
        _LANDING["html"],
        headers={"Cache-Control": "public, max-age=300, stale-while-revalidate=86400", "ETag": _LANDING["etag"]},
    )


@router.get("/robots.txt")
def robots():
    return Response(
        "User-agent: *\nAllow: /\nDisallow: /app\nDisallow: /api/\nDisallow: /auth/\nDisallow: /reset\nDisallow: /media/\n"
        f"\nSitemap: {config.PUBLIC_URL}/sitemap.xml\n",
        media_type="text/plain",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/sitemap.xml")
def sitemap():
    day = time.strftime("%Y-%m-%d")
    pages = [
        ("/", "1.0", "weekly"),
        ("/signup", "0.8", "monthly"),
        ("/login", "0.4", "monthly"),
        ("/terms", "0.3", "yearly"),
        ("/privacy", "0.3", "yearly"),
        ("/acceptable-use", "0.2", "yearly"),
    ]
    body = "".join(
        f"<url><loc>{config.PUBLIC_URL}{u}</loc><lastmod>{day}</lastmod><changefreq>{c}</changefreq><priority>{p}</priority></url>"
        for u, p, c in pages
    )
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + body
        + "</urlset>",
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/login")
@router.get("/signup")
def auth_page():
    return _page("auth.html", shared=True)


@router.get("/reset")
def reset_page():
    return _page("reset.html", shared=True)


@router.get("/privacy")
@router.get("/terms")
@router.get("/acceptable-use")
def legal_page(request: Request):
    """Public, so Google's app review (and anyone) can read them. Business name, law and contact come from settings."""
    import html as _h

    page = request.url.path.strip("/")
    title, desc = LEGAL[page]
    contact = config.CONTACT_EMAIL
    who = (
        f'<a href="mailto:{_h.escape(contact)}">{_h.escape(contact)}</a>'
        if contact
        else "the contact details on our website"
    )
    name = _h.escape(config.LEGAL_NAME or "NovaCut")
    law = (
        f"the laws of {_h.escape(config.LEGAL_COUNTRY)}"
        if config.LEGAL_COUNTRY
        else "the laws of the country where NovaCut's operator is established"
    )
    out = _LEGAL_HTML or (WEB / "legal.html").read_text(encoding="utf-8")
    for k, v in {
        "{{CONTACT}}": who,
        "{{PAGE}}": page,
        "{{PAGE_PATH}}": page,
        "{{SITE}}": _h.escape(config.PUBLIC_URL),
        "{{NAME}}": name,
        "{{LAW}}": law,
        "{{TITLE}}": title,
        "{{DESC}}": desc,
    }.items():
        out = out.replace(k, v)
    return HTMLResponse(out, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/app")
@router.get("/app/{rest:path}")
def app_page(rest: str = ""):
    """Every app address (/app/videos, /app/creators/<id>/setup ...) is the same page; it shows the right view."""
    return _page("index.html")


@router.get("/admin")
@router.get("/admin/{rest:path}")
def admin_page(request: Request, rest: str = ""):
    _admin(request)
    return _page("admin.html")


@router.get("/healthz")
def healthz():
    with db.engine.connect() as c:
        c.execute(sa.text("select 1"))
    return {"ok": True}


@router.get("/brand/{name}")
def brand(name: str):
    if not re.fullmatch(r"[\w\-]+\.(svg|png|ico|webp|jpg|mp4)", name) or not (WEB / "brand" / name).is_file():
        raise HTTPException(404)
    return FileResponse(
        WEB / "brand" / name, headers={"Cache-Control": "public, max-age=604800, stale-while-revalidate=86400"}
    )


@router.get("/assets/{name}")
def asset(name: str):
    if not re.fullmatch(r"[\w\-]+\.js", name) or not (WEB / name).is_file():
        raise HTTPException(404)
    return FileResponse(
        WEB / name,
        media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=86400, stale-while-revalidate=604800"},
    )


@router.get("/favicon.ico")
def favicon():
    return RedirectResponse("/brand/favicon.svg", 308)
