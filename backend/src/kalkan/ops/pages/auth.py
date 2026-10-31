"""Sign-in, TOTP and sign-out pages (architecture.md §19; K117)."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response

from kalkan.ops import auth
from kalkan.ops.web import (
    client_ip,
    csrf_guard,
    now,
    redirect,
    render,
    services,
    transaction,
)
from kalkan.security import new_token

router = APIRouter()

Text = Annotated[str, Form()]


def _cookie(response: Response, name: str, value: str, max_age: timedelta) -> None:
    response.set_cookie(
        name,
        value,
        max_age=int(max_age.total_seconds()),
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )


def _wait_text(lock: timedelta) -> str:
    minutes = max(1, round(lock.total_seconds() / 60))
    return f"Too many failed attempts. Try again in {minutes} minute(s)."


def _login_page(request: Request, error: str | None = None, status_code: int = 200) -> Response:
    token = request.cookies.get(auth.LOGIN_CSRF_COOKIE) or new_token()
    response = render(
        request, "login.html", {"login_csrf": token, "error": error}, status_code=status_code
    )
    _cookie(response, auth.LOGIN_CSRF_COOKIE, token, auth.PENDING_LIFETIME)
    return response


@router.get("/login")
def login_form(request: Request) -> Response:
    return _login_page(request)


@router.post("/login")
def login(request: Request, email: Text, password: Text, csrf: Text) -> Response:
    if not auth.same(request.cookies.get(auth.LOGIN_CSRF_COOKIE), csrf):
        return _login_page(request, "The form expired; try again.", 403)
    address = email.strip().lower()
    ip = client_ip(request)
    at = now()
    with transaction(request) as session:
        lock = auth.locked_for(session, address, ip, at)
        if lock is not None:
            return _login_page(request, _wait_text(lock), 429)
        user = auth.check_password(session, address, password)
        if user is None:
            auth.record_attempt(session, email=address, ip=ip, success=False, now=at)
            error = "Wrong e-mail or password."
            token = None
        else:
            # A new sign-in replaces whatever session this browser had.
            auth.end_session(session, request.cookies.get(auth.SESSION_COOKIE))
            token = auth.open_session(session, user, now=at, mfa_passed=False)
    if token is None:
        return _login_page(request, error, 401)
    response = redirect("/login/totp")
    _cookie(response, auth.SESSION_COOKIE, token, auth.PENDING_LIFETIME)
    return response


@router.get("/login/totp")
def totp_form(request: Request) -> Response:
    with transaction(request) as session:
        user = auth.pending_user(session, request.cookies.get(auth.SESSION_COOKIE), now())
    if user is None:
        return redirect("/login")
    return render(
        request, "totp.html", {"csrf": auth.csrf_for(request.cookies[auth.SESSION_COOKIE])}
    )


@router.post("/login/totp")
def totp(request: Request, code: Text, csrf: Text) -> Response:
    pending = request.cookies.get(auth.SESSION_COOKIE)
    if pending is None or not auth.same(auth.csrf_for(pending), csrf):
        return redirect("/login")
    ip = client_ip(request)
    at = now()
    settings = services(request).settings
    with transaction(request) as session:
        user = auth.pending_user(session, pending, at)
        if user is None:
            return redirect("/login", "The sign-in expired; start again.")
        lock = auth.locked_for(session, user.email, ip, at)
        if lock is not None:
            auth.end_session(session, pending)
            return redirect("/login", _wait_text(lock))
        if not auth.check_totp(settings, user, code):
            auth.record_attempt(session, email=user.email, ip=ip, success=False, now=at)
            ok = False
        else:
            # A new token for the full session: the pending one is never promoted.
            auth.end_session(session, pending)
            token = auth.open_session(session, user, now=at, mfa_passed=True)
            user.last_login_at = at
            auth.record_attempt(session, email=user.email, ip=ip, success=True, now=at)
            auth.audit(session, user.email, "staff.login", ip=ip, target=user.email)
            ok = True
    if not ok:
        return render(
            request,
            "totp.html",
            {"csrf": auth.csrf_for(pending), "error": "Wrong code."},
            status_code=401,
        )
    response = redirect("/")
    _cookie(response, auth.SESSION_COOKIE, token, auth.SESSION_LIFETIME)
    response.delete_cookie(auth.LOGIN_CSRF_COOKIE, path="/")
    return response


@router.post("/logout")
def logout(request: Request, staff: Annotated[auth.Staff, Depends(csrf_guard)]) -> Response:
    with transaction(request) as session:
        auth.end_session(session, request.cookies.get(auth.SESSION_COOKIE))
        auth.audit(session, staff.email, "staff.logout", ip=client_ip(request), target=staff.email)
    response = redirect("/login")
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@router.get("/")
def home() -> Response:
    return redirect("/cases")
