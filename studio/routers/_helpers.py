"""Shared helpers used by multiple routers."""
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from fastapi import HTTPException, Request

from .. import auth, config, creators, ctx, jobs, live, load, media, profiles, safe, usage

_CDN_RANGES = None


def _from_cdn(addr: str) -> bool:
    global _CDN_RANGES
    import ipaddress
    if _CDN_RANGES is None:
        ranges = config.env("PEAKCUT_CDN_RANGES") or (
            "173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 "
            "190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 "
            "104.24.0.0/14 172.64.0.0/13 131.0.72.0/22 2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 "
            "2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32")
        _CDN_RANGES = [ipaddress.ip_network(r) for r in ranges.replace(",", " ").split()]
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in n for n in _CDN_RANGES)


def _ip(request: Request) -> str:
    """The visitor's address, used for rate limits. The connection's own address, or the one the proxy in front
    reports (PEAKCUT_TRUST_PROXY). If that connection comes from the CDN, the CDN's header names the real visitor
    (PEAKCUT_CLIENT_IP_HEADER). The header is believed only when the CDN really sent the request, so nobody can
    pick their own address by sending it themselves."""
    fwd = request.headers.get("x-forwarded-for", "")
    peer = (fwd.split(",")[0].strip() if fwd and config.env("PEAKCUT_TRUST_PROXY") else "") or (request.client.host if request.client else "")
    named = config.env("PEAKCUT_CLIENT_IP_HEADER")
    if named and request.headers.get(named) and _from_cdn(peer):
        return request.headers[named].split(",")[0].strip()[:64]
    return peer


# ---------------------------------------------------------------- user / permission helpers
def _me(request: Request):
    return request.state.user


def _admin(request: Request):
    """The signed-in admin, read from the database for this very request. Anyone else gets "not found"."""
    u = getattr(request.state, "user", None)
    if not u or not u.is_admin or u.disabled:
        raise HTTPException(404)
    return u


def _need_verified():
    if not auth.is_verified(ctx.uid()):
        raise HTTPException(403, "Confirm your email first. We sent you a link when you signed up; you can send a new one from the banner at the top.")


# ---------------------------------------------------------------- rate / capacity helpers
def _job_slots():
    from .. import tools
    if tools.missing():
        raise HTTPException(503, tools.HELP)
    if jobs.busy_count() >= config.MAX_ACTIVE_JOBS_PER_USER:
        raise HTTPException(429, f"You already have {config.MAX_ACTIVE_JOBS_PER_USER} videos in progress. Wait for one to finish.")


_create_lock = threading.Lock()      # "do I have a free slot?" and "take it" happen together, so bursts cannot slip past the limit
_lookups = threading.BoundedSemaphore(8)   # link and channel previews running at once, whole server
_disk: dict = {}


def _quota():
    """Each account has a fixed amount of disk space. Measured at most once a minute."""
    if load.disk_full():
        from .. import storage
        raise HTTPException(507, storage.full_message())
    uid = ctx.uid()
    got = _disk.get(uid)
    if not got or time.time() - got[0] > 60:
        total = 0
        for root, _dirs, files in os.walk(config.user_dir(uid)):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        got = _disk[uid] = (time.time(), total)
    if got[1] > config.MAX_USER_GB * 1e9:
        raise HTTPException(507, "Your storage is full. Delete some older videos to make room.")


def _throttle(key, limit, window, msg="Too many requests at once. Wait a moment."):
    try:
        auth.throttle(key, limit, window)
    except auth.AuthError:
        raise HTTPException(429, msg)


def _lookup(fn, *a):
    """Run a preview lookup, unless the server is already doing as many as it should."""
    if not _lookups.acquire(blocking=False):
        raise HTTPException(429, "Lots of people are looking things up right now. Try again in a few seconds.")
    try:
        return fn(*a)
    finally:
        _lookups.release()


# ---------------------------------------------------------------- upload helpers
_UP_ID = re.compile(r"[0-9a-f]{24}")
_up_lock = threading.Lock()


