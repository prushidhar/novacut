"""Admin dashboard and management routes."""
from __future__ import annotations

import re

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import account, admin as admins, auth, config, db
from ._helpers import _admin

router = APIRouter()


def _uid_ok(uid: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]{6,40}", uid):
        raise HTTPException(404)
    return uid


@router.get("/api/admin/email-preview")
def email_preview(request: Request, kind: str = "verify"):
    """What account emails look like, for admins."""
    from .. import mailer

    _admin(request)
    if kind == "reset":
        page = mailer.preview(
            "Reset your password",
            "Someone asked to reset the password for this PeakCut account. If that was you, choose a new one below.",
            "Choose a new password",
            config.PUBLIC_URL + "/reset?t=example",
            "The link works for one hour. If you didn't ask for this, ignore this email and your password stays the same.",
        )
    else:
        page = mailer.preview()
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.post("/api/admin/test-email")
def test_email(request: Request):
    """Send the confirm-email template to the admin, to check the email service works."""
    from .. import mailer

    me_ = _admin(request)
    if not mailer.ready():
        raise HTTPException(400, "No email service is set up yet. Add the PEAKCUT_SMTP settings to .env first.")
    mailer.send(
        me_.email,
        "PeakCut test email",
        "Your emails are working",
        "This is a test from your PeakCut server. Account emails like this one will now reach people.",
        "Open PeakCut",
        config.PUBLIC_URL + "/app",
        "You can ignore this message.",
    )
    return {"sent_to": me_.email}


@router.get("/api/admin/feedback")
def admin_feedback(request: Request, status: str = ""):
    from .. import feedback

    _admin(request)
    return feedback.listing(status)


@router.post("/api/admin/feedback/{fid}")
def admin_feedback_status(request: Request, fid: int, body: dict = Body(...)):
    from .. import feedback

    _admin(request)
    try:
        feedback.set_status(fid, str(body.get("status", "")))
    except feedback.Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.get("/api/admin/overview")
def admin_overview(request: Request):
    _admin(request)
    return admins.overview()


@router.get("/api/admin/users/{uid}")
def admin_user(request: Request, uid: str):
    _admin(request)
    try:
        return admins.user_detail(_uid_ok(uid))
    except admins.Refused as e:
        raise HTTPException(e.status, str(e))


@router.post("/api/admin/users/{uid}/disabled")
def admin_disable(request: Request, uid: str, body: dict = Body(...)):
    me_ = _admin(request)
    try:
        return admins.set_disabled(me_, _uid_ok(uid), body.get("disabled"))
    except admins.Refused as e:
        raise HTTPException(e.status, str(e))


@router.post("/api/admin/users/{uid}/delete")
def admin_delete_user(request: Request, uid: str, body: dict = Body(...)):
    me_ = _admin(request)
    target = auth.get_user(_uid_ok(uid))
    if not target:
        raise HTTPException(404, "No such account")
    if target.id == me_.id:
        raise HTTPException(400, "You can't delete your own account from here.")
    if str(body.get("confirm") or "").strip().lower() != target.email.lower():
        raise HTTPException(400, "Type the account's email address exactly to confirm.")
    try:
        account.delete_account(target.id)
    except account.Busy as e:
        raise HTTPException(409, str(e))
    with db.engine.begin() as c:
        admins._log(c, me_.id, "delete", target.id, "account deleted")
    return {"ok": True}
