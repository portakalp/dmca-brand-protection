"""The ops panel end to end through its HTTP pages.

The app runs on the test database, the storage emulator and the test Redis database, served by
Starlette's test client over https (the cookies are Secure).
"""

from __future__ import annotations

import io
import re
import uuid
from datetime import timedelta
from typing import Any

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kalkan.db.base import utcnow
from kalkan.db.models import (
    Artifact,
    AuditLog,
    AuthAttempt,
    BrandAsset,
    Case,
    CaseEvent,
    Customer,
    Invitation,
    JobOutbox,
    Label,
    Setting,
    StaffSession,
    WhitelistEntry,
)
from kalkan.db.outbox import CLEAR_BLOCKED_CASES, COMPUTE_SIGNALS, HASH_BRAND_ASSETS
from kalkan.db.session import Database
from kalkan.domain.enums import (
    ArtifactKind,
    BlockedReason,
    BrandAssetKind,
    CaseState,
    Category,
    EventType,
)
from kalkan.notices.blocks import clear_blocked_cases
from kalkan.ops import auth
from kalkan.ops.web import Services
from kalkan.scheduler import reconciler
from kalkan.security import hash_token
from tests import factories
from tests.integration.ops_support import (
    EMAIL,
    SCREENSHOT,
    captured_case,
    csrf_of,
    password_step,
    sign_in,
)


def _audit(database: Database, action: str) -> list[AuditLog]:
    with database.worker_session() as session:
        return list(session.scalars(select(AuditLog).where(AuditLog.action == action)))


def _outbox(database: Database, kind: str) -> list[dict[str, Any]]:
    with database.worker_session() as session:
        return [
            row.payload for row in session.scalars(select(JobOutbox).where(JobOutbox.kind == kind))
        ]


# --- sign-in ------------------------------------------------------------------------------------


def test_sign_in_cannot_finish_without_the_totp_code(
    ops_client: TestClient, totp_secret: str, database: Database
) -> None:
    assert ops_client.get("/cases", follow_redirects=False).headers["location"] == "/login"
    assert password_step(ops_client).status_code == 303
    # The password alone opens a pending session that no page accepts.
    assert ops_client.get("/cases", follow_redirects=False).headers["location"] == "/login"
    page = ops_client.get("/login/totp")
    wrong = ops_client.post(
        "/login/totp", data={"code": "000000", "csrf": csrf_of(page.text)}, follow_redirects=False
    )
    assert wrong.status_code == 401
    assert ops_client.get("/cases", follow_redirects=False).status_code == 303

    sign_in(ops_client, totp_secret)
    assert ops_client.get("/cases").status_code == 200
    with database.worker_session() as session:
        sessions = list(session.scalars(select(StaffSession)))
        # The pending sessions are gone; the full one has a fresh token.
        assert [row.mfa_passed for row in sessions] == [True]
        assert sessions[0].token_hash == hash_token(ops_client.cookies[auth.SESSION_COOKIE])
    assert [row.actor for row in _audit(database, "staff.login")] == [EMAIL]


def test_failed_attempts_lock_the_sign_in(
    ops_client: TestClient, totp_secret: str, database: Database
) -> None:
    for _ in range(auth.MAX_FAILURES):
        assert password_step(ops_client, "wrong password!").status_code == 401
    # Locked: even the right password is refused now.
    locked = password_step(ops_client)
    assert locked.status_code == 429
    assert "Too many failed attempts" in locked.text
    with database.worker_session() as session:
        attempts = list(session.scalars(select(AuthAttempt)))
    assert len(attempts) == auth.MAX_FAILURES
    assert {(row.scope.value, row.success) for row in attempts} == {("staff", False)}


def test_failed_totp_codes_count_too(
    ops_client: TestClient, totp_secret: str, database: Database
) -> None:
    password_step(ops_client)
    for _ in range(auth.MAX_FAILURES):
        page = ops_client.get("/login/totp")
        ops_client.post("/login/totp", data={"code": "123456", "csrf": csrf_of(page.text)})
    page = ops_client.get("/login/totp")
    locked = ops_client.post(
        "/login/totp",
        data={"code": pyotp.TOTP(totp_secret).now(), "csrf": csrf_of(page.text)},
        follow_redirects=False,
    )
    assert locked.headers["location"] == "/login"
    assert ops_client.get("/cases", follow_redirects=False).headers["location"] == "/login"