def _up_root() -> Path:
    d = config.TMP_DIR / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _up_sweep():
    """Forget uploads that were started and never finished (after six hours)."""
    for d in _up_root().glob("up-*"):
        try:
            if d.is_dir() and time.time() - d.stat().st_mtime > 6 * 3600:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _up_get(upid: str) -> dict:
    """The upload, only if it belongs to whoever is asking."""
    if not _UP_ID.fullmatch(upid or ""):
        raise HTTPException(404, "No such upload")
    try:
        meta = json.loads((_up_root() / f"up-{upid}" / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, "This upload has expired. Start it again.")
    if meta.get("user") != ctx.uid():
        raise HTTPException(404, "No such upload")
    return meta


# ---------------------------------------------------------------- data helpers
def _pairs(v, limit=300) -> list:
    """[[video id, clip id], ...] from the page, checked."""
    out = []
    for x in (v if isinstance(v, list) else [])[:limit]:
        if isinstance(x, (list, tuple)) and len(x) == 2 and all(isinstance(y, str) and re.fullmatch(r"[\w\-]{1,60}", y) for y in x):
            out.append((x[0], x[1]))
    return out


def _creator_rows():
    out = []
    for c in creators.all_creators():
        if profiles.needs_refresh(c):
            profiles.refresh(c["id"])
        s = live.active_for(c["id"])
        out.append({**c, "live_job": s.job["id"] if s else None, "log": creators.read_log(c["id"], 30)})
    return out


def _sync_token() -> str:
    return f"{BOOT}.{ctx.version()}.{jobs.queue_epoch() if jobs.busy_count() else 0}"


def _sync_unchanged(v: str):
    """{\"v\": token} if the page is up to date and this user's creators are already in memory, else None."""
    mine = creators._mem.get(ctx.uid())
    if mine is None or not v or v != _sync_token():
        return None
    for c in list(mine.values()):                      # still keep live status fresh while the page is open
        if profiles.needs_refresh(c):
            profiles.refresh(c["id"])
    return {"v": v}


def _job_or_404(jid: str):
    job = jobs.get(jid) if re.fullmatch(r"[\w\-]{8,60}", jid) else None
    if not job:
        raise HTTPException(404, "No such video")
    return job


# ---------------------------------------------------------------- constants
VIDEO_TYPES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".ts", ".flv"}
BOOT = f"{int(time.time()) % 100000:x}"     # part of every sync token, so pages reload their data after a restart

WEB = config.ROOT / "web"

PAGES = {
    "/": "landing.html",
    "/login": "auth.html",
    "/signup": "auth.html",
    "/reset": "reset.html",
    "/privacy": "legal.html",
    "/terms": "legal.html",
    "/acceptable-use": "legal.html",
}


def _from_here(req: Request, must_say: bool = False) -> bool:
    from urllib.parse import urlparse
    if (req.headers.get("sec-fetch-site") or "").lower() == "same-origin":
        return True
    origin = (req.headers.get("origin") or "").rstrip("/").lower()
    if not origin:
        return not must_say
    return urlparse(origin).netloc == (req.headers.get("host") or "").lower() or origin == config.PUBLIC_URL.lower()


def _own_address(host: str) -> bool:
    import ipaddress
    from urllib.parse import urlparse
    name = host.strip().lower()
    name = name[1:name.index("]")] if name.startswith("[") and "]" in name else name.rsplit(":", 1)[0] if ":" in name else name
    if name in ("localhost", urlparse(config.PUBLIC_URL).hostname or "") or name.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _channel_ok(body: dict):
    links = [str(body.get("channel") or "").strip()]
    chs = body.get("channels")
    if chs is not None and not isinstance(chs, dict):
        raise HTTPException(400, "Bad channel list.")
    for plat, url in (chs or {}).items():
        url = str(url or "").strip()
        if url and creators.platform_key(url) != plat:
            raise HTTPException(400, f"That link isn't a {plat.title()} channel.")
        links.append(url)
    for ch in links:
        if ch and not media.site_allowed(live.normalize(ch)):
            raise HTTPException(400, "Channel links must be from YouTube, Twitch or Kick.")


def _on_this_computer(request: Request) -> bool:
    """Is the person sitting at the computer PeakCut runs on? Only then can a folder window be opened for them."""
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost") and not config.env("PEAKCUT_TRUST_PROXY")


def _ai_error(e) -> str:
    from .. import ai
    return safe.friendly(e, 300).replace(ai.key() or "\0", "<key>")


def _back(**q):
    from urllib.parse import urlencode as _ue
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/app/settings/apps?" + _ue(q), 303)


def _signed_in(request: Request, user, first: bool, redirect=None):
    from fastapi.responses import JSONResponse, RedirectResponse
    token = auth.start_session(user.id, _ip(request), request.headers.get("user-agent", ""))
    resp = RedirectResponse(redirect, 303) if redirect else JSONResponse({"user": auth.public(user)})
    resp.set_cookie(**auth.cookie_args(token))
    return resp


def _page(name, shared=False):
    """A page. The browser keeps its copy and asks each time whether it has changed; when it has not, the answer
    is a few bytes ("not modified") instead of the whole page again. Signed-in pages are never kept by anything
    in between (a proxy or CDN), only by the person's own browser."""
    from fastapi.responses import FileResponse
    return FileResponse(WEB / name, headers={"Cache-Control": ("public" if shared else "private") + ", no-cache"})


LEGAL = {
    "privacy": ("Privacy Policy", "How PeakCut collects, uses and protects your data, including Google user data, cookies and your privacy rights."),
    "terms": ("Terms of Service", "The terms for using PeakCut, the AI clipping tool for streams and videos: accounts, availability and liability."),
    "acceptable-use": ("Acceptable Use Policy", "What you may and may not do with PeakCut, the AI stream clipping tool."),
}

_LANDING: dict = {}
_LEGAL_HTML = ""
