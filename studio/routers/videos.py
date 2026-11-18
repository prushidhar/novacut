"""Videos, jobs, uploads, rendering, and clips management."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .. import account, accounts, auth, config, creators, ctx, jobs, live, load, media, render, usage
from .. import tools as _tools
from ._helpers import (
    VIDEO_TYPES,
    _create_lock,
    _creator_rows,
    _disk,
    _job_or_404,
    _job_slots,
    _lookup,
    _me,
    _need_verified,
    _pairs,
    _quota,
    _sync_token,
    _sync_unchanged,
    _throttle,
    _up_get,
    _up_lock,
    _up_root,
    _up_sweep,
)

router = APIRouter()


@router.get("/api/status")
def status(request: Request):
    from .. import notices, updates

    me_ = _me(request)
    return {
        "user": auth.public(me_),
        "update": updates.latest() if me_.is_admin else None,
        "accounts": config.ACCOUNTS,
        "disk_full": load.disk_full(),
        "no_ffmpeg": bool(_tools.missing()),
        "links": {"repo": config.REPO_URL, "follow": config.FOLLOW_URL},
        "ai": usage.brief(),
        "unread": notices.unread(),
        "tour": not accounts.settings()["tour_done"],
        "youtube": accounts.youtube_ready(),
        "connect": {p: accounts.can_connect(p) for p in accounts.PLATFORM_NAMES},
        "max_upload_mb": config.MAX_UPLOAD_MB,
        "upload_part_mb": config.UPLOAD_PART_MB,
        "max_batch": config.MAX_ACTIVE_JOBS_PER_USER,
        "styles": {k: v["label"] for k, v in render.STYLES.items()},
        "layouts": render.LAYOUTS,
    }


@router.get("/api/sync")
def sync(v: str = ""):
    token = _sync_token()
    if v == token:
        return _sync_unchanged(v) or {"v": token}
    from .. import notices

    return {
        "v": token,
        "jobs": [jobs.public(j) for j in jobs.list_jobs()],
        "creators": _creator_rows(),
        "accounts": accounts.list_accounts(),
        "ai": usage.brief(),
        "unread": notices.unread(),
    }


@router.get("/api/jobs")
def list_jobs():
    return [jobs.public(j) for j in jobs.list_jobs()]


@router.get("/api/jobs/{jid}")
def get_job(jid: str):
    return jobs.public(_job_or_404(jid))


@router.get("/api/preview")
def link_preview(request: Request, url: str = ""):
    url = url.strip()[:500]
    if not media.site_allowed(url):
        raise HTTPException(400, "Paste a full link to a YouTube, Twitch or Kick video.")
    try:
        auth.throttle(f"preview:{_me(request).id}", 12, 60)
    except auth.AuthError:
        raise HTTPException(429, "Too many links at once. Wait a moment.")
    try:
        return _lookup(media.preview, url)
    except media.YouTubeBlocked as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e).lower()
        if "offline" in msg or "not live" in msg or "not currently live" in msg:
            raise HTTPException(422, "This channel is not live right now.")
        raise HTTPException(422, "Couldn't open that video. Check the link is public and complete.")


@router.post("/api/jobs")
def create_from_link(body: dict = Body(...)):
    _need_verified()
    _quota()
    _throttle("newjob:" + ctx.uid(), 30, 600)
    url = str(body.get("url", "")).strip()[:500]
    if not media.site_allowed(url):
        raise HTTPException(400, "Paste a full link to a YouTube, Twitch or Kick video.")
    try:
        media.check_youtube(url)
    except media.YouTubeBlocked as e:
        raise HTTPException(503, str(e))
    creator = str(body.get("creator") or "") or None
    if creator and not creators.get(creator):
        raise HTTPException(404, "No such creator")
    seen = media._PREVIEWS.get(url)
    if seen and seen.get("is_live"):
        try:
            with _create_lock:
                job = jobs.create(
                    {"type": "live", "url": url, "name": seen["title"] or url},
                    body.get("options") or {},
                    enqueue=False,
                    creator=creator,
                    kind="live",
                )
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e) if isinstance(e, ValueError) else "Those options aren't valid.")
        job.update(title=seen["title"], uploader=seen["uploader"], duration=0)
        if media.save_thumb(jobs.job_dir(job["id"]), url=url):
            job["thumb"] = True
        try:
            live.start_from_job(job, seen)
        except ValueError as e:
            jobs.delete(job["id"])
            raise HTTPException(409, str(e))
        return jobs.public(job)
    try:
        with _create_lock:
            _job_slots()
            job = jobs.create({"type": "url", "url": url, "name": url}, body.get("options") or {}, creator=creator)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e) if isinstance(e, ValueError) else "Those options aren't valid.")
    if seen:
        job.update(title=seen["title"], uploader=seen["uploader"], duration=seen["duration"])
        if media.save_thumb(jobs.job_dir(job["id"]), url=url):
            job["thumb"] = True
        jobs.save(job)
    return jobs.public(job)


@router.post("/api/upload")
def create_from_file(file: UploadFile = File(...), options: str = Form("{}"), creator: str = Form("")):
    _need_verified()
    _job_slots()
    name = re.sub(r"[^\w.\- ]", "_", Path(file.filename or "video.mp4").name)[:120]
    ext = Path(name).suffix.lower()
    if ext not in VIDEO_TYPES:
        raise HTTPException(400, "Upload a video file (mp4, mov, mkv or webm).")
    if creator and not creators.get(creator):
        raise HTTPException(404, "No such creator")
    try:
        opts = json.loads(options)
        opts = opts if isinstance(opts, dict) else {}
    except ValueError:
        opts = {}
    updir = config.TMP_DIR / "uploads"
    updir.mkdir(exist_ok=True)
    tmp = updir / f"{os.urandom(8).hex()}{ext}"
    limit, size = config.MAX_UPLOAD_MB * 1024 * 1024, 0
    try:
        with tmp.open("wb") as f:
            while chunk := file.file.read(4 * 1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"That file is too big. The limit is {config.MAX_UPLOAD_MB} MB.")
                f.write(chunk)
        try:
            with _create_lock:
                _job_slots()
                job = jobs.create(
                    {"type": "file", "path": str(tmp), "name": name}, opts, enqueue=False, creator=creator or None
                )
        except (ValueError, TypeError):
            raise HTTPException(400, "Those options aren't valid.")
        _disk.pop(ctx.uid(), None)
        dest = jobs.job_dir(job["id"]) / ("source" + ext)
        shutil.move(str(tmp), dest)
    finally:
        tmp.unlink(missing_ok=True)
    job["source"]["path"] = str(dest)
    jobs.enqueue(job)
    return jobs.public(job)


@router.post("/api/upload/start")
def upload_start(body: dict = Body(...)):
    _need_verified()
    _job_slots()
    _quota()
    _throttle("upstart:" + ctx.uid(), 30, 600)
    name = re.sub(r"[^\w.\- ]", "_", Path(str(body.get("name") or "video.mp4")).name)[:120]
    ext = Path(name).suffix.lower()
    if ext not in VIDEO_TYPES:
        raise HTTPException(400, "Upload a video file (mp4, mov, mkv or webm).")
    size = body.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise HTTPException(400, "Bad file size.")
    if size > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"That file is too big. The limit is {config.MAX_UPLOAD_MB} MB.")
    if _disk.get(ctx.uid(), (0, 0))[1] + size > config.MAX_USER_GB * 1e9:
        raise HTTPException(507, "There isn't room for this file in your storage. Delete some older videos to make room.")
    creator = str(body.get("creator") or "")
    if creator and not creators.get(creator):
        raise HTTPException(404, "No such creator")
    opts = body.get("options") if isinstance(body.get("options"), dict) else {}
    try:
        jobs.clean_options(opts)
    except (ValueError, TypeError):
        raise HTTPException(400, "Those options aren't valid.")
    _up_sweep()
    mine = 0
    for d in _up_root().glob("up-*"):
        try:
            mine += json.loads((d / "meta.json").read_text(encoding="utf-8")).get("user") == ctx.uid()
        except (OSError, ValueError):
            pass
    if mine >= config.MAX_ACTIVE_JOBS_PER_USER:
        raise HTTPException(429, f"You already have {mine} uploads in progress. Let one finish first.")
    part = config.UPLOAD_PART_MB * 1024 * 1024
    upid = os.urandom(12).hex()
    d = _up_root() / f"up-{upid}"
    d.mkdir()
    meta = {
        "user": ctx.uid(),
        "name": name,
        "ext": ext,
        "size": size,
        "part": part,
        "parts": -(-size // part),
        "options": opts,
        "creator": creator,
        "t": time.time(),
    }
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return {"id": upid, "part": part, "parts": meta["parts"]}


@router.put("/api/upload/{upid}/{n}")
async def upload_part(request: Request, upid: str, n: int):
    meta = await run_in_threadpool(_up_get, upid)
    if not 0 <= n < meta["parts"]:
        raise HTTPException(400, "No such piece.")
    want = meta["part"] if n < meta["parts"] - 1 else meta["size"] - meta["part"] * (meta["parts"] - 1)
    d = _up_root() / f"up-{upid}"
    tmp = d / f"{n:05d}.{os.urandom(4).hex()}.tmp"
    got = 0
    try:
        with tmp.open("wb") as f:
            async for chunk in request.stream():
                got += len(chunk)
                if got > want:
                    raise HTTPException(400, "That piece is the wrong size.")
                await run_in_threadpool(f.write, chunk)
        if got != want:
            raise HTTPException(400, "That piece is the wrong size.")
        tmp.replace(d / f"{n:05d}.part")
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "n": n}


@router.post("/api/upload/{upid}/finish")
def upload_finish(upid: str):
    meta = _up_get(upid)
    _need_verified()
    d = _up_root() / f"up-{upid}"
    with _up_lock:
        if not (d / "meta.json").is_file():
            raise HTTPException(404, "This upload has expired. Start it again.")
        parts = [d / f"{i:05d}.part" for i in range(meta["parts"])]
        missing = [i for i, p in enumerate(parts) if not p.is_file()]
        if missing:
            raise HTTPException(409, f"Piece {missing[0] + 1} of {meta['parts']} has not arrived yet.")
        if sum(p.stat().st_size for p in parts) != meta["size"]:
            raise HTTPException(400, "The pieces do not add up to the file.")
        try:
            with _create_lock:
                _job_slots()
                job = jobs.create(
                    {"type": "file", "path": "", "name": meta["name"]},
                    meta["options"],
                    enqueue=False,
                    creator=meta["creator"] or None,
                )
        except (ValueError, TypeError):
            raise HTTPException(400, "Those options aren't valid.")
        dest = jobs.job_dir(job["id"]) / ("source" + meta["ext"])
        try:
            with dest.open("wb") as out:
                for p in parts:
                    with p.open("rb") as src:
                        shutil.copyfileobj(src, out, 4 * 1024 * 1024)
        except OSError:
            jobs.delete(job["id"])
            raise HTTPException(507, "There wasn't room to save this file. Delete some older videos and try again.")
        shutil.rmtree(d, ignore_errors=True)
    _disk.pop(ctx.uid(), None)
    job["source"]["path"] = str(dest)
    jobs.enqueue(job)
    return jobs.public(job)


@router.delete("/api/upload/{upid}")
def upload_cancel(upid: str):
    _up_get(upid)
    shutil.rmtree(_up_root() / f"up-{upid}", ignore_errors=True)
    return {"ok": True}


@router.post("/api/jobs/{jid}/clips/{cid}/render")
def rerender(jid: str, cid: str, body: dict = Body(...)):
    job = _job_or_404(jid)
    clip = next((c for c in job["clips"] if c["id"] == cid), None)
    if not clip:
        raise HTTPException(404, "No such clip")
    if job["status"] in ("queued", "running") or clip.get("status") in ("queued", "rendering"):
        raise HTTPException(409, "This clip is still being made. Try again when it has finished.")
    _throttle("render:" + ctx.uid(), 20, 600, "That's a lot of re-makes in a short time. Wait a few minutes.")
    try:
        jobs.request_render(jid, cid, body)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return jobs.public(job)


@router.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    job = _job_or_404(jid)
    if job["status"] in ("queued", "running", "live") or jid in live.SESSIONS:
        raise HTTPException(409, "This is still being worked on. Stop it or wait for it to finish first.")
    jobs.delete(jid)
    return {"ok": True}


@router.delete("/api/jobs/{jid}/clips/{cid}")
def delete_clip(jid: str, cid: str):
    _job_or_404(jid)
    try:
        if not account.delete_clip(jid, cid):
            raise HTTPException(404, "No such clip")
    except account.Busy as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@router.post("/api/bulk/delete")
def bulk_delete(body: dict = Body(...)):
    _throttle("bulkdel:" + ctx.uid(), 30, 600)
    vids = [
        x
        for x in (body.get("videos") if isinstance(body.get("videos"), list) else [])[:300]
        if isinstance(x, str) and re.fullmatch(r"[\w\-]{8,60}", x)
    ]
    return account.delete_many(vids, _pairs(body.get("clips")))


@router.get("/api/clips.zip")
def clips_zip(ids: str = ""):
    _throttle("zip:" + ctx.uid(), 12, 600, "Give it a moment: you just downloaded several groups of clips.")
    pairs = _pairs([p.split(".", 1) for p in ids.split(",") if "." in p][:300])
    try:
        f = account.zip_clips(pairs)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return FileResponse(
        f,
        filename=f"peakcut-clips-{len(pairs)}.zip",
        media_type="application/zip",
        headers={"Cache-Control": "private, no-store"},
        background=BackgroundTask(lambda: f.unlink(missing_ok=True)),
    )


@router.get("/media/{jid}/{name}")
def media_file(jid: str, name: str, download: int = 0):
    if not re.fullmatch(r"[\w\-]+\.(mp4|jpg)", name):
        raise HTTPException(404)
    f = Path(_job_or_404(jid)["dir"]) / "clips" / name
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, filename=name if download else None, headers={"Cache-Control": "private, max-age=300"})
