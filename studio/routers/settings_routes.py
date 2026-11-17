"""Settings, AI, storage, external accounts, posting, alerts, and feedback routes."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .. import account, accounts, ai, alerts, auth, config, creators, ctx, jobs, posting, safe, usage
from ._helpers import _admin, _ai_error, _back, _me, _on_this_computer, _throttle

router = APIRouter()


@router.get("/api/notifications")
def notifications_list():
    from .. import notices

    return notices.mine()


@router.post("/api/notifications/read")
def notifications_read():
    from .. import notices

    notices.read_all()
    return {"ok": True}


@router.get("/api/me/photo")
def my_photo(request: Request):
    u = _me(request)
    got = auth.photo(u.id) if u else None
    if not got:
        raise HTTPException(404)
    return FileResponse(got[0], media_type=got[1], headers={"Cache-Control": "private, max-age=86400"})


@router.post("/api/feedback")
def send_feedback(request: Request, body: dict = Body(...)):
    from .. import feedback

    u = _me(request)
    try:
        auth.throttle("feedback:" + u.id, 10, 3600)
        return feedback.submit(
            u.id,
            str(body.get("kind", "")),
            str(body.get("message", "")),
            str(body.get("page", "")),
            str(body.get("context", "")),
        )
    except auth.AuthError:
        raise HTTPException(429, "You've sent a lot of messages in the last hour. Try again a bit later.")
    except feedback.Refused as e:
        raise HTTPException(400, str(e))


@router.get("/api/usage")
def ai_usage():
    return usage.summary({c["id"]: c["name"] for c in creators.all_creators()})


@router.get("/api/ai")
def ai_get(request: Request):
    _admin(request)
    return ai.public()


@router.put("/api/ai")
def ai_put(request: Request, body: dict = Body(...)):
    _admin(request)
    k = body.get("key")
    try:
        out = ai.save(
            body.get("provider"),
            body.get("models"),
            body.get("base_url") or "",
            None if k is None or (isinstance(k, str) and not k.strip() and not body.get("remove_key")) else str(k),
        )
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    usage.forget_all()
    ctx.bump()
    return out


@router.post("/api/ai/models")
def ai_models(request: Request, body: dict = Body(default={})):
    _admin(request)
    _throttle("aimodels:" + ctx.uid(), 30, 600)
    try:
        return {"models": ai.list_models(body.get("provider"), body.get("base_url"), body.get("key"))}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            502,
            "The service did not give its list of models. Check the key, or type the model yourself. "
            + _ai_error(e)[:160],
        )


@router.post("/api/ai/test")
def ai_test(request: Request):
    _admin(request)
    _throttle("aitest:" + ctx.uid(), 20, 600)
    try:
        return ai.test()
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, ai.explain(e) or "The AI did not answer: " + _ai_error(e))


@router.get("/api/overview")
def overview():
    return posting.overview()


@router.get("/api/settings")
def get_settings():
    return accounts.public_settings()


@router.put("/api/settings")
def put_settings(body: dict = Body(...)):
    try:
        accounts.save_settings(body)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return accounts.public_settings()


@router.get("/api/accounts")
def list_accounts():
    return accounts.list_accounts()


@router.delete("/api/accounts/{aid}")
def remove_account(aid: str):
    accounts.remove(aid)
    return {"ok": True}


@router.get("/oauth/{platform}/start")
def connect_start(platform: str):
    if platform not in accounts.PLATFORM_NAMES:
        raise HTTPException(404)
    try:
        return RedirectResponse(accounts.connect_url(platform), 303)
    except ValueError as e:
        return _back(connect_error=str(e))


@router.get("/oauth/{platform}/callback")
def connect_callback(
    platform: str,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    error_reason: str = "",
):
    if platform not in accounts.PLATFORM_NAMES:
        raise HTTPException(404)
    name = accounts.PLATFORM_NAMES[platform]
    if error or not code:
        why = (
            "you cancelled"
            if error in ("access_denied",) or error_reason == "user_denied"
            else (error_description or error or "no sign-in code came back")
        )
        return _back(connect_error=f"{name} was not connected: {why[:160]}.")
    try:
        _throttle("connect:" + ctx.uid(), 30, 600)
        acc = accounts.connect_finish(platform, code, state)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return _back(connect_error=f"{name} was not connected: {safe.friendly(e, 240)}")
    return _back(connected=platform, name=acc["name"])


@router.get("/pub/{token}.mp4")
def public_clip(token: str):
    """A clip for a platform to fetch while posting it: one clip, one hour, signed."""
    got = accounts.read_clip_link(token)
    if not got:
        raise HTTPException(404)
    uid, job_id, name = got
    with ctx.as_user(uid):
        job = jobs.get(job_id) if re.fullmatch(r"[\w\-]{8,60}", job_id) else None
        f = Path(job["dir"]) / "clips" / name if job else None
    if not f or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})


@router.get("/api/storage")
def storage_get(request: Request):
    from .. import storage

    _admin(request)
    return storage.info(_on_this_computer(request))


@router.post("/api/storage/open")
def storage_open(request: Request):
    from .. import storage

    _admin(request)
    if not _on_this_computer(request):
        raise HTTPException(400, "The folder can only be opened on the computer PeakCut runs on.")
    try:
        storage.open_folder()
    except OSError as e:
        raise HTTPException(500, "Couldn't open the folder: " + safe.friendly(e, 200))
    return {"ok": True}


@router.post("/api/storage/pick")
def storage_pick(request: Request):
    from .. import storage

    _admin(request)
    if not _on_this_computer(request):
        raise HTTPException(
            400, "Type the folder's path instead: a folder window can only be shown on the computer PeakCut runs on."
        )
    return {"path": storage.pick_folder()}


@router.put("/api/storage")
def storage_put(request: Request, body: dict = Body(...)):
    from .. import storage

    _admin(request)
    try:
        if body.get("cancel"):
            storage.cancel_change()
            return storage.info(_on_this_computer(request))
        storage.change(str(body.get("path") or ""), bool(body.get("move", True)))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return storage.info(_on_this_computer(request))


@router.get("/api/apps")
def apps_get(request: Request):
    _admin(request)
    return accounts.apps_public()


@router.put("/api/apps")
def apps_put(request: Request, body: dict = Body(...)):
    _admin(request)
    try:
        return accounts.save_app(str(body.get("platform", "")), body.get("client_id"), body.get("client_secret"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/posts")
def add_posts(body: dict = Body(...)):
    job = jobs.get(str(body.get("job")))
    clip = next((c for c in job["clips"] if c["id"] == body.get("clip")), None) if job else None
    if not clip or clip.get("status") != "done":
        raise HTTPException(404, "That clip is not ready")
    made = posting.enqueue(job, clip, [str(a) for a in body.get("accounts", [])][:20], now=bool(body.get("now")))
    if not made:
        raise HTTPException(400, "Nothing was queued. Pick at least one account this clip has not already gone to.")
    return {"queued": len(made)}


@router.post("/api/posts/{pid}/{action}")
def post_action(pid: str, action: str):
    try:
        {"cancel": posting.cancel, "retry": posting.retry}[action](pid)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/api/alerts/test")
def test_alert():
    if not alerts.configured():
        raise HTTPException(400, "Save a Discord webhook or Telegram details first.")
    auth_problem = None
    try:
        auth.throttle("alert:" + ctx.uid(), 10, 600)
    except auth.AuthError as e:
        auth_problem = str(e)
    if auth_problem:
        raise HTTPException(429, auth_problem)
    problems = alerts.send("PeakCut: test message. Alerts are working.")
    if problems:
        raise HTTPException(400, "; ".join(problems))
    return {"ok": True}


@router.post("/api/account/delete")
def delete_my_account(request: Request, body: dict = Body(...)):
    """Deletes the signed-in account and everything in it."""
    me_ = _me(request)
    if str(body.get("confirm") or "").strip().lower() != me_.email.lower():
        raise HTTPException(400, "Type your email address exactly to confirm.")
    try:
        account.delete_account(me_.id)
    except account.Busy as e:
        raise HTTPException(409, str(e))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.cookie_args("x")["key"], path="/")
    return resp
