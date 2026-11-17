"""PeakCut web server. Run with:  python -m studio.server

Public pages:  /  /login  /signup          The app:  /app  (signed-in users only)
Everything under /api, /media and /oauth needs a session, except /api/auth/* and /api/public."""
from __future__ import annotations

import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
import uvicorn

from . import auth, config, ctx, db, jobs, live, posting, render, transcribe
from . import tools as _tools
from .routers import (
    admin_routes,
    auth_routes,
    creators_routes,
    pages,
    settings_routes,
    videos,
    ws_routes,
)
from .routers._helpers import (
    PAGES,
    _from_here,
    _ip,
    _job_slots,
    _need_verified,
    _own_address,
    _quota,
    _sync_unchanged,
)


def _quiet(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - said again, in plain words, when a video needs it
        print(f"  {e}", flush=True)


app = FastAPI(title="PeakCut", docs_url=None, redoc_url=None, openapi_url=None)

OPEN = (
    "/api/auth/",
    "/pub/",
    "/api/public",
    "/auth/google/",
    "/auth/verify",
    "/brand/",
    "/assets/",
    "/healthz",
    "/favicon.ico",
    "/robots.txt",
    "/sitemap.xml",
    "/ws",
)
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; media-src 'self' blob:; connect-src 'self' ws: wss:; "
    "frame-src https://player.twitch.tv https://www.youtube-nocookie.com https://player.kick.com; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://accounts.google.com"
)


@app.on_event("startup")
async def _more_threads():
    import anyio.to_thread

    anyio.to_thread.current_default_thread_limiter().total_tokens = int(config.env("PEAKCUT_REQUEST_THREADS", "64"))


@app.on_event("startup")
def _startup():
    db.init()
    for f in (config.TMP_DIR / "uploads").glob("*"):  # uploads cut short by a restart
        try:
            f.unlink()
        except OSError:
            pass
    render.install_fonts()
    if _tools.missing():
        print("\n  " + _tools.HELP + "\n", flush=True)
    if not transcribe.REMOTE and config.env("PEAKCUT_FETCH_MODEL", "1") != "0":
        from . import fetch_model

        threading.Thread(target=lambda: _quiet(fetch_model.ensure), daemon=True).start()
    jobs.start()
    live.start()
    posting.start()
    from . import updates

    updates.start()  # looks for a newer PeakCut every minute, in the background