def test_lock_grows_and_ends() -> None:
    now = utcnow()
    five = [now - timedelta(minutes=1)] * auth.MAX_FAILURES
    assert auth._locked_for(five[:-1], now) is None
    assert auth._locked_for(five, now) == timedelta(minutes=14)
    ten = [now - timedelta(hours=2)] * 5 + five
    assert auth._locked_for(ten, now) == timedelta(minutes=29)
    assert auth._locked_for(five, now + timedelta(minutes=15)) is None


def test_posts_need_the_csrf_token(signed_in: tuple[TestClient, str], database: Database) -> None:
    client, csrf = signed_in
    refused = client.post("/customers", data={"name": "Nope"})
    assert refused.status_code == 403
    wrong = client.post("/customers", data={"name": "Nope", "csrf": "x" * 64})
    assert wrong.status_code == 403
    ok = client.post("/customers", data={"name": "Yes Pay", "csrf": csrf}, follow_redirects=False)
    assert ok.status_code == 303
    with database.worker_session() as session:
        assert session.scalars(select(Customer.name)).all() == ["Yes Pay"]


def test_sign_out_ends_the_session(signed_in: tuple[TestClient, str], database: Database) -> None:
    client, csrf = signed_in
    client.post("/logout", data={"csrf": csrf})
    assert client.get("/cases", follow_redirects=False).headers["location"] == "/login"
    with database.worker_session() as session:
        assert session.scalars(select(StaffSession)).all() == []
    assert len(_audit(database, "staff.logout")) == 1


