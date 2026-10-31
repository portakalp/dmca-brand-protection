"""The notice engine against the database, the storage emulator and Mailpit: gates, dry-run,
the real wave after dry-run, retraction, the evidence page."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from kalkan.api.app import create_app
from kalkan.config import Settings
from kalkan.db.models import (
    Case,
    CaseEvent,
    Customer,
    Domain,
    EvidenceLink,
    JobOutbox,
    Notice,
    ReviewTask,
    Verdict,
)
from kalkan.db.outbox import AI_NOTICE_TEXT, PLAN_NOTICES, SEND_NOTICE
from kalkan.db.session import Database
from kalkan.db.settings_store import write_setting
from kalkan.domain import case_state
from kalkan.domain.enums import (
    BlockedReason,
    CaseState,
    Category,
    Channel,
    CustomerStatus,
    EventType,
    NoticeBasis,
    NoticeStatus,
    TaskKind,
    TaskStatus,
    VerdictSource,
)
from kalkan.email.provider import Email, Sent, SmtpProvider
from kalkan.notices import render
from kalkan.notices.alert import send_customer_alert
from kalkan.notices.blocks import clear_blocked_cases
from kalkan.notices.planning import plan_notices
from kalkan.notices.retraction import request_retraction
from kalkan.notices.sender import send_notice
from kalkan.notices.text import write_explanation
from kalkan.storage.gcs import ObjectStore
from tests.integration.test_decision import clone_case

REDIRECT = "operator@example.test"
REGISTRAR = "abuse@registrar.example"
HOSTING = "abuse@host.example"
MAILPIT = "http://localhost:8025/api/v1"


@dataclass
class FakeProvider:
    sent: list[Email] = field(default_factory=list)

    def send(self, email: Email) -> Sent:
        self.sent.append(email)
        return Sent(f"<{uuid.uuid4().hex}@example.test>", None)


def notice_settings(settings: Settings, **values: Any) -> Settings:
    update: dict[str, Any] = {
        "notices_dry_run": True,
        "dry_run_redirect_email": REDIRECT,
        "mail_from": "abuse-reports@example.test",
        "reply_domain": "reply.example.test",
        "evidence_base_url": "http://evidence.localhost",
        "smtp_url": "smtp://localhost:2525",
    }
    update.update(values)
    # model_copy does not validate: secrets are wrapped as the settings loader would.
    if isinstance(update.get("smtp_url"), str):
        update["smtp_url"] = SecretStr(update["smtp_url"])
    return settings.model_copy(update=update)


def approved_case(
    database: Database,
    store: ObjectStore,
    settings: Settings,
    *,
    basis: NoticeBasis = NoticeBasis.PHISHING,
    **customer: Any,
) -> Case:
    """A captured clone, approved on ``basis``, with registrar and host abuse addresses."""
    case = clone_case(database, store, settings)
    with database.worker_session() as session:
        owner = session.get_one(Customer, case.customer_id)
        owner.sending_enabled = True
        for name, value in customer.items():
            setattr(owner, name, value)
        domain = session.get_one(Domain, case.domain_id)
        domain.name = f"mintera-{uuid.uuid4().hex[:6]}.online"
        domain.tld = "online"
        domain.registrar_iana_id = 1068
        domain.registrar_abuse_email = REGISTRAR
        domain.hosting_abuse_email = HOSTING
        domain.hosting_country = "PL"
        domain.asn = 64500
        row = session.get_one(Case, case.id)
        row.category = Category.PHISHING_CLONE
        session.add(
            Verdict(
                customer_id=case.customer_id,
                case_id=case.id,
                source=VerdictSource.STAFF,
                category=Category.PHISHING_CLONE,
                confidence=Decimal("1.000"),
                copied_elements=["logo", "layout"],
                collects_credentials_or_payment=True,
                evidence=[],
                suggested_basis=basis,
                short_explanation="Operator decision.",
            )
        )
        case_state.transition(session, row, CaseState.VERIFIED, actor="test")
        case_state.transition(session, row, CaseState.APPROVED, actor="test")
    return case


def drain(database: Database, settings: Settings, provider: Any) -> None:
    """The scheduler and the workers in one loop: plan, text (no AI: fixed paragraph), send."""
    now = datetime.now(UTC)
    for _ in range(10):
        with database.worker_session() as session:
            jobs = session.scalars(
                select(JobOutbox)
                .where(
                    JobOutbox.dispatched_at.is_(None),
                    JobOutbox.kind.in_([PLAN_NOTICES, AI_NOTICE_TEXT, SEND_NOTICE]),
                )
                .order_by(JobOutbox.created_at)
            ).all()
            for job in jobs:
                job.dispatched_at = now
            work = [(job.kind, dict(job.payload)) for job in jobs]
        if not work:
            return
        for kind, payload in work:
            if kind == PLAN_NOTICES:
                plan_notices(database, settings, uuid.UUID(payload["case_id"]), now=now)
            elif kind == AI_NOTICE_TEXT:
                result = write_explanation(
                    database, None, settings, uuid.UUID(payload["notice_id"]), now=now
                )
                if result is not None and result.queued:
                    send_notice(database, settings, lambda: provider, result.notice_id, now=now)
            else:
                send_notice(
                    database, settings, lambda: provider, uuid.UUID(payload["notice_id"]), now=now
                )


def plan_and_send(database: Database, settings: Settings, provider: Any, case: Case) -> None:
    plan_notices(database, settings, case.id, now=datetime.now(UTC))
    drain(database, settings, provider)


def notices_of(database: Database, case_id: uuid.UUID) -> list[Notice]:
    with database.worker_session() as session:
        rows = session.scalars(
            select(Notice).where(Notice.case_id == case_id).order_by(Notice.channel, Notice.dry_run)
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def case_row(database: Database, case_id: uuid.UUID) -> Case:
    with database.worker_session() as session:
        found = session.get_one(Case, case_id)
        session.expunge(found)
        return found


@pytest.fixture
def unflagged_legal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """The legal texts as if the lawyer had approved them."""
    for path in render.LEGAL_DIR.glob("*.txt"):
        lines = path.read_text(encoding="utf-8").splitlines()
        (tmp_path / path.name).write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
    monkeypatch.setattr(render, "LEGAL_DIR", tmp_path)
    render.legal_text.cache_clear()
    yield
    render.legal_text.cache_clear()


# --- dry-run ----------------------------------------------------------------------------------


def test_a_dry_run_wave_goes_only_to_the_redirect_mailbox(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    settings = notice_settings(test_settings)
    provider = FakeProvider()
    case = approved_case(database, object_store, settings)
    plan_and_send(database, settings, provider, case)

    assert [email.to for email in provider.sent] == [REDIRECT, REDIRECT, REDIRECT]
    assert all(email.subject.startswith("[DRY RUN] [") for email in provider.sent)
    tops = sorted(email.text.split("\n")[1] for email in provider.sent)
    assert tops == [
        f"Real recipient: {HOSTING}",
        f"Real recipient: {REGISTRAR}",
        "Real recipient: https://safebrowsing.google.com/safebrowsing/report_phish/",
    ]
    notices = notices_of(database, case.id)
    assert {(n.channel, n.status, n.blocked_reason, n.dry_run) for n in notices} == {
        (channel, NoticeStatus.BLOCKED, BlockedReason.DRY_RUN, True)
        for channel in (Channel.REGISTRAR, Channel.HOSTING, Channel.GOOGLE)
    }
    assert all(n.provider_message_id for n in notices)
    assert all(n.explanation_source and n.explanation_source.value == "fallback" for n in notices)
    found = case_row(database, case.id)
    assert (found.state, found.blocked_reason, found.first_notice_at) == (
        CaseState.APPROVED,
        BlockedReason.DRY_RUN,
        None,
    )
    with database.worker_session() as session:
        task = session.scalars(select(ReviewTask).where(ReviewTask.case_id == case.id)).one()
    assert (task.kind, task.status) == (TaskKind.MANUAL_CHANNEL, TaskStatus.OPEN)
    assert (task.note or "").startswith("[DRY RUN]")

    # Planning again sends no second preview.
    plan_and_send(database, settings, provider, case)
    assert len(provider.sent) == 3
    assert len(notices_of(database, case.id)) == 3


# --- gates ------------------------------------------------------------------------------------


GATES = [
    ("monitor_only", {"monitor_only": True}, {}, None),
    ("sending_disabled", {"sending_enabled": False}, {}, None),
    ("suspended", {"status": CustomerStatus.SUSPENDED}, {}, None),
    ("kill_switch", {"dry_run": False}, {"notices_dry_run": False}, None),
    (
        "daily_limit",
        {"dry_run": False, "loa_signed_at": datetime.now(UTC)},
        {"notices_dry_run": False},
        0,
    ),
    ("no_loa", {"dry_run": False}, {"notices_dry_run": False}, 50),
    (
        "legal_review",
        {"dry_run": False, "loa_signed_at": datetime.now(UTC)},
        {"notices_dry_run": False},
        50,
    ),
]


@pytest.mark.parametrize(("reason", "customer", "env", "limit"), GATES, ids=[g[0] for g in GATES])
def test_each_gate_blocks_with_its_reason(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    reason: str,
    customer: dict[str, Any],
    env: dict[str, Any],
    limit: int | None,
) -> None:
    settings = notice_settings(test_settings, **env)
    provider = FakeProvider()
    if limit is not None:
        with database.worker_session() as session:
            write_setting(session, "sending_enabled", True, actor="test")
            limits = dict.fromkeys(("hosting", "registrar", "registry", "google"), limit)
            write_setting(session, "channel_daily_limits", limits, actor="test")
    case = approved_case(database, object_store, settings, **customer)
    plan_and_send(database, settings, provider, case)

    assert provider.sent == []
    found = case_row(database, case.id)
    assert found.state is CaseState.APPROVED
    assert found.blocked_reason == BlockedReason(reason)
    notices = notices_of(database, case.id)
    if reason == "monitor_only":
        assert notices == []  # no notice at all, not even a preview
    else:
        assert {n.blocked_reason for n in notices} == {BlockedReason(reason)}
        assert {n.status for n in notices} == {NoticeStatus.BLOCKED}


# --- the real wave after dry-run ----------------------------------------------------------------


def test_turning_dry_run_off_sends_a_real_wave_next_to_the_previews(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    unflagged_legal: None,
) -> None:
    preview_settings = notice_settings(test_settings)
    provider = FakeProvider()
    case = approved_case(database, object_store, preview_settings)
    plan_and_send(database, preview_settings, provider, case)
    previews = len(provider.sent)

    real_settings = notice_settings(test_settings, notices_dry_run=False)
    with database.worker_session() as session:
        owner = session.get_one(Customer, case.customer_id)
        owner.dry_run = False
        owner.loa_signed_at = datetime.now(UTC)
        write_setting(session, "sending_enabled", True, actor="test")
    assert clear_blocked_cases(database, case.customer_id) == 1
    before = datetime.now(UTC)
    drain(database, real_settings, provider)

    real = provider.sent[previews:]
    assert sorted(email.to for email in real) == [HOSTING, REGISTRAR]
    assert all(not email.subject.startswith("[DRY RUN]") for email in real)
    assert "3.18.2" in next(email.text for email in real if email.to == REGISTRAR)
    notices = notices_of(database, case.id)
    assert len(notices) == 6  # three previews and three real notices: the key includes dry_run
    real_rows = {n.channel: n for n in notices if not n.dry_run}
    assert real_rows[Channel.REGISTRAR].status is NoticeStatus.SENT
    assert real_rows[Channel.HOSTING].language.value == "pl"
    assert real_rows[Channel.GOOGLE].status is NoticeStatus.MANUAL_PENDING
    found = case_row(database, case.id)
    assert found.state is CaseState.NOTIFIED
    assert found.first_notice_at is not None
    assert found.next_check_at is not None
    assert timedelta(hours=6) <= found.next_check_at - before < timedelta(hours=6, minutes=5)
    with database.worker_session() as session:
        tasks = {
            (task.kind, task.status, (task.note or "").startswith("[DRY RUN]"))
            for task in session.scalars(select(ReviewTask).where(ReviewTask.case_id == case.id))
        }
        events = [
            event.type
            for event in session.scalars(
                select(CaseEvent).where(CaseEvent.case_id == case.id).order_by(CaseEvent.at)
            )
        ]
    assert tasks == {
        (TaskKind.MANUAL_CHANNEL, TaskStatus.REJECTED, True),
        (TaskKind.MANUAL_CHANNEL, TaskStatus.OPEN, False),
    }
    assert events.count(EventType.NOTICE_SENT) == 2


def test_retraction_goes_to_the_same_recipients(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    unflagged_legal: None,
) -> None:
    settings = notice_settings(test_settings, notices_dry_run=False)
    with database.worker_session() as session:
        write_setting(session, "sending_enabled", True, actor="test")
    provider = FakeProvider()
    case = approved_case(
        database, object_store, settings, dry_run=False, loa_signed_at=datetime.now(UTC)
    )
    plan_and_send(database, settings, provider, case)
    first = len(provider.sent)
    with database.worker_session() as session:
        row = session.get_one(Case, case.id, with_for_update=True)
        case_state.transition(session, row, CaseState.DISMISSED, actor="whitelist")
        assert request_retraction(session, row, settings) == 3
    drain(database, settings, provider)

    retractions = provider.sent[first:]
    assert sorted(email.to for email in retractions) == [HOSTING, REGISTRAR]
    assert all("Withdrawal of report" in email.subject for email in retractions)
    by_template = {(n.channel, n.template.value): n.status for n in notices_of(database, case.id)}
    assert by_template[(Channel.REGISTRAR, "phishing")] is NoticeStatus.RETRACTED
    assert by_template[(Channel.REGISTRAR, "retraction")] is NoticeStatus.SENT
    with database.worker_session() as session:
        kinds = {
            task.kind
            for task in session.scalars(
                select(ReviewTask).where(
                    ReviewTask.case_id == case.id, ReviewTask.status == TaskStatus.OPEN
                )
            )
        }
    assert TaskKind.RETRACTION in kinds  # the web-form report is retracted by hand


def test_a_closed_case_gets_no_notice(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    settings = notice_settings(test_settings)
    provider = FakeProvider()
    case = approved_case(database, object_store, settings)
    plan_notices(database, settings, case.id, now=datetime.now(UTC))
    with database.worker_session() as session:
        row = session.get_one(Case, case.id)
        case_state.transition(session, row, CaseState.DISMISSED, actor="whitelist")
    drain(database, settings, provider)
    assert provider.sent == []
    assert {n.status for n in notices_of(database, case.id)} == {NoticeStatus.REJECTED}


def test_the_customer_alert_goes_once(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    settings = notice_settings(test_settings)
    provider = FakeProvider()
    case = approved_case(database, object_store, settings, alert_email="alerts@customer.example")
    now = datetime.now(UTC)
    assert send_customer_alert(database, settings, lambda: provider, case.id, now=now)
    assert not send_customer_alert(database, settings, lambda: provider, case.id, now=now)
    [email] = provider.sent
    assert email.to == REDIRECT  # the customer is still in dry-run
    assert email.subject.startswith("[DRY RUN] [")
    assert "alerts@customer.example" in email.text


def test_the_reconciler_plans_forgotten_approved_cases(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    from kalkan.scheduler import reconciler

    settings = notice_settings(test_settings)
    case = approved_case(database, object_store, settings)
    later = datetime.now(UTC) + timedelta(hours=2)
    sent: list[tuple[str, dict[str, str]]] = []
    result = reconciler.reconcile(database, lambda actor, **kw: sent.append((actor, kw)), later)
    assert ("plan_notices", {"case_id": str(case.id)}) in sent
    assert result.plans == 1
    # Once the case is blocked by dry-run it is the gate's, not the reconciler's.
    plan_and_send(database, settings, FakeProvider(), case)
    sent.clear()
    reconciler.reconcile(database, lambda actor, **kw: sent.append((actor, kw)), later)
    assert ("plan_notices", {"case_id": str(case.id)}) not in sent


# --- the evidence page ------------------------------------------------------------------------


@pytest.fixture
def api(test_settings: Settings) -> Iterator[TestClient]:
    app = create_app(test_settings.model_copy(update={"edge_shared_secret": None}))
    with TestClient(app) as client:
        yield client


def _evidence_path(body: str) -> str:
    found = re.search(r"http://evidence\.localhost(/e/[A-Za-z0-9_-]+)", body)
    assert found is not None, "no evidence link in the notice"
    return found.group(1)


def test_the_evidence_link_opens_its_case_only(
    database: Database, object_store: ObjectStore, test_settings: Settings, api: TestClient
) -> None:
    settings = notice_settings(test_settings)
    provider = FakeProvider()
    case = approved_case(database, object_store, settings)
    other = approved_case(database, object_store, settings)
    plan_and_send(database, settings, provider, case)
    path = _evidence_path(provider.sent[0].text)

    page = api.get(path)
    assert page.status_code == 200
    assert page.headers["x-robots-tag"].startswith("noindex")
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert case.number in page.text
    assert '<meta name="robots" content="noindex' in page.text
    images = re.findall(r'<img src="([^"]+)"', page.text)
    assert images
    shot = api.get(images[0])
    assert shot.status_code == 200
    assert shot.headers["content-type"] == "image/png"
    assert shot.content.startswith(b"\x89PNG")
    with database.worker_session() as session:
        views = session.scalars(select(EvidenceLink.views).where(EvidenceLink.case_id == case.id))
        assert max(views) >= 1
        foreign = session.scalars(select(Notice.id).where(Notice.case_id == other.id)).all()
        from kalkan.db.models import Artifact

        other_file = session.scalars(
            select(Artifact.id).where(Artifact.case_id == other.id)
        ).first()
    assert foreign == []
    assert api.get(f"{path}/files/{other_file}").status_code == 404
    assert api.get("/e/not-a-token").status_code == 404


def test_the_evidence_page_is_rate_limited(api: TestClient) -> None:
    api.app.state.evidence_limiter.limit = 3  # type: ignore[attr-defined]
    codes = [
        api.get("/e/whatever", headers={"x-forwarded-for": "198.51.100.7"}).status_code
        for _ in range(4)
    ]
    assert codes == [404, 404, 404, 429]
    assert api.get("/e/whatever", headers={"x-forwarded-for": "198.51.100.8"}).status_code == 404


# --- end to end through Mailpit -------------------------------------------------------------------


def test_end_to_end_the_preview_lands_in_mailpit_and_its_evidence_opens(
    database: Database, object_store: ObjectStore, test_settings: Settings, api: TestClient
) -> None:
    settings = notice_settings(test_settings)
    marker = uuid.uuid4().hex[:8]
    with httpx2.Client() as http:
        http.delete(f"{MAILPIT}/messages")
        case = approved_case(database, object_store, settings)
        with database.worker_session() as session:
            session.get_one(Customer, case.customer_id).name = f"Renamed {marker}"
        provider = SmtpProvider("smtp://localhost:2525", "abuse-reports@example.test")
        plan_and_send(database, settings, provider, case)
        listed = http.get(f"{MAILPIT}/search", params={"query": case.number}).json()
        assert listed["messages_count"] == 3
        message = listed["messages"][0]
        assert message["Subject"].startswith("[DRY RUN] [")
        assert [to["Address"] for to in message["To"]] == [REDIRECT]
        text = http.get(f"{MAILPIT}/message/{message['ID']}").json()["Text"]
    page = api.get(_evidence_path(text))
    assert page.status_code == 200
    for src in re.findall(r'<img src="([^"]+)"', page.text):
        assert api.get(src).status_code == 200
