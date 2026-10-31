"""Pre-tenant resolvers (§17): ids only, for valid tokens only, callable only by kalkan_api."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from kalkan.db.base import utcnow
from kalkan.db.models import (
    AuthAttempt,
    EvidenceLink,
    Invitation,
    PasswordReset,
    Report,
    User,
    UserSession,
)
from kalkan.db.session import Database
from kalkan.domain.enums import AuthScope
from kalkan.security import hash_token
from tests.factories import TenantRows, tenant_rows

SIGNATURES = {
    "auth_lookup_user(citext)": {"user_id", "customer_id"},
    "auth_resolve_session(text)": {"session_id", "user_id", "customer_id"},
    "auth_resolve_invitation(text)": {"invitation_id", "customer_id"},
    "auth_resolve_password_reset(text)": {"reset_id", "user_id", "customer_id"},
    "resolve_evidence_link(text)": {"link_id", "case_id", "customer_id"},
    "resolve_report_token(text)": {"report_id", "customer_id"},
    "auth_recent_failures(citext, inet, timestamp with time zone)": {
        "email_failures",
        "ip_failures",
    },
}


@pytest.fixture
def rows(database: Database) -> TenantRows:
    with database.worker_session() as session:
        return tenant_rows(session, "resolver-tenant")


def _expire(database: Database, model: type, row_id: object) -> None:
    with database.worker_session() as session:
        session.execute(
            update(model).where(model.id == row_id).values(expires_at=utcnow() - timedelta(1))  # type: ignore[attr-defined]
        )


def test_lookup_user(database: Database, rows: TenantRows) -> None:
    with database.api_preauth_session() as session:
        found = session.lookup_user("OWNER@resolver-tenant.example")  # citext: case-insensitive
        assert found is not None
        assert (found.user_id, found.customer_id) == (rows.ids["users"], rows.customer.id)
        assert session.lookup_user("nobody@example.com") is None
    with database.worker_session() as session:
        session.execute(update(User).values(disabled_at=utcnow()))
    with database.api_preauth_session() as session:
        assert session.lookup_user("owner@resolver-tenant.example") is None


def test_resolve_session(database: Database, rows: TenantRows) -> None:
    token_hash = hash_token(rows.tokens["session"])
    with database.api_preauth_session() as session:
        found = session.resolve_session(token_hash)
        assert found is not None
        assert found.session_id == rows.ids["sessions"]
        assert found.customer_id == rows.customer.id
        assert session.resolve_session(hash_token("wrong")) is None
    _expire(database, UserSession, rows.ids["sessions"])
    with database.api_preauth_session() as session:
        assert session.resolve_session(token_hash) is None


def test_resolve_invitation(database: Database, rows: TenantRows) -> None:
    token_hash = hash_token(rows.tokens["invitation"])
    with database.api_preauth_session() as session:
        found = session.resolve_invitation(token_hash)
        assert found is not None
        assert found.invitation_id == rows.ids["invitations"]
    with database.worker_session() as session:
        session.execute(update(Invitation).values(accepted_at=utcnow()))
    with database.api_preauth_session() as session:
        assert session.resolve_invitation(token_hash) is None


def test_resolve_password_reset(database: Database, rows: TenantRows) -> None:
    token_hash = hash_token(rows.tokens["reset"])
    with database.api_preauth_session() as session:
        found = session.resolve_password_reset(token_hash)
        assert found is not None
        assert (found.reset_id, found.user_id) == (rows.ids["password_resets"], rows.ids["users"])
    with database.worker_session() as session:
        session.execute(update(PasswordReset).values(used_at=utcnow()))
    with database.api_preauth_session() as session:
        assert session.resolve_password_reset(token_hash) is None


def test_resolve_evidence_link(database: Database, rows: TenantRows) -> None:
    token_hash = hash_token(rows.tokens["evidence"])
    with database.api_preauth_session() as session:
        found = session.resolve_evidence_link(token_hash)
        assert found is not None
        assert (found.link_id, found.case_id) == (rows.ids["evidence_links"], rows.ids["cases"])
    with database.worker_session() as session:
        session.execute(update(EvidenceLink).values(revoked_at=utcnow()))
    with database.api_preauth_session() as session:
        assert session.resolve_evidence_link(token_hash) is None


def test_report_token_is_single_use_and_expires(database: Database, rows: TenantRows) -> None:
    token_hash = hash_token("report-token")
    with database.worker_session() as session:
        session.execute(
            update(Report).values(
                print_token_hash=token_hash, print_token_expires_at=utcnow() + timedelta(hours=1)
            )
        )
    with database.api_preauth_session() as session:
        found = session.resolve_report_token(token_hash)
        assert found is not None
        assert (found.report_id, found.customer_id) == (rows.ids["reports"], rows.customer.id)
    with database.api_preauth_session() as session:
        assert session.resolve_report_token(token_hash) is None

    with database.worker_session() as session:
        session.execute(
            update(Report).values(
                print_token_hash=token_hash, print_token_expires_at=utcnow() - timedelta(minutes=1)
            )
        )
    with database.api_preauth_session() as session:
        assert session.resolve_report_token(token_hash) is None


def test_recent_failures_count_customer_failures_by_email_and_ip(database: Database) -> None:
    now = utcnow()
    with database.worker_session() as session:
        for minutes, email, ip, success, scope in (
            (1, "a@example.com", "203.0.113.1", False, AuthScope.CUSTOMER),
            (2, "a@example.com", "203.0.113.2", False, AuthScope.CUSTOMER),
            (3, "b@example.com", "203.0.113.1", False, AuthScope.CUSTOMER),
            (4, "a@example.com", "203.0.113.1", True, AuthScope.CUSTOMER),
            (5, "a@example.com", "203.0.113.1", False, AuthScope.STAFF),
            (60, "a@example.com", "203.0.113.1", False, AuthScope.CUSTOMER),
        ):
            session.add(
                AuthAttempt(
                    scope=scope,
                    email=email,
                    ip=ip,
                    success=success,
                    at=now - timedelta(minutes=minutes),
                )
            )
    with database.api_preauth_session() as session:
        failures = session.recent_failures("A@example.com", "203.0.113.1", now - timedelta(15))
        recent = session.recent_failures(
            "a@example.com", "203.0.113.1", now - timedelta(minutes=15)
        )
    assert (failures.by_email, failures.by_ip) == (3, 3)
    assert (recent.by_email, recent.by_ip) == (2, 2)


@pytest.mark.parametrize(("signature", "columns"), SIGNATURES.items())
def test_resolvers_are_locked_down(database: Database, signature: str, columns: set[str]) -> None:
    name = signature.split("(", 1)[0]
    with database.worker_session() as session:
        info = session.execute(
            text(
                """
                SELECT p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner) AS owner,
                       pg_get_function_result(p.oid) AS result,
                       has_function_privilege('kalkan_api', p.oid, 'EXECUTE') AS api,
                       has_function_privilege('kalkan_worker', p.oid, 'EXECUTE') AS worker,
                       EXISTS (SELECT 1 FROM aclexplode(p.proacl) acl
                               WHERE acl.grantee = 0) AS public_grant
                FROM pg_proc p WHERE p.proname = :name
                """
            ),
            {"name": name},
        ).one()
    assert info.prosecdef is True
    assert info.owner == "kalkan_owner"
    assert info.proconfig == ["search_path=public, pg_temp"]
    assert info.api is True
    assert info.worker is False
    assert info.public_grant is False
    returned = {
        part.strip().split(" ")[0] for part in info.result.removeprefix("TABLE(")[:-1].split(",")
    }
    assert returned == columns


def test_worker_cannot_call_resolvers(database: Database) -> None:
    with pytest.raises(DBAPIError) as caught, database.worker_session() as session:
        session.execute(text("SELECT * FROM auth_lookup_user('a@example.com')"))
    assert getattr(caught.value.orig, "sqlstate", None) == "42501"
