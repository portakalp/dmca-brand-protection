"""The customer API's endpoints (401, 403 and another tenant's 404 for
every endpoint, file streams included; dry-run previews never reach the customer)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kalkan.config import Settings
from kalkan.db.models import (
    Artifact,
    Brand,
    BrandAsset,
    Case,
    CaseEvent,
    Customer,
    Domain,
    Invitation,
    JobOutbox,
    Notice,
    Report,
    Scan,
    Signal,
    UserSession,
    WhitelistEntry,
)
from kalkan.db.outbox import WHITELIST_CHANGED
from kalkan.db.session import Database
from kalkan.domain.case_state import transition
from kalkan.domain.enums import (
    ArtifactKind,
    BlockedReason,
    BrandAssetKind,
    CaseState,
    Channel,
    CustomerStatus,
    EventType,
    NoticeMethod,
    NoticeStatus,
    NoticeTemplate,
    Profile,
    ScanStage,
    UserRole,
    WhitelistSource,
)
from kalkan.enrichment.admission import apply_whitelist
from kalkan.security import hash_token
from kalkan.storage.gcs import ObjectStore
from tests import factories
from tests.integration.api_support import (
    Account,
    account,
    api,  # noqa: F401 - a fixture
    case_row,
    mailbox,  # noqa: F401 - a fixture
    sign_in,
)
from tests.integration.conftest import TEST_EVIDENCE_BUCKET, TEST_REPORTS_BUCKET

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@dataclass
class Ids:
    case: uuid.UUID
    artifact: uuid.UUID
    html: uuid.UUID
    asset: uuid.UUID
    entry: uuid.UUID
    user: uuid.UUID
    invitation: uuid.UUID
    report: uuid.UUID


def furnish(database: Database, store: ObjectStore, mint: Account) -> Ids:
    """A capture, a reference image, a whitelist entry, an invitation and a report."""
    with database.worker_session() as session:
        case = session.get_one(Case, mint.case_id)
        scan = Scan(
            customer_id=mint.customer_id,
            case_id=case.id,
            stage=ScanStage.BROWSER,
            profile=Profile.DESKTOP,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            outcome="ok",
        )
        session.add(scan)
        session.flush()
        files = {}
        for kind, data, content_type in (
            (ArtifactKind.SCREENSHOT, PNG, "image/png"),
            (ArtifactKind.HTML, b"<script>alert(1)</script>", "text/html; charset=utf-8"),
        ):
            key = store.put(
                TEST_EVIDENCE_BUCKET, f"cases/{case.id}/{scan.id}/{kind.value}-desktop", data
            ).key
            row = Artifact(
                customer_id=mint.customer_id,
                case_id=case.id,
                scan_id=scan.id,
                kind=kind,
                profile=Profile.DESKTOP,
                storage_key=key,
                sha256=hashlib.sha256(data).hexdigest(),
                bytes=len(data),
                content_type=content_type,
            )
            session.add(row)
            session.flush()
            files[kind] = row.id
        session.add(
            Signal(
                customer_id=mint.customer_id, case_id=case.id, name="visual_similarity", value=0.93
            )
        )
        asset_key = store.put(
            TEST_EVIDENCE_BUCKET, f"brands/{mint.brand_id}/refs/{uuid.uuid4()}.png", PNG
        ).key
        asset = BrandAsset(
            customer_id=mint.customer_id,
            brand_id=mint.brand_id,
            kind=BrandAssetKind.REFERENCE_DESKTOP,
            storage_key=asset_key,
            sha256="b" * 64,
        )
        entry = WhitelistEntry(
            customer_id=mint.customer_id,
            pattern=f"partner-{uuid.uuid4().hex[:6]}.example",
            kind="partner",
            source=WhitelistSource.MANUAL,
        )
        invitation = Invitation(
            customer_id=mint.customer_id,
            email=f"guest-{uuid.uuid4().hex[:6]}@customer.example",
            role=UserRole.VIEWER,
            token_hash=hash_token(uuid.uuid4().hex),
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        pdf_key = store.put(TEST_REPORTS_BUCKET, f"reports/{uuid.uuid4()}.pdf", b"%PDF-1.7").key
        report = Report(customer_id=mint.customer_id, period="2026-09", pdf_key=pdf_key)
        session.add_all([asset, entry, invitation, report])
        session.flush()
        return Ids(
            case=case.id,
            artifact=files[ArtifactKind.SCREENSHOT],
            html=files[ArtifactKind.HTML],
            asset=asset.id,
            entry=entry.id,
            user=mint.viewer.user_id,
            invitation=invitation.id,
            report=report.id,
        )


# (method, path, who may call it, body). "member": any signed-in user; "owner": owners;
# "writer": owners of an account that may write.
ENDPOINTS: list[tuple[str, str, str, object]] = [
    ("GET", "/api/v1/me", "member", None),
    ("GET", "/api/v1/overview", "member", None),
    ("GET", "/api/v1/brands", "member", None),
    ("GET", "/api/v1/threats", "member", None),
    ("GET", "/api/v1/threats/{case}", "member", None),
    ("GET", "/api/v1/threats/{case}/evidence.zip", "member", None),
    ("GET", "/api/v1/artifacts/{artifact}", "member", None),
    ("GET", "/api/v1/brand-assets/{asset}", "member", None),
    ("POST", "/api/v1/threats/{case}/mark-ours", "writer", {}),
    ("GET", "/api/v1/whitelist", "member", None),
    ("POST", "/api/v1/whitelist", "writer", {"pattern": "new-partner.example"}),
    ("DELETE", "/api/v1/whitelist/{entry}", "writer", None),
    ("GET", "/api/v1/whitelist/export", "owner", None),
    ("GET", "/api/v1/reports", "member", None),
    ("GET", "/api/v1/reports/{report}/download", "member", None),
    ("GET", "/api/v1/users", "owner", None),
    ("POST", "/api/v1/users", "writer", {"email": "someone@customer.example"}),
    ("DELETE", "/api/v1/users/{user}", "writer", None),
    ("DELETE", "/api/v1/users/invitations/{invitation}", "writer", None),
    ("GET", "/api/v1/settings", "owner", None),
    ("PATCH", "/api/v1/settings", "writer", {"timezone": "Europe/Warsaw"}),
]
OWNED = [row for row in ENDPOINTS if "{" in row[1]]


def _path(template: str, ids: Ids) -> str:
    return template.format(**{name: getattr(ids, name) for name in Ids.__dataclass_fields__})


@pytest.fixture
def tenants(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> tuple[Account, Ids, Account, Ids]:
    mint = account(database, test_settings)
    other = account(database, test_settings)
    return (
        mint,
        furnish(database, object_store, mint),
        other,
        furnish(database, object_store, other),
    )


@pytest.mark.parametrize(("method", "template", "who", "body"), ENDPOINTS)
def test_without_a_session_every_endpoint_is_401(
    api: TestClient,  # noqa: F811
    tenants: tuple[Account, Ids, Account, Ids],
    method: str,
    template: str,
    who: str,
    body: object,
) -> None:
    _, ids, _, _ = tenants
    response = api.request(method, _path(template, ids), json=body)
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "template", "who", "body"), [row for row in ENDPOINTS if row[2] != "member"]
)
def test_a_viewer_gets_403_on_owner_endpoints(
    api: TestClient,  # noqa: F811
    tenants: tuple[Account, Ids, Account, Ids],
    method: str,
    template: str,
    who: str,
    body: object,
) -> None:
    mint, ids, _, _ = tenants
    sign_in(api, mint.viewer)
    response = api.request(method, _path(template, ids), json=body)
    assert response.status_code == 403
    assert response.json()["code"] == "role"


@pytest.mark.parametrize(("method", "template", "who", "body"), OWNED)
def test_another_tenants_records_are_404(
    api: TestClient,  # noqa: F811
    tenants: tuple[Account, Ids, Account, Ids],
    method: str,
    template: str,
    who: str,
    body: object,
) -> None:
    mint, ids, _, foreign = tenants
    sign_in(api, mint.owner)
    assert api.request(method, _path(template, foreign), json=body).status_code == 404
    if method == "GET":
        assert api.request(method, _path(template, ids), json=body).status_code == 200


@pytest.mark.parametrize("change", [{"status": CustomerStatus.SUSPENDED}, {"is_demo": True}])
def test_read_only_and_demo_accounts_cannot_write(
    api: TestClient,  # noqa: F811
    tenants: tuple[Account, Ids, Account, Ids],
    database: Database,
    change: dict[str, object],
) -> None:
    mint, ids, _, _ = tenants
    with database.worker_session() as session:
        owner = session.get_one(Customer, mint.customer_id)
        for name, value in change.items():
            setattr(owner, name, value)
    sign_in(api, mint.owner)
    for method, template, who, body in ENDPOINTS:
        response = api.request(method, _path(template, ids), json=body)
        if who == "writer":
            assert response.status_code == 403, template
            assert response.json()["code"] == "read_only"
        else:
            assert response.status_code == 200, template
    assert api.get("/api/v1/me").json()["permissions"]["write_whitelist"] is False


# --- threats ---------------------------------------------------------------------------------


def _previewed_case(database: Database, mint: Account) -> uuid.UUID:
    """An approved case whose only notice is a dry-run preview."""
    with database.worker_session() as session:
        owner = session.get_one(Customer, mint.customer_id)
        brand = session.get_one(Brand, mint.brand_id)
        case = factories.case(session, owner, brand)
        transition(session, case, CaseState.VERIFIED, actor="t")
        transition(session, case, CaseState.APPROVED, actor="t")
        case.blocked_reason = BlockedReason.DRY_RUN
        session.add(
            Notice(
                customer_id=owner.id,
                case_id=case.id,
                channel=Channel.REGISTRAR,
                method=NoticeMethod.EMAIL,
                recipient="abuse@registrar.example",
                template=NoticeTemplate.PHISHING,
                language="en",
                status=NoticeStatus.BLOCKED,
                blocked_reason=BlockedReason.DRY_RUN,
                dry_run=True,
                provider_message_id="<preview@example.test>",
            )
        )
        session.add(
            CaseEvent(
                customer_id=owner.id,
                case_id=case.id,
                type=EventType.REPLY_RECEIVED,
                actor="system",
                data={"classification": "removed", "dry_run": True},
            )
        )
        return case.id


def test_a_previewed_case_is_active_and_its_preview_is_hidden(
    api: TestClient,  # noqa: F811
    tenants: tuple[Account, Ids, Account, Ids],
    database: Database,
) -> None:
    mint, _, _, _ = tenants
    case_id = _previewed_case(database, mint)
    sign_in(api, mint.owner)
    detail = api.get(f"/api/v1/threats/{case_id}").json()
    assert detail["threat"]["status"] == "active"
    assert detail["notices"] == []
    assert all(event["type"] != "reply_received" for event in detail["timeline"])
    listed = api.get("/api/v1/threats", params={"status": "active"}).json()
    assert case_id in {uuid.UUID(item["id"]) for item in listed["items"]}
    overview = api.get("/api/v1/overview").json()
    assert overview["status_distribution"]["active"] == 2
    assert overview["live_threats"] == 2
    body = json.dumps(detail) + json.dumps(listed) + json.dumps(overview)
    assert "preview@example.test" not in body
    assert "dry_run" not in body


def test_a_notified_case_shows_its_next_check_and_escalation(
    api: TestClient,  # noqa: F811
    tenants: tuple[Account, Ids, Account, Ids],
    database: Database,
) -> None:
    mint, _, _, _ = tenants
    sent = datetime(2026, 10, 8, 10, 5, tzinfo=UTC)
    with database.worker_session() as session:
        owner = session.get_one(Customer, mint.customer_id)
        case = factories.case(session, owner, session.get_one(Brand, mint.brand_id))
        for state in (CaseState.VERIFIED, CaseState.APPROVED, CaseState.NOTIFIED):
            transition(session, case, state, actor="t")
        case.next_check_at = sent + timedelta(hours=6)
        session.add(
            Notice(
                customer_id=owner.id,
                case_id=case.id,
                channel=Channel.HOSTING,
                method=NoticeMethod.EMAIL,
                recipient="abuse@host.example",
                template=NoticeTemplate.PHISHING,
                language="en",
                status=NoticeStatus.SENT,
                dry_run=False,
                sent_at=sent,
            )
        )
        case_id = case.id
    sign_in(api, mint.owner)
    steps = api.get(f"/api/v1/threats/{case_id}").json()["next_steps"]
    assert steps == {
        "next_check_at": "2026-10-08T16:05:00Z",
        "escalation_at": "2026-10-10T10:05:00Z",
        "escalation_channel": "registry",
    }


def test_the_threat_list_filters_pages_and_exports(
    api: TestClient,  # noqa: F811
    tenants: tuple[Account, Ids, Account, Ids],
    database: Database,
) -> None:
    mint, _, _, _ = tenants
    _previewed_case(database, mint)
    hidden = _previewed_case(database, mint)
    with database.worker_session() as session:
        case = session.get_one(Case, hidden)
        transition(session, case, CaseState.DISMISSED, actor="t", event=EventType.WHITELISTED)
        domain = session.get_one(Domain, case_row(database, mint.case_id).domain_id)
        domain.name = "=cmd-mintera.example"
    sign_in(api, mint.owner)
    everything = api.get("/api/v1/threats").json()
    assert everything["total"] == 2  # the whitelisted case is no threat
    first = api.get("/api/v1/threats", params={"page_size": 1}).json()
    second = api.get("/api/v1/threats", params={"page_size": 1, "page": 2}).json()
    assert len(first["items"]) == len(second["items"]) == 1
    assert first["items"][0]["id"] != second["items"][0]["id"]
    found = api.get("/api/v1/threats", params={"q": "CMD-mint"}).json()
    assert [item["domain"] for item in found["items"]] == ["=cmd-mintera.example"]
    assert api.get("/api/v1/threats", params={"q": "%"}).json()["total"] == 0
    assert api.get("/api/v1/threats", params={"category": "phishing_clone"}).json()["total"] == 1
    assert api.get("/api/v1/threats", params={"status": "taken_down"}).json()["total"] == 0
    exported = api.get("/api/v1/threats", params={"format": "csv"})
    assert exported.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(exported.text)))
    assert rows[0][:3] == ["number", "domain", "status"]
    assert "'=cmd-mintera.example" in {row[1] for row in rows[1:]}


def test_the_threat_detail_and_its_files(
    api: TestClient,  # noqa: F811
    tenants: tuple[Account, Ids, Account, Ids],
) -> None:
    mint, ids, _, _ = tenants
    sign_in(api, mint.owner)
    detail = api.get(f"/api/v1/threats/{ids.case}").json()
    assert detail["comparison"]["visual_similarity"] == 0.93
    assert detail["comparison"]["clone"] == [
        {"profile": "desktop", "url": f"/api/v1/artifacts/{ids.artifact}"}
    ]
    assert detail["comparison"]["official"][0]["url"] == f"/api/v1/brand-assets/{ids.asset}"
    assert detail["infrastructure"]["hosting"] == "Hosting Ltd"
    assert [event["type"] for event in detail["timeline"]] == ["verified"]
    assert {item["kind"] for item in detail["files"]} == {"screenshot", "html"}
    assert detail["next_steps"] == {
        "next_check_at": None,
        "escalation_at": None,
        "escalation_channel": None,
    }

    image = api.get(f"/api/v1/artifacts/{ids.artifact}")
    assert image.status_code == 200
    assert f'filename="screenshot-desktop-{ids.artifact}.' in image.headers["content-disposition"]
    assert image.headers["content-type"] == "image/png"
    assert image.headers["content-security-policy"].startswith("sandbox")
    assert image.headers["cache-control"] == "private, max-age=300"
    page = api.get(f"/api/v1/artifacts/{ids.html}")
    assert page.headers["content-type"] == "application/octet-stream"
    assert page.headers["content-disposition"].startswith("attachment")

    archive = api.get(f"/api/v1/threats/{ids.case}/evidence.zip")
    assert archive.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as opened:
        manifest = json.loads(opened.read("manifest.json"))
        names = set(opened.namelist())
        shot = next(f for f in manifest["files"] if f["kind"] == "screenshot")
        assert opened.read(shot["name"]) == PNG
    assert len(manifest["files"]) == 2
    assert names == {f["name"] for f in manifest["files"]} | {"manifest.json"}
    assert shot["sha256"] == hashlib.sha256(PNG).hexdigest()


def test_this_is_our_site_whitelists_dismisses_and_queues_the_retraction(
    api: TestClient,  # noqa: F811
    tenants: tuple[Account, Ids, Account, Ids],
    database: Database,
) -> None:
    mint, ids, _, _ = tenants
    sign_in(api, mint.owner)
    marked = api.post(f"/api/v1/threats/{ids.case}/mark-ours", json={"kind": "mirror"})
    assert marked.status_code == 202, marked.text
    entry = marked.json()["whitelist_entry"]
    assert (entry["kind"], entry["source"]) == ("mirror", "this_is_ours")
    with database.worker_session() as session:
        jobs = session.scalars(select(JobOutbox).where(JobOutbox.kind == WHITELIST_CHANGED)).all()
        assert [job.payload for job in jobs] == [{"customer_id": str(mint.customer_id)}]
        dismissed = apply_whitelist(session, mint.customer_id, now=datetime.now(UTC))
    assert dismissed == [ids.case]
    assert case_row(database, ids.case).state is CaseState.DISMISSED
    # A second click finds the same entry.
    again = api.post(f"/api/v1/threats/{ids.case}/mark-ours", json={})
    assert again.json()["whitelist_entry"]["id"] == entry["id"]


# --- whitelist, users, settings, reports -------------------------------------------------------


def test_whitelist_add_delete_import_and_export(
    api: TestClient,  # noqa: F811
    tenants: tuple[Account, Ids, Account, Ids],
    database: Database,
) -> None:
    mint, ids, _, _ = tenants
    sign_in(api, mint.owner)
    assert api.post("/api/v1/whitelist", json={"pattern": "github.io"}).status_code == 422
    added = api.post("/api/v1/whitelist", json={"pattern": "WWW.Mint-Partner.example"})
    assert added.status_code == 201
    assert added.json()["pattern"] == "mint-partner.example"
    assert (
        api.post("/api/v1/whitelist", json={"pattern": "mint-partner.example"}).status_code == 409
    )
    assert api.delete(f"/api/v1/whitelist/{ids.entry}").status_code == 204
    upload = (
        "pattern,kind,note,brand\n"
        "affiliate-one.example,affiliate,,Mintera Bank\n"
        "com,other,,\n"
        "mint-partner.example,,,\n"
        "two.example,nonsense,,\n"
        "three.example,social,=HYPERLINK(1),Unknown brand\n"
        "four.example,social,=HYPERLINK(1),\n"
    )
    preview = api.post(
        "/api/v1/whitelist/import",
        params={"dry_run": "true"},
        files={"file": ("list.csv", upload, "text/csv")},
    ).json()
    before = {item["pattern"] for item in api.get("/api/v1/whitelist").json()}
    assert "affiliate-one.example" not in before
    imported = api.post(
        "/api/v1/whitelist/import", files={"file": ("list.csv", upload, "text/csv")}
    ).json()
    assert preview == imported
    assert (imported["added"], imported["skipped"]) == (2, 1)
    assert [(e["line"], e["error"]) for e in imported["errors"]] == [
        (3, imported["errors"][0]["error"]),
        (5, "unknown kind"),
        (6, "unknown brand"),
    ]
    exported = api.get("/api/v1/whitelist/export").text
    assert "'=HYPERLINK(1)" in exported
    assert "affiliate-one.example,affiliate,,Mintera Bank" in exported
    patterns = {item["pattern"] for item in api.get("/api/v1/whitelist").json()}
    assert {"mint-partner.example", "affiliate-one.example", "four.example"} <= patterns
    with database.worker_session() as session:
        assert session.get(WhitelistEntry, ids.entry) is None


def test_users_settings_and_reports(
    api: TestClient,  # noqa: F811
    tenants: tuple[Account, Ids, Account, Ids],
    database: Database,
    test_settings: Settings,
) -> None:
    mint, ids, _, _ = tenants
    viewer = TestClient(api.app, headers={"X-Kalkan-CSRF": "1"})
    sign_in(viewer, mint.viewer)
    sign_in(api, mint.owner)
    listed = api.get("/api/v1/users").json()
    assert {u["email"] for u in listed["users"]} == {mint.owner.email, mint.viewer.email}
    assert len(listed["invitations"]) == 1
    assert api.delete(f"/api/v1/users/{mint.owner.user_id}").status_code == 409
    assert api.delete(f"/api/v1/users/{mint.viewer.user_id}").status_code == 204
    assert viewer.get("/api/v1/me").status_code == 401
    with database.worker_session() as session:
        assert (
            session.scalars(
                select(UserSession).where(UserSession.user_id == mint.viewer.user_id)
            ).all()
            == []
        )
    assert api.delete(f"/api/v1/users/invitations/{ids.invitation}").status_code == 204

    assert api.patch("/api/v1/settings", json={"timezone": "Mars/Base"}).status_code == 422
    changed = api.patch(
        "/api/v1/settings", json={"timezone": "Europe/Warsaw", "alert_email": "Alerts@Mint.example"}
    ).json()
    assert changed == {"alert_email": "alerts@mint.example", "timezone": "Europe/Warsaw"}

    reports = api.get("/api/v1/reports").json()
    assert [(r["period"], r["pdf"], r["csv"]) for r in reports] == [("2026-09", True, False)]
    pdf = api.get(f"/api/v1/reports/{ids.report}/download")
    assert pdf.status_code == 200
    assert pdf.content == b"%PDF-1.7"
    assert (
        api.get(f"/api/v1/reports/{ids.report}/download", params={"format": "csv"}).status_code
        == 404
    )

    # The print page's data: only the single-use report token, no session.
    token = "print-token-" + uuid.uuid4().hex
    with database.worker_session() as session:
        report = session.get_one(Report, ids.report)
        report.print_token_hash = hash_token(token)
        report.print_token_expires_at = datetime.now(UTC) + timedelta(minutes=60)
    anonymous = TestClient(api.app)
    path = f"/api/v1/reports/{ids.report}/data"
    assert anonymous.get(path).status_code == 401
    data = anonymous.get(path, headers={"X-Report-Token": token})
    assert data.status_code == 200, data.text
    assert data.json()["overview"]["period"] == {"start": "2026-09-01", "end": "2026-09-30"}
    assert anonymous.get(path, headers={"X-Report-Token": token}).status_code == 401


def test_the_api_never_opens_a_worker_connection(
    api: TestClient,  # noqa: F811
    tenants: tuple[Account, Ids, Account, Ids],
) -> None:
    mint, ids, _, _ = tenants
    sign_in(api, mint.owner)
    for method, template, _, _ in ENDPOINTS:
        if method == "GET":
            api.get(_path(template, ids))
    database = api.app.state.database  # type: ignore[attr-defined]
    assert "worker_engine" not in database.__dict__
    assert "api_engine" in database.__dict__
