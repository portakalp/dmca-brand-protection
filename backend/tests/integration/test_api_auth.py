"""Sign-in, TOTP, sessions, CSRF, password resets and invitations (no data
endpoint without TOTP, session lifetimes and logout, the CSRF header, the attempt limit)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pyotp
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from kalkan.config import Settings
from kalkan.db.models import AuthAttempt, Invitation, PasswordReset, User, UserSession
from kalkan.db.session import Database
from kalkan.domain.enums import UserRole
from kalkan.security import hash_token
from tests.integration.api_support import (
    PASSWORD,
    Outbox,
    account,
    api,  # noqa: F401 - a fixture
    mailbox,  # noqa: F401 - a fixture
    person,
    sign_in,
)

LOGIN = "/api/v1/auth/login"


def _sessions(database: Database, user_id: uuid.UUID) -> list[UserSession]:
    with database.worker_session() as session:
        rows = session.scalars(select(UserSession).where(UserSession.user_id == user_id)).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def test_an_unknown_email_and_a_wrong_password_look_the_same(
    api: TestClient,  # noqa: F811
    database: Database,
    test_settings: Settings,
) -> None:
    mint = account(database, test_settings)
    unknown = api.post(LOGIN, json={"email": "nobody@nowhere.example", "password": "x" * 12})
    wrong = api.post(LOGIN, json={"email": mint.owner.email, "password": "x" * 12})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"] == "Wrong e-mail or password."
    assert unknown.headers["content-type"].startswith("application/problem+json")
    assert "kalkan_session" not in api.cookies


def test_no_data_without_the_second_factor(
    api: TestClient,  # noqa: F811
    database: Database,
    test_settings: Settings,
) -> None:
    mint = account(database, test_settings)
    assert api.get("/api/v1/me").status_code == 401
    first = api.post(LOGIN, json={"email": mint.owner.email, "password": PASSWORD})
    assert first.json() == {"mfa": "verify"}
    cookie = first.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    for path in ("/api/v1/me", "/api/v1/overview", "/api/v1/threats", "/api/v1/whitelist"):
        response = api.get(path)
        assert response.status_code == 401
        assert response.json()["code"] == "mfa_required"
    assert api.post("/api/v1/auth/mfa", json={"code": "000000"}).status_code == 401
    pending = api.cookies["kalkan_session"]
    assert api.post("/api/v1/auth/mfa", json={"code": mint.owner.code()}).status_code == 204
    assert api.cookies["kalkan_session"] != pending  # a new token after the second factor
    me = api.get("/api/v1/me").json()
    assert me["user"]["email"] == mint.owner.email
    assert me["permissions"]["mark_ours"] is True


def test_the_first_sign_in_sets_up_totp_and_recovery_codes_work_once(
    api: TestClient,  # noqa: F811
    database: Database,
    test_settings: Settings,
) -> None:
    mint = account(database, test_settings)
    fresh = person(database, test_settings, mint.customer_id, UserRole.VIEWER, totp=False)
    first = api.post(LOGIN, json={"email": fresh.email, "password": PASSWORD})
    assert first.json() == {"mfa": "setup"}
    assert api.post("/api/v1/auth/mfa", json={"code": "123456"}).json()["code"] == (
        "setup_required"
    )
    setup = api.post("/api/v1/auth/totp/setup").json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/")
    verified = api.post(
        "/api/v1/auth/totp/verify", json={"code": pyotp.TOTP(setup["secret"]).now()}
    )
    codes = verified.json()["recovery_codes"]
    assert len(codes) == len(set(codes)) == 10
    assert api.get("/api/v1/me").status_code == 200
    assert api.post("/api/v1/auth/totp/setup").status_code == 409

    api.post("/api/v1/auth/logout")
    api.post(LOGIN, json={"email": fresh.email, "password": PASSWORD})
    assert api.post("/api/v1/auth/mfa", json={"code": codes[0]}).status_code == 204
    api.post("/api/v1/auth/logout")
    api.post(LOGIN, json={"email": fresh.email, "password": PASSWORD})
    assert api.post("/api/v1/auth/mfa", json={"code": codes[0]}).status_code == 401
    with database.worker_session() as session:
        assert len(session.get_one(User, fresh.user_id).recovery_codes_hash) == 9


def test_logout_and_session_lifetimes(
    api: TestClient,  # noqa: F811
    database: Database,
    test_settings: Settings,
) -> None:
    mint = account(database, test_settings)
    sign_in(api, mint.owner)
    assert api.post("/api/v1/auth/logout").status_code == 204
    assert api.get("/api/v1/me").status_code == 401
    assert _sessions(database, mint.owner.user_id) == []

    # 12 idle hours end a session; so do 7 days.
    for change in (
        {"last_seen_at": datetime.now(UTC) - timedelta(hours=12, minutes=1)},
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
    ):
        sign_in(api, mint.owner)
        assert api.get("/api/v1/me").status_code == 200
        with database.worker_session() as session:
            for row in session.scalars(
                select(UserSession).where(UserSession.user_id == mint.owner.user_id)
            ):
                for name, value in change.items():
                    setattr(row, name, value)
        assert api.get("/api/v1/me").status_code == 401
    # The idle session was deleted; an expired one is no longer resolved at all.
    assert all(
        row.expires_at < datetime.now(UTC) for row in _sessions(database, mint.owner.user_id)
    )
    with database.worker_session() as session:
        session.execute(delete(UserSession).where(UserSession.user_id == mint.owner.user_id))
    # A session waiting for its second factor lives 30 minutes.
    api.cookies.clear()
    api.post(LOGIN, json={"email": mint.owner.email, "password": PASSWORD})
    [pending] = _sessions(database, mint.owner.user_id)
    assert not pending.mfa_passed
    assert timedelta(minutes=29) < pending.expires_at - datetime.now(UTC) <= timedelta(minutes=30)
    # A full session lives 7 days.
    api.post("/api/v1/auth/mfa", json={"code": mint.owner.code()})
    [full] = _sessions(database, mint.owner.user_id)
    assert full.mfa_passed
    assert timedelta(days=6, hours=23) < full.expires_at - datetime.now(UTC) <= timedelta(days=7)


def test_a_disabled_user_loses_the_session(
    api: TestClient,  # noqa: F811
    database: Database,
    test_settings: Settings,
) -> None:
    mint = account(database, test_settings)
    sign_in(api, mint.viewer)
    with database.worker_session() as session:
        session.get_one(User, mint.viewer.user_id).disabled_at = datetime.now(UTC)
    assert api.get("/api/v1/me").status_code == 401


def test_five_failures_lock_sign_in(
    api: TestClient,  # noqa: F811
    database: Database,
    test_settings: Settings,
) -> None:
    mint = account(database, test_settings)
    for _ in range(5):
        assert (
            api.post(LOGIN, json={"email": mint.owner.email, "password": "y" * 12}).status_code
            == 401
        )
    locked = api.post(LOGIN, json={"email": mint.owner.email, "password": PASSWORD})
    assert locked.status_code == 429
    assert locked.json()["retry_after"] == 15 * 60
    with database.worker_session() as session:
        attempts = session.scalars(
            select(AuthAttempt).where(AuthAttempt.email == mint.owner.email)
        ).all()
    assert [a.success for a in attempts].count(False) == 5
    assert {a.scope.value for a in attempts} == {"customer"}


def test_state_changes_need_the_csrf_header(
    api: TestClient,  # noqa: F811
    database: Database,
    test_settings: Settings,
) -> None:
    mint = account(database, test_settings)
    sign_in(api, mint.owner)
    bare = {"X-Kalkan-CSRF": ""}
    for method, path, body in (
        ("POST", "/api/v1/whitelist", {"pattern": "mint-partner.example"}),
        ("POST", f"/api/v1/threats/{mint.case_id}/mark-ours", {}),
        ("DELETE", f"/api/v1/users/{mint.viewer.user_id}", None),
        ("PATCH", "/api/v1/settings", {"timezone": "UTC"}),
        ("POST", LOGIN, {"email": mint.owner.email, "password": PASSWORD}),
    ):
        response = api.request(method, path, json=body, headers=bare)
        assert response.status_code == 403, path
        assert response.json()["code"] == "csrf"
    assert api.get("/api/v1/me", headers=bare).status_code == 200
    # Webhooks and evidence pages are outside the rule.
    webhook = api.post("/api/webhooks/postmark/inbound", json={}, headers=bare)
    assert webhook.status_code == 401
    assert api.get("/e/no-such-token", headers=bare).status_code == 404


def test_a_password_reset_link_works_once_and_ends_the_sessions(
    api: TestClient,  # noqa: F811
    database: Database,
    test_settings: Settings,
    mailbox: Outbox,  # noqa: F811
) -> None:
    mint = account(database, test_settings)
    other = TestClient(api.app, headers={"X-Kalkan-CSRF": "1"})
    sign_in(other, mint.owner)
    assert (
        api.post("/api/v1/auth/password/forgot", json={"email": "no@one.example"}).status_code
        == 202
    )
    assert mailbox.sent == []
    assert (
        api.post("/api/v1/auth/password/forgot", json={"email": mint.owner.email}).status_code
        == 202
    )
    [mail] = mailbox.sent
    assert mail.to == mint.owner.email
    token = re.search(r"reset-password\?token=([\w-]+)", mail.text)
    assert token is not None
    new = "a much better password 2026"
    assert (
        api.post(
            "/api/v1/auth/password/reset", json={"token": token.group(1), "password": new}
        ).status_code
        == 204
    )
    assert other.get("/api/v1/me").status_code == 401  # every session ended
    assert (
        api.post(
            "/api/v1/auth/password/reset", json={"token": token.group(1), "password": new}
        ).status_code
        == 400
    )
    assert api.post(LOGIN, json={"email": mint.owner.email, "password": new}).status_code == 200
    with database.worker_session() as session:
        reset = session.scalars(select(PasswordReset)).one()
        assert reset.used_at is not None
        assert reset.token_hash == hash_token(token.group(1))


def test_an_invitation_is_accepted_once_then_totp_is_set_up(
    api: TestClient,  # noqa: F811
    database: Database,
    test_settings: Settings,
    mailbox: Outbox,  # noqa: F811
) -> None:
    mint = account(database, test_settings)
    sign_in(api, mint.owner)
    invited = api.post("/api/v1/users", json={"email": "new@customer.example", "role": "viewer"})
    assert invited.status_code == 201, invited.text
    [mail] = mailbox.sent
    token = re.search(r"invite\?token=([\w-]+)", mail.text)
    assert token is not None
    guest = TestClient(api.app, headers={"X-Kalkan-CSRF": "1"})
    accepted = guest.post(
        "/api/v1/invitations/accept",
        json={"token": token.group(1), "password": "a long enough password", "display_name": "N"},
    )
    assert accepted.json() == {"mfa": "setup"}
    assert guest.get("/api/v1/me").status_code == 401  # TOTP first
    again = TestClient(api.app, headers={"X-Kalkan-CSRF": "1"}).post(
        "/api/v1/invitations/accept",
        json={"token": token.group(1), "password": "a long enough password"},
    )
    assert again.status_code == 400
    with database.worker_session() as session:
        user = session.scalars(select(User).where(User.email == "new@customer.example")).one()
        assert (user.role, user.customer_id) == (UserRole.VIEWER, mint.customer_id)
        assert session.scalars(select(Invitation)).one().accepted_at is not None
    # An address with an account elsewhere cannot be invited.
    other = account(database, test_settings)
    assert (
        api.post("/api/v1/users", json={"email": other.owner.email, "role": "viewer"}).status_code
        == 409
    )