def test_pages_send_security_headers(signed_in: tuple[TestClient, str]) -> None:
    client, _ = signed_in
    for path in ("/cases", "/labels", "/customers", "/settings", "/jobs", "/system"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert page.headers["x-frame-options"] == "DENY"
        assert page.headers["cache-control"] == "no-store"


# --- cases and labelling ------------------------------------------------------------------------


def test_labelling_writes_labels_and_the_audit_log(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, csrf = signed_in
    first, second = sorted(
        (
            captured_case(ops_services.database, ops_services.store, ops_services.settings)
            for _ in range(2)
        ),
        key=lambda case: case.number,
    )
    with ops_services.database.worker_session() as session:
        # A case without a screenshot is not in the queue.
        owner = factories.customer(session)
        factories.case(session, owner, factories.brand(session, owner))

    page = client.get("/labels")
    assert first.number in page.text
    assert "2 in the queue" in page.text
    assert 'src="/files/assets/' in page.text  # the official reference next to the candidate
    assert 'data-key="1"' in page.text

    # N skips to the next case.
    skipped = client.get(f"/labels?after={first.number}")
    assert second.number in skipped.text
    saved = client.post(
        f"/labels/{second.id}",
        data={
            "category": "parked_or_empty",
            "note": " empty page ",
            "after": first.number,
            "csrf": csrf,
        },
        follow_redirects=False,
    )
    assert saved.headers["location"] == f"/labels?after={first.number}"
    # The pass ended: the skipped case comes back.
    again = client.get(saved.headers["location"])
    assert first.number in again.text
    assert "1</strong> / 200" in again.text

    client.post(f"/labels/{first.id}", data={"category": "phishing_clone", "csrf": csrf})
    assert "The queue is empty" in client.get("/labels").text
    # Labelling again from the case page replaces the label.
    client.post(f"/labels/{first.id}", data={"category": "unclear", "back": "case", "csrf": csrf})
    with ops_services.database.worker_session() as session:
        labels = {
            row.case_id: (row.category, row.note, row.labeled_by)
            for row in session.scalars(select(Label))
        }
    assert labels == {
        first.id: (Category.UNCLEAR, None, EMAIL),
        second.id: (Category.PARKED_OR_EMPTY, "empty page", EMAIL),
    }
    audit = _audit(ops_services.database, "label.set")
    assert [(row.target, row.data["category"], row.data["old"]) for row in audit] == [
        (second.number, "parked_or_empty", None),
        (first.number, "phishing_clone", None),
        (first.number, "unclear", "phishing_clone"),
    ]
    bad = client.post(f"/labels/{first.id}", data={"category": "spam", "csrf": csrf})
    assert bad.status_code == 400


def test_case_search_and_detail(signed_in: tuple[TestClient, str], ops_services: Services) -> None:
    client, _ = signed_in
    case = captured_case(
        ops_services.database,
        ops_services.store,
        ops_services.settings,
        domain="mintera-verify.test",
    )
    captured_case(
        ops_services.database, ops_services.store, ops_services.settings, domain="other-site.test"
    )
    found = client.get("/cases", params={"q": "verify"})
    assert "mintera-verify.test" in found.text
    assert "other-site.test" not in found.text
    assert case.number in client.get("/cases", params={"q": case.number, "state": "detected"}).text
    assert "No cases." in client.get("/cases", params={"q": "verify", "state": "approved"}).text
    detail = client.get(f"/cases/{case.id}")
    assert detail.status_code == 200
    assert "Mintera Bank | Login" in detail.text
    assert client.get(f"/cases/{uuid.uuid4()}").status_code == 404


def test_files_stream_images_inline_and_everything_else_as_downloads(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, _ = signed_in
    case = captured_case(ops_services.database, ops_services.store, ops_services.settings)
    bucket = ops_services.settings.gcs_evidence_bucket
    assert bucket is not None
    with ops_services.database.worker_session() as session:
        image = session.scalars(select(Artifact).where(Artifact.case_id == case.id)).one()
        html = ops_services.store.put(
            bucket, f"cases/{case.id}/page.html", b"<script>alert(1)</script>", "text/html"
        )
        session.add(
            Artifact(
                customer_id=case.customer_id,
                case_id=case.id,
                kind=ArtifactKind.HTML,
                storage_key=html.key,
                sha256=html.sha256,
                bytes=html.size,
                content_type="text/html",
            )
        )
        session.flush()
        html_id = session.scalars(
            select(Artifact.id).where(Artifact.kind == ArtifactKind.HTML)
        ).one()
    shown = client.get(f"/files/artifacts/{image.id}")
    assert shown.content == SCREENSHOT
    assert shown.headers["content-type"] == "image/png"
    assert shown.headers["content-disposition"].startswith("inline")
    assert shown.headers["content-security-policy"].startswith("sandbox")
    hostile = client.get(f"/files/artifacts/{html_id}")
    assert hostile.headers["content-type"] == "application/octet-stream"
    assert hostile.headers["content-disposition"].startswith("attachment")
    assert hostile.headers["content-security-policy"].startswith("sandbox")
    assert client.get(f"/files/artifacts/{uuid.uuid4()}").status_code == 404


# --- onboarding ---------------------------------------------------------------------------------


def test_a_customer_is_set_up_from_scratch(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, csrf = signed_in
    database = ops_services.database
    created = client.post(
        "/customers",
        data={"name": "Güven Finans", "alert_email": "alerts@guvenfinans.test", "csrf": csrf},
        follow_redirects=False,
    )
    page_url = created.headers["location"]
    with database.worker_session() as session:
        customer = session.scalars(select(Customer)).one()
    assert (customer.slug, customer.status.value, customer.dry_run, customer.sending_enabled) == (
        "guven-finans",
        "trial",
        True,
        False,
    )
    assert page_url == f"/customers/{customer.id}"

    client.post(
        f"/customers/{customer.id}/brands",
        data={
            "name": "Güven Finans",
            "keywords": "guvenfinans, guven-finans",
            "markets": "TR",
            "languages": "tr",
            "csrf": csrf,
        },
    )
    bad_brand = client.post(
        f"/customers/{customer.id}/brands", data={"name": "X", "keywords": "xy", "csrf": csrf}
    )
    assert "at least 4 characters" in bad_brand.text
    with database.worker_session() as session:
        brand = session.scalars(select(factories.Brand)).one()
    assert brand.keywords == ["guvenfinans", "guven-finans"]

    client.post(
        f"/brands/{brand.id}/domains",
        data={"domain": "guvenfinans.test", "kind": "primary", "csrf": csrf},
    )
    logo = client.post(
        f"/brands/{brand.id}/assets",
        data={"kind": "logo_light", "csrf": csrf},
        files={"file": ("logo.png", SCREENSHOT, "image/png")},
    )
    assert "logo_light uploaded" in logo.text
    fake = client.post(
        f"/brands/{brand.id}/assets",
        data={"kind": "logo_dark", "csrf": csrf},
        files={"file": ("logo.png", b"<html>not an image</html>", "image/png")},
    )
    assert "unsupported file type" in fake.text
    client.post(f"/brands/{brand.id}/references", data={"csrf": csrf})

    # The whitelist: a preview with row errors imports nothing; a valid file imports.
    broken = (
        "pattern,kind,note\ngf-partner.test,affiliate,\nnot a host,affiliate,\nok.test,friend,\n"
    )
    preview = client.post(
        f"/customers/{customer.id}/whitelist/preview",
        data={"csrf": csrf, "brand": ""},
        files={"file": ("list.csv", broken.encode(), "text/csv")},
    )
    assert "2 invalid row(s)" in preview.text
    assert "<td>3</td>" in preview.text
    assert "<td>4</td>" in preview.text
    assert "unknown kind &#39;friend&#39;" in preview.text
    good = "pattern,kind,note\ngf-partner.test,affiliate,Partner\nguvenfinans.test,official,\n"
    preview = client.post(
        f"/customers/{customer.id}/whitelist/preview",
        data={"csrf": csrf, "csv_text": good, "brand": str(brand.id)},
    )
    assert "Preview: 1 new, 1 already listed." in preview.text
    with database.worker_session() as session:
        assert session.scalars(
            select(WhitelistEntry.pattern).order_by(WhitelistEntry.pattern)
        ).all() == ["guvenfinans.test"]
    imported = client.post(
        f"/customers/{customer.id}/whitelist/import",
        data={"csrf": csrf, "csv_text": good, "brand": str(brand.id)},
    )
    assert "1 imported, 1 already listed" in imported.text

    # Flags: the confirmation box is checked by the server.
    unconfirmed = client.post(
        f"/customers/{customer.id}/flags",
        data={"flag": "sending_enabled", "value": "true", "csrf": csrf},
    )
    assert "tick the confirmation box" in unconfirmed.text
    client.post(
        f"/customers/{customer.id}/flags",
        data={"flag": "sending_enabled", "value": "true", "confirm": "yes", "csrf": csrf},
    )
    client.post(
        f"/customers/{customer.id}/flags",
        data={"flag": "is_test", "value": "true", "confirm": "yes", "csrf": csrf},
    )
    client.post(
        f"/customers/{customer.id}/loa",
        data={"signed_on": "2026-10-20", "confirm": "yes", "csrf": csrf},
        files={"file": ("loa.pdf", b"%PDF-1.7 signed letter", "application/pdf")},
    )
    invited = client.post(
        f"/customers/{customer.id}/invitations",
        data={"email": "Owner@guvenfinans.test", "csrf": csrf},
    )
    link = re.search(r"/invite/([A-Za-z0-9_-]+)", invited.text)
    assert link is not None

    with database.worker_session() as session:
        customer = session.get_one(Customer, customer.id)
        assert (customer.sending_enabled, customer.is_test, customer.dry_run) == (True, True, True)
        assert customer.loa_signed_at is not None
        assert customer.loa_signed_at.date().isoformat() == "2026-10-20"
        assert customer.loa_file_key is not None
        assert ops_services.store.read_bytes(customer.loa_file_key) == b"%PDF-1.7 signed letter"
        invitation = session.scalars(select(Invitation)).one()
        assert (invitation.email, invitation.role.value) == ("owner@guvenfinans.test", "owner")
        assert invitation.token_hash == hash_token(link.group(1))
        assets = session.scalars(select(BrandAsset)).all()
        assert [asset.kind for asset in assets] == [BrandAssetKind.LOGO_LIGHT]
        assert ops_services.store.read_bytes(assets[0].storage_key) == SCREENSHOT
    assert _outbox(database, HASH_BRAND_ASSETS) == [{"brand_id": str(brand.id)}]
    # sending_enabled and the LOA lift gates; is_test does not.
    assert _outbox(database, CLEAR_BLOCKED_CASES) == [{"customer_id": str(customer.id)}] * 2
    actions = {row.action for row in _audit(database, "customer.sending_enabled")} | {
        row.action for row in _audit(database, "customer.loa")
    }
    assert actions == {"customer.sending_enabled", "customer.loa"}
    page = client.get(f"/customers/{customer.id}")
    assert "guvenfinans.test" in page.text
    assert "owner@guvenfinans.test" in page.text


def test_a_test_customer_stays_in_dry_run(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, csrf = signed_in
    with ops_services.database.worker_session() as session:
        customer = factories.customer(session)
    client.post(
        f"/customers/{customer.id}/flags",
        data={"flag": "is_test", "value": "true", "confirm": "yes", "csrf": csrf},
    )
    refused = client.post(
        f"/customers/{customer.id}/flags",
        data={"flag": "dry_run", "value": "false", "confirm": "yes", "csrf": csrf},
    )
    assert "always stays in dry-run" in refused.text
    with ops_services.database.worker_session() as session:
        assert session.get_one(Customer, customer.id).dry_run is True


# --- settings, failed jobs, system --------------------------------------------------------------


def test_settings_are_validated_and_audited(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, csrf = signed_in
    page = client.get("/settings")
    assert "NOTICES_DRY_RUN" in page.text
    bad = client.post("/settings/visual_similarity_min", data={"value": "1.5", "csrf": csrf})
    assert "between 0 and 1" in bad.text
    client.post("/settings/visual_similarity_min", data={"value": "0.8", "csrf": csrf})
    client.post("/settings/sending_enabled", data={"value": "true", "csrf": csrf})  # not confirmed
    client.post("/settings/sending_enabled", data={"value": "true", "confirm": "yes", "csrf": csrf})
    client.post(
        "/settings/channel_daily_limits",
        data={"limit_hosting": "10", "limit_registrar": "5", "limit_google": "0", "csrf": csrf},
    )
    with ops_services.database.worker_session() as session:
        values = {row.key: row.value for row in session.scalars(select(Setting))}
        # Put the migration's defaults back: settings rows are kept between tests.
        for key, value in (("sending_enabled", False), ("visual_similarity_min", 0.85)):
            session.get_one(Setting, key).value = value
        session.get_one(Setting, "channel_daily_limits").value = values["channel_daily_limits"] | {
            "hosting": 50,
            "registrar": 20,
            "google": 50,
        }
    assert values["visual_similarity_min"] == 0.8
    assert values["sending_enabled"] is True
    assert values["channel_daily_limits"]["hosting"] == 10
    assert values["channel_daily_limits"]["google"] == 0
    # The kill switch is lifted for every customer.
    assert _outbox(ops_services.database, CLEAR_BLOCKED_CASES) == [{}]
    audit = _audit(ops_services.database, "setting.sending_enabled")
    assert [(row.actor, row.data) for row in audit] == [(EMAIL, {"old": False, "new": True})]


def _failed(database: Database, job: str, *, hours_ago: float = 3) -> Case:
    at = utcnow() - timedelta(hours=hours_ago)
    with database.worker_session() as session:
        customer = factories.customer(session)
        case = factories.case(session, customer, factories.brand(session, customer))
        session.add_all(
            [
                CaseEvent(
                    customer_id=customer.id,
                    case_id=case.id,
                    type=EventType.DETECTED,
                    actor="system",
                    at=at,
                ),
                CaseEvent(
                    customer_id=customer.id,
                    case_id=case.id,
                    type=EventType.JOB_FAILED,
                    actor="system",
                    at=at + timedelta(minutes=5),
                    data={"job": job, "error": "TimeoutError: boom"},
                ),
            ]
        )
    return case


def test_failed_jobs_are_listed_and_requeued(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, csrf = signed_in
    database = ops_services.database
    precheck = _failed(database, "http_precheck")
    signals = _failed(database, "compute_signals")
    capture = _failed(database, "browser_capture")
    with database.worker_session() as session:
        session.get_one(Case, capture.id).state = CaseState.IN_REVIEW
    _failed(database, "http_precheck", hours_ago=30)  # older than a day

    page = client.get("/jobs")
    assert page.text.count("TimeoutError: boom") == 3
    assert "take new evidence from the review queue" in page.text
    # Left alone by the reconciler until re-queued.
    assert reconciler.stuck_detected_cases(database, utcnow()) == []

    with database.worker_session() as session:
        events = {
            row.case_id: row.id
            for row in session.scalars(
                select(CaseEvent).where(CaseEvent.type == EventType.JOB_FAILED)
            )
        }
    for case in (precheck, signals):
        done = client.post(f"/jobs/{events[case.id]}/requeue", data={"csrf": csrf})
        assert "Re-queued" in done.text
    refused = client.post(f"/jobs/{events[capture.id]}/requeue", data={"csrf": csrf})
    assert "review queue" in refused.text

    assert _outbox(database, "http_precheck") == [{"case_id": str(precheck.id)}]
    assert _outbox(database, COMPUTE_SIGNALS) == [{"case_id": str(signals.id)}]
    assert client.get("/jobs").text.count("re-queued 20") == 2
    # The reconciler takes the case back into its rules.
    assert set(reconciler.stuck_detected_cases(database, utcnow())) == {
        str(precheck.id),
        str(signals.id),
    }
    assert len(_audit(database, "job.requeue")) == 2


def test_system_page_shows_queues_and_spend(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, _ = signed_in
    ops_services.redis.hset("dramatiq:signals.msgs", mapping={"a": "1", "b": "2"})
    today = utcnow().strftime("%Y%m%d")
    ops_services.redis.set(f"kalkan:proxy:bytes:{today}", str(3 * 1024**3))
    page = client.get("/system")
    assert re.search(r'<td class="mono">signals</td><td>2</td>', page.text)
    assert "3.00 GB" in page.text
    assert "No AI calls yet" in page.text


def test_clearing_blocked_cases(database: Database) -> None:
    with database.worker_session() as session:
        first = factories.customer(session)
        second = factories.customer(session)
        cases = []
        for owner in (first, second):
            brand = factories.brand(session, owner)
            case = factories.case(session, owner, brand, CaseState.APPROVED)
            case.blocked_reason = BlockedReason.NO_LOA
            cases.append(case)
        detected = factories.case(session, first, factories.brand(session, first))
        detected.blocked_reason = BlockedReason.DRY_RUN  # not approved: left as it is
    assert clear_blocked_cases(database, first.id) == 1
    assert clear_blocked_cases(database, None) == 1
    with database.worker_session() as session:
        reasons = {
            case.id: session.get_one(Case, case.id).blocked_reason for case in [*cases, detected]
        }
    assert reasons == {cases[0].id: None, cases[1].id: None, detected.id: BlockedReason.DRY_RUN}


def test_uploads_are_checked_by_content() -> None:
    from fastapi import UploadFile

    from kalkan.ops import uploads

    def upload(data: bytes, name: str = "x.png") -> UploadFile:
        return UploadFile(io.BytesIO(data), filename=name)

    found = uploads.read_upload(
        upload(SCREENSHOT, "../../Logo Light.png"), max_bytes=10**7, allowed=uploads.ASSET_TYPES
    )
    assert (found.content_type, found.extension, found.filename) == (
        "image/png",
        "png",
        "Logo-Light.png",
    )
    svg = uploads.read_upload(
        upload(b'<?xml version="1.0"?><svg xmlns="x"/>'), max_bytes=100, allowed=uploads.ASSET_TYPES
    )
    assert svg.extension == "svg"
    with pytest.raises(uploads.UploadError, match="larger"):
        uploads.read_upload(upload(SCREENSHOT), max_bytes=100, allowed=uploads.ASSET_TYPES)
    with pytest.raises(uploads.UploadError, match="unsupported"):
        uploads.read_upload(upload(b"%PDF-1.4"), max_bytes=100, allowed=uploads.ASSET_TYPES)
    with pytest.raises(uploads.UploadError, match="empty"):
        uploads.read_upload(upload(b""), max_bytes=100, allowed=uploads.ASSET_TYPES)