# ================================================================ the gate: who is asking, and are they allowed
class Gate:
    """Runs before every request: finds the signed-in user, blocks what needs sign-in, blocks cross-site form
    posts, caps upload size and adds security headers."""

    def __init__(self, app):
        self.inner = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        req = Request(scope)
        path, method = scope["path"], scope["method"]

        async def deny(status, msg, redirect=None):
            no_keep = {"Cache-Control": "private, no-store"}  # never kept by a browser or CDN
            resp = (
                RedirectResponse(redirect, 303, headers=no_keep)
                if redirect
                else JSONResponse({"detail": msg}, status, headers=no_keep)
            )
            await resp(scope, receive, send)

        if method not in ("GET", "HEAD", "OPTIONS"):
            if not _from_here(req):
                return await deny(403, "Cross-site requests are not allowed.")
            piece = method == "PUT" and path.startswith("/api/upload/")
            cap = (
                (
                    (min(config.MAX_UPLOAD_MB, config.UPLOAD_PART_MB) if piece else config.MAX_UPLOAD_MB)
                    + 8
                )
                * 1024
                * 1024
                if path == "/api/upload" or piece
                else 256 * 1024
            )
            try:
                if int(req.headers.get("content-length") or 0) > cap:
                    return await deny(
                        413,
                        f"That file is too big. The limit is {config.MAX_UPLOAD_MB} MB."
                        if path == "/api/upload"
                        else "That request is too large.",
                    )
            except ValueError:
                return await deny(400, "Bad request.")
            seen, inner_receive = 0, receive

            async def receive():
                nonlocal seen
                m = await inner_receive()
                if m["type"] == "http.request":
                    seen += len(m.get("body", b""))
                    if seen > cap:
                        raise HTTPException(413, "That request is too large.")
                return m

        is_open = path in PAGES or any(path.startswith(p) for p in OPEN)
        admin_area = path == "/admin" or path.startswith(("/admin/", "/api/admin/"))
        if config.ACCOUNTS:
            cookie = req.cookies.get(auth.COOKIE, "")
            user = (
                auth._sess.get(auth._h(cookie)) if cookie and not admin_area else None
            )  # remembered: no database, no thread hop
            user = (
                user[1]
                if user and time.time() - user[0] < auth.SESS_FOR
                else (await run_in_threadpool(auth.user_for, cookie, admin_area) if cookie else None)
            )
        else:
            if not _own_address(req.headers.get("host", "")):
                return await deny(403, "Open PeakCut by its own address.")
            if path == "/" or path == "/reset" or path.startswith("/auth/"):
                return await deny(303, "", redirect="/app")
            if path.startswith("/api/auth/") and path != "/api/auth/me":
                return await deny(404, "Not Found")
            user = auth._local or await run_in_threadpool(auth.local_user)
        if admin_area:
            if not user or not user.is_admin:
                return await deny(404, "Not Found")
            if method not in ("GET", "HEAD"):
                if not _from_here(req, must_say=True):
                    return await deny(403, "Cross-site requests are not allowed.")
        if not user and not is_open:
            if path == "/app" or path.startswith("/app/"):
                return await deny(401, "", redirect="/login")
            return await deny(401, "Please sign in.")
        if user and path in ("/login", "/signup"):
            return await deny(303, "", redirect="/app")
        scope.setdefault("state", {})["user"] = user
        if user and path in ("/api/upload", "/api/upload/start") and method == "POST":
            def allowed():
                with ctx.as_user(user.id):
                    _need_verified()
                    _job_slots()
                    _quota()

            try:
                await run_in_threadpool(allowed)
            except HTTPException as e:
                return await deny(e.status_code, e.detail)
        if user and path.startswith("/api/") and not auth.seen_recently(user.id):
            await run_in_threadpool(auth.touch, user.id)

        unchanged = False

        async def send_with_headers(message):
            nonlocal unchanged
            if unchanged:
                if message["type"] == "http.response.body" and not message.get("more_body"):
                    await send({"type": "http.response.body", "body": b""})
                return
            if message["type"] == "http.response.start":
                h = message.setdefault("headers", [])
                if method == "GET" and message.get("status") == 200:
                    tag = next((v.decode("latin-1") for k, v in h if k.lower() == b"etag"), "")
                    have = req.headers.get("if-none-match", "")
                    if tag and have and tag.removeprefix("W/") in [x.strip().removeprefix("W/") for x in have.split(",")]:
                        unchanged = True
                        message["status"] = 304
                        h[:] = [
                            (k, v)
                            for k, v in h
                            if k.lower() not in (b"content-length", b"content-type", b"content-encoding")
                        ]
                h += [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"content-security-policy", CSP.encode()),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                ]
                if config.HTTPS:
                    h.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                if path.startswith("/api/"):
                    h.append((b"cache-control", b"no-store"))
                elif message.get("status", 200) >= 300 and not any(k.lower() == b"cache-control" for k, _ in h):
                    h.append((b"cache-control", b"private, no-store"))
                if path.startswith(("/app", "/admin", "/api/", "/auth/", "/reset", "/media/")):
                    h.append((b"x-robots-tag", b"noindex, nofollow"))
            await send(message)

        token = ctx._uid.set(user.id if user else None)
        try:
            if user and path == "/api/sync" and method == "GET":
                quick = _sync_unchanged(req.query_params.get("v", ""))
                if quick is not None:
                    return await JSONResponse(quick)(scope, receive, send_with_headers)
            await self.inner(scope, receive, send_with_headers)
        finally:
            ctx._uid.reset(token)


app.add_middleware(Gate)


class Compress:
    """gzip for text responses. Videos and images are already compressed and need byte ranges, so they pass through."""

    def __init__(self, app):
        from starlette.middleware.gzip import GZipMiddleware

        self.plain, self.gzip = app, GZipMiddleware(app, minimum_size=800, compresslevel=6)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        media = path.startswith(("/media/", "/api/upload")) or path.endswith((".mp4", ".jpg", ".png", ".webp", ".ico"))
        await (self.plain if scope["type"] != "http" or media else self.gzip)(scope, receive, send)


app.add_middleware(Compress)


# Mount routers
app.include_router(pages.router)
app.include_router(auth_routes.router)
app.include_router(videos.router)
app.include_router(creators_routes.router)
app.include_router(settings_routes.router)
app.include_router(admin_routes.router)
app.include_router(ws_routes.router)


def main():
    print(f"\n  PeakCut is running:  {config.PUBLIC_URL}\n")
    if not config.ACCOUNTS and (config.HTTPS or config.HOST not in ("127.0.0.1", "localhost", "::1")):
        print(
            "  Sign-in is off, so anyone who can reach this address can use PeakCut and its AI key.\n"
            "  On a server other people can reach, set PEAKCUT_ACCOUNTS=1 in .env.\n",
            flush=True,
        )
    if config.env("PEAKCUT_AUTO_RELOAD") == "1":
        uvicorn.run(
            "studio.server:app",
            host=config.HOST,
            port=config.PORT,
            log_level="warning",
            backlog=4096,
            reload=True,
            reload_dirs=[str(config.ROOT / "studio")],
            reload_includes=["*.py"],
            proxy_headers=bool(config.env("PEAKCUT_TRUST_PROXY")),
            forwarded_allow_ips="*",
        )
        return
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level="warning",
        backlog=4096,
        proxy_headers=bool(config.env("PEAKCUT_TRUST_PROXY")),
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
