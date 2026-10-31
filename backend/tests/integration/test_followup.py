"""The follow-up engine against the database, Redis and the storage emulator: time-advanced
checks, the geo-block and warning scenarios, escalation from the last notice of the first wave,
30 days of monitoring, reopening, inbound and bounce webhooks.

Time moves by passing ``now``; the HTTP side is a ``SafeHttpClient`` on a mock transport and
the browser's answer is a job folder written as the browser would (``test_persist``).
"""

from __future__ import annotations

import base64
import functools
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
import redis
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from kalkan.api.app import create_app
from kalkan.config import Settings
from kalkan.db.models import (
    Case,
    CaseEvent,
    Customer,
    JobOutbox,
    Message,
    Notice,
    Recheck,
    ReviewTask,
    WebhookEvent,
)
from kalkan.db.outbox import HTTP_PRECHECK, PLAN_NOTICES, PROCESS_WEBHOOK_EVENT, RECHECK
from kalkan.db.session import Database
from kalkan.db.settings_store import write_setting
from kalkan.domain import case_state
from kalkan.domain.enums import (
    CaseState,
    Channel,
    EventType,
    MessageDirection,
    NoticeBasis,
    NoticeStatus,
    Profile,
    RecheckMethod,
    RecheckResult,
    ReplyClass,
    TaskKind,
    TaskStatus,
    WebhookSource,
)
from kalkan.followup import escalation, recheck, webhooks
from kalkan.followup.classify import classify_reply
from kalkan.followup.recheck import Target
from kalkan.net.safe_http import NoSuchHostError, SafeHttpClient
from kalkan.notices.planning import plan_notices
from kalkan.scanning import persist
from kalkan.scanning.capture_contract import Purpose
from kalkan.scanning.capture_jobs import issue_capture
from kalkan.storage.gcs import ObjectStore
from kalkan.workers.followup import issue_browser as worker_issue_browser
from tests.ai_support import FakeApi, recorded
from tests.integration.test_notices import (
    HOSTING,
    REGISTRAR,
    FakeProvider,
    approved_case,
    drain,
    notice_settings,
    notices_of,
    unflagged_legal,  # noqa: F401 - a fixture
)
from tests.integration.test_persist import (
    BUCKET,
    _result,
    _write,
    capture_redis,  # noqa: F401 - a fixture
    root,  # noqa: F401 - a fixture
)
from tests.unit.test_replies import postmark

AUTH = "postmark:s3cret"
UP_PAGE = "<html><title>Mintera Bank | Login</title><body>Mintera Bank login</body></html>"
WARNING_PAGE = (
    "<html><title>Suspected phishing site | Cloudflare</title><body>Warning: Suspected "
    "Phishing Site Ahead! This website has been reported for potential phishing.</body></html>"
)
# The legal texts as if the lawyer had approved them, so real notices go out.
pytestmark = pytest.mark.usefixtures("unflagged_legal")
SUSPENDED_PAGE = "<html><title>Account Suspended</title><body>This Account has been suspended."


# --- helpers ----------------------------------------------------------------------------------


@dataclass
class Site:
    """What the mock network answers; change it between checks."""

    status: int = 200
    body: str = UP_PAGE
    dns: bool = True

    def client(self) -> SafeHttpClient:
        def resolve(host: str) -> list[str]:
            if not self.dns:
                raise NoSuchHostError(f"{host}: no such host")
            return ["93.184.215.14"]

        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                self.status,
                stream=httpx2.ByteStream(self.body.encode()),
                headers={"content-type": "text/html; charset=utf-8"},
            )

        return SafeHttpClient(resolver=resolve, transport=httpx2.MockTransport(handler))


@dataclass
class Browser:
    """Stands in for the capture job issuer; ``pending`` plays a job that never came back."""

    pending: bool = False
    issued: list[Target] = field(default_factory=list)

    def __call__(self, target: Target, now: datetime) -> uuid.UUID | None:
        self.issued.append(target)
        return None if self.pending else uuid.uuid4()


def live(test_settings: Settings) -> Settings:
    # The test factories number cases TST-<hex>.
    return notice_settings(test_settings, notices_dry_run=False, case_prefix="TST")


def notified_case(
    database: Database,
    store: ObjectStore,
    test_settings: Settings,
    *,
    basis: NoticeBasis = NoticeBasis.PHISHING,
) -> Case:
    """A case whose real first wave went out (registrar, hosting, the Google form)."""
    settings = live(test_settings)
    with database.worker_session() as session:
        write_setting(session, "sending_enabled", True, actor="test")
    case = approved_case(
        database, store, settings, basis=basis, dry_run=False, loa_signed_at=datetime.now(UTC)
    )
    plan_notices(database, settings, case.id, now=datetime.now(UTC))
    drain(database, settings, FakeProvider())
    found = row(database, case.id)
    assert found.state is CaseState.NOTIFIED
    return found


def row(database: Database, case_id: uuid.UUID) -> Case:
    with database.worker_session() as session:
        found = session.get_one(Case, case_id)
        session.expunge(found)
        return found


def events(database: Database, case_id: uuid.UUID) -> list[CaseEvent]:
    with database.worker_session() as session:
        rows = session.scalars(
            select(CaseEvent).where(CaseEvent.case_id == case_id).order_by(CaseEvent.at)
        ).all()
        for item in rows:
            session.expunge(item)
        return list(rows)


def event_types(database: Database, case_id: uuid.UUID, since: datetime) -> list[EventType]:
    return [event.type for event in events(database, case_id) if event.at >= since]


def jobs(database: Database, kind: str) -> list[dict[str, str]]:
    with database.worker_session() as session:
        return [
            dict(job.payload)
            for job in session.scalars(select(JobOutbox).where(JobOutbox.kind == kind))
        ]


def tasks(database: Database, case_id: uuid.UUID, kind: TaskKind) -> list[ReviewTask]:
    with database.worker_session() as session:
        rows = session.scalars(
            select(ReviewTask).where(ReviewTask.case_id == case_id, ReviewTask.kind == kind)
        ).all()
        for item in rows:
            session.expunge(item)
        return list(rows)


def check(
    database: Database,
    case_id: uuid.UUID,
    site: Site,
    at: datetime,
    browser: recheck.IssueBrowser | None = None,
) -> recheck.RecheckOutcome:
    with site.client() as client:
        return recheck.run_recheck(
            database,
            client,
            case_id,
            now=lambda: at,
            issue_browser=browser or Browser(pending=True),
        )


# --- the notified site -------------------------------------------------------------------------


def test_a_notified_site_that_is_up_is_checked_every_six_hours(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    case = notified_case(database, object_store, test_settings)
    at = datetime.now(UTC) + timedelta(hours=6)
    result = check(database, case.id, Site(), at)
    assert result.observation is not None
    assert result.observation.result is RecheckResult.ONLINE
    found = row(database, case.id)
    assert (found.state, found.offline_since, found.next_check_at) == (
        CaseState.NOTIFIED,
        None,
        at + timedelta(hours=6),
    )
    with database.worker_session() as session:
        checks = session.scalars(select(Recheck).where(Recheck.case_id == case.id)).all()
        assert [(c.method, c.result) for c in checks] == [
            (RecheckMethod.HTTP, RecheckResult.ONLINE)
        ]


def test_seventy_two_hours_down_without_a_break_takes_the_site_down(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    case = notified_case(database, object_store, test_settings)
    start = datetime.now(UTC) + timedelta(hours=6)
    down, up = Site(dns=False), Site()

    check(database, case.id, down, start)
    assert row(database, case.id).offline_since == start
    # Up at hour 36: the window starts again with the next down check.
    check(database, case.id, up, start + timedelta(hours=36))
    assert row(database, case.id).offline_since is None
    second = start + timedelta(hours=42)
    for hours in range(42, 114, 6):
        check(database, case.id, down, start + timedelta(hours=hours))
        assert row(database, case.id).state is CaseState.NOTIFIED
    taken = start + timedelta(hours=114)
    check(database, case.id, down, taken)

    found = row(database, case.id)
    assert found.state is CaseState.TAKEN_DOWN
    assert (found.offline_since, found.taken_down_at) == (second, taken)
    assert found.monitor_until == taken + timedelta(days=30)
    assert found.next_check_at == taken + timedelta(days=1)
    assert found.last_action == "Taken down"
    # The takedown time of product.md §12: the first notice to the start of the 72-hour window.
    assert found.first_notice_at is not None
    assert second - found.first_notice_at > timedelta(hours=47)
    assert event_types(database, case.id, start) == [
        EventType.OFFLINE_DETECTED,
        EventType.ONLINE_AGAIN,
        EventType.OFFLINE_DETECTED,
        EventType.TAKEN_DOWN,
    ]


def test_a_site_blocking_our_country_is_not_taken_for_down(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    capture_redis: redis.Redis,  # noqa: F811
    root: Path,  # noqa: F811
) -> None:
    """HTTP 403 from here, the page through the proxy from the target market."""
    case = notified_case(database, object_store, test_settings)
    at = datetime.now(UTC) + timedelta(hours=6)
    sent: list[Any] = []

    def issue(target: Target, now: datetime) -> uuid.UUID | None:
        job = issue_capture(
            capture_redis,
            purpose=Purpose.RECHECK,
            customer_id=target.customer_id,
            brand_id=target.brand_id,
            case_id=target.case_id,
            urls=target.urls,
            country=target.country,
            now=now,
            send=sent.append,
        )
        return job.job_id if job is not None else None

    # A check that saw it down before: the browser's page clears the window too.
    with database.worker_session() as session:
        session.get_one(Case, case.id).offline_since = at - timedelta(hours=6)
    result = check(database, case.id, Site(status=403, body="Forbidden"), at, issue)
    assert result.browser_job is not None
    assert [job.purpose for job in sent] == [Purpose.RECHECK]
    assert row(database, case.id).offline_since == at - timedelta(hours=6)  # undecided yet

    job_id = result.browser_job
    _write(root, job_id, [_result(Profile.DESKTOP), _result(Profile.MOBILE)])
    later = at + timedelta(minutes=5)
    stored = persist.persist_capture(
        database, capture_redis, object_store, BUCKET, job_id, now=lambda: later, root=root
    )
    assert stored.status is persist.PersistStatus.RECHECK

    found = row(database, case.id)
    assert (found.state, found.offline_since) == (CaseState.NOTIFIED, None)
    with database.worker_session() as session:
        checks = session.scalars(
            select(Recheck).where(Recheck.case_id == case.id).order_by(Recheck.checked_at)
        ).all()
        assert [(c.method, c.result) for c in checks] == [
            (RecheckMethod.HTTP, RecheckResult.UNKNOWN),
            (RecheckMethod.BROWSER, RecheckResult.ONLINE),
        ]
        from kalkan.db.models import Scan

        scans = session.scalars(
            select(Scan).where(Scan.case_id == case.id, Scan.started_at >= at - timedelta(1))
        ).all()
    assert len(scans) >= 2  # the new evidence of the confirmation
    assert EventType.ONLINE_AGAIN in event_types(database, case.id, at)


def test_a_warning_page_is_recorded_and_never_starts_the_window(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    case = notified_case(database, object_store, test_settings)
    at = datetime.now(UTC) + timedelta(hours=6)
    warning = Site(body=WARNING_PAGE)
    result = check(database, case.id, warning, at)
    assert result.observation is not None
    assert result.observation.result is RecheckResult.WARNING
    check(database, case.id, warning, at + timedelta(hours=6))
    for hours in range(12, 96, 6):
        check(database, case.id, warning, at + timedelta(hours=hours))
    found = row(database, case.id)
    assert (found.state, found.offline_since) == (CaseState.NOTIFIED, None)
    assert event_types(database, case.id, at).count(EventType.WARNING_ACTIVE) == 1


def test_a_suspension_page_counts_as_down(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    case = notified_case(database, object_store, test_settings)
    at = datetime.now(UTC) + timedelta(hours=6)
    browser = Browser()
    result = check(database, case.id, Site(body=SUSPENDED_PAGE), at, browser)
    # Down by HTTP: the browser confirms before anything changes.
    assert result.browser_job is not None
    assert len(browser.issued) == 1
    assert row(database, case.id).offline_since is None
    assert row(database, case.id).next_check_at == at + timedelta(hours=6)
    # The browser never answered: the next check stands on its own.
    check(database, case.id, Site(body=SUSPENDED_PAGE), at + timedelta(hours=6))
    assert row(database, case.id).offline_since == at + timedelta(hours=6)


# --- taken down: monitoring, archive, reopening ------------------------------------------------


def _taken_down(database: Database, case_id: uuid.UUID, at: datetime) -> None:
    with database.worker_session() as session:
        case = session.get_one(Case, case_id)
        case.offline_since = at - timedelta(hours=72)
        case_state.transition(session, case, CaseState.TAKEN_DOWN, actor="test", at=at)


def test_a_taken_down_site_is_watched_for_thirty_days_then_archived(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    case = notified_case(database, object_store, test_settings)
    at = datetime.now(UTC) + timedelta(days=4)
    _taken_down(database, case.id, at)
    down = Site(dns=False)
    for day in (1, 2, 29):
        check(database, case.id, down, at + timedelta(days=day))
        found = row(database, case.id)
        assert found.state is CaseState.TAKEN_DOWN
        assert found.next_check_at == at + timedelta(days=day + 1)
    check(database, case.id, down, at + timedelta(days=30))
    found = row(database, case.id)
    assert (found.state, found.next_check_at, found.last_action) == (
        CaseState.ARCHIVED,
        None,
        "Archived",
    )


def test_a_site_that_comes_back_is_reopened_with_a_new_round(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    settings = live(test_settings)
    case = notified_case(database, object_store, test_settings)
    at = datetime.now(UTC) + timedelta(days=4)
    _taken_down(database, case.id, at)
    browser = Browser()
    back = Site()
    result = check(database, case.id, back, at + timedelta(days=3), browser)
    assert result.browser_job is not None  # up again: the browser confirms first
    assert row(database, case.id).state is CaseState.TAKEN_DOWN
    reopened_at = at + timedelta(days=4)
    check(database, case.id, back, reopened_at)  # the confirmation never came: HTTP stands

    found = row(database, case.id)
    assert (found.state, found.reopened_count, found.taken_down_at) == (
        CaseState.NOTIFIED,
        1,
        None,
    )
    assert found.next_check_at == reopened_at + timedelta(hours=6)
    assert EventType.REOPENED in event_types(database, case.id, reopened_at)
    assert jobs(database, PLAN_NOTICES)[-1]["case_id"] == str(case.id)

    plan_notices(database, settings, case.id, now=datetime.now(UTC))
    drain(database, settings, FakeProvider())
    rounds = {(n.channel, n.round, n.status) for n in notices_of(database, case.id)}
    assert (Channel.REGISTRAR, 2, NoticeStatus.SENT) in rounds
    assert (Channel.HOSTING, 2, NoticeStatus.SENT) in rounds
    assert (Channel.REGISTRAR, 1, NoticeStatus.SENT) in rounds


# --- watching and dismissed ---------------------------------------------------------------------


def _parked(database: Database, case_id: uuid.UUID, state: CaseState, **kw: Any) -> None:
    with database.worker_session() as session:
        case = session.get_one(Case, case_id)
        case.state = state
        for name, value in kw.items():
            setattr(case, name, value)


def test_a_watched_site_that_grows_a_login_form_goes_back_to_detection(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    case = approved_case(database, object_store, notice_settings(test_settings))
    _parked(database, case.id, CaseState.WATCHING)
    at = datetime.now(UTC) + timedelta(days=7)
    check(database, case.id, Site(body="<html><title>For sale</title>This domain is for sale"), at)
    assert row(database, case.id).next_check_at == at + timedelta(days=7)
    form = "<html><body><form><input type=password name=p></form></body></html>"
    result = check(database, case.id, Site(body=form), at + timedelta(days=7))
    assert result.reappeared == "a password field appeared"
    assert row(database, case.id).state is CaseState.DETECTED
    assert jobs(database, HTTP_PRECHECK)[-1]["case_id"] == str(case.id)


def test_a_whitelisted_case_is_never_checked_again(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    case = approved_case(database, object_store, notice_settings(test_settings))
    with database.worker_session() as session:
        found = session.get_one(Case, case.id)
        case_state.transition(
            session, found, CaseState.DISMISSED, actor="test", event=EventType.WHITELISTED
        )
        assert found.next_check_at is None
        found.next_check_at = datetime.now(UTC)
    result = check(database, case.id, Site(), datetime.now(UTC) + timedelta(days=30))
    assert result.skipped
    assert row(database, case.id).next_check_at is None


def test_due_cases_are_leased_and_the_demo_is_left_out(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    case = notified_case(database, object_store, test_settings)
    demo = approved_case(database, object_store, notice_settings(test_settings), is_demo=True)
    at = datetime.now(UTC) + timedelta(hours=7)
    _parked(database, demo.id, CaseState.NOTIFIED, next_check_at=at - timedelta(hours=1))
    assert recheck.lease_due(database, at) == [case.id]
    assert row(database, case.id).next_check_at == at + timedelta(hours=1)
    assert recheck.lease_due(database, at) == []


def test_the_worker_issues_a_recheck_job_for_the_browser(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    capture_redis: redis.Redis,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kalkan.workers import followup

    case = notified_case(database, object_store, test_settings)
    sent: list[Any] = []
    monkeypatch.setattr(followup, "capture_redis", lambda: capture_redis)
    monkeypatch.setattr(
        followup, "issue_capture", functools.partial(issue_capture, send=sent.append)
    )
    with database.worker_session() as session:
        target = recheck._target(session, case.id)
    assert target is not None
    first = worker_issue_browser(target, datetime.now(UTC))
    assert first is not None
    assert worker_issue_browser(target, datetime.now(UTC)) is None  # one job per case
    assert [job.purpose for job in sent] == [Purpose.RECHECK]
    assert sent[0].country == "PL" or sent[0].country is None


# --- escalation --------------------------------------------------------------------------------


def _sent_at(database: Database, case_id: uuid.UUID, when: dict[Channel, datetime]) -> None:
    with database.worker_session() as session:
        for notice in session.scalars(select(Notice).where(Notice.case_id == case_id)):
            if notice.channel in when and not notice.dry_run:
                notice.sent_at = when[notice.channel]


def test_escalation_counts_from_the_last_notice_of_the_first_wave(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    settings = live(test_settings)
    case = notified_case(database, object_store, test_settings)
    t0 = datetime.now(UTC)
    _sent_at(database, case.id, {Channel.REGISTRAR: t0, Channel.HOSTING: t0 + timedelta(hours=10)})

    def run(hours: float) -> list[escalation.Escalated]:
        with database.worker_session() as session:
            found = session.get_one(Case, case.id, with_for_update=True)
            return escalation.escalate_case(
                session, settings, found, now=t0 + timedelta(hours=hours)
            )

    assert run(50) == []  # 48 hours after the registrar, but not after the host
    first = run(58)
    assert [(e.level, e.channel) for e in first] == [(1, "registry")]
    assert run(59) == []
    registry = [n for n in notices_of(database, case.id) if n.channel is Channel.REGISTRY]
    assert [(n.escalation_level, n.round, n.status) for n in registry] == [
        (1, 1, NoticeStatus.DRAFT)
    ]
    drain(database, settings, FakeProvider())
    # No registry address is known for .online: the operator finds the way.
    registry = [n for n in notices_of(database, case.id) if n.channel is Channel.REGISTRY]
    assert registry[0].status is NoticeStatus.MANUAL_PENDING
    second = run(106)
    assert [(e.level, e.channel) for e in second] == [(2, "operator")]
    assert [t.status for t in tasks(database, case.id, TaskKind.ESCALATION)] == [TaskStatus.OPEN]
    escalations = [
        event.data
        for event in events(database, case.id)
        if event.type is EventType.REGISTRAR_ESCALATED
    ]
    assert [(d["level"], d["channel"], d["round"]) for d in escalations] == [
        (1, "registry", 1),
        (2, "operator", 1),
    ]


def test_a_trademark_case_escalates_to_the_operator(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    settings = live(test_settings)
    case = notified_case(database, object_store, test_settings, basis=NoticeBasis.TRADEMARK)
    t0 = datetime.now(UTC)
    _sent_at(database, case.id, {Channel.HOSTING: t0})
    done = escalation.check_escalations(database, settings, now=t0 + timedelta(hours=49))
    assert [(e.level, e.channel) for e in done] == [(1, "operator")]
    assert len(tasks(database, case.id, TaskKind.ESCALATION)) == 1


def test_no_escalation_after_a_removed_reply_or_while_the_site_is_down(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    settings = live(test_settings)
    case = notified_case(database, object_store, test_settings)
    down = notified_case(database, object_store, test_settings)
    t0 = datetime.now(UTC)
    with database.worker_session() as session:
        session.add(
            Message(
                customer_id=case.customer_id,
                case_id=case.id,
                direction=MessageDirection.INBOUND,
                from_addr=REGISTRAR,
                classification=ReplyClass.REMOVED,
                classification_confidence=Decimal("0.9"),
                received_at=t0 + timedelta(hours=1),
            )
        )
        session.get_one(Case, down.id).offline_since = t0 + timedelta(hours=40)
    assert escalation.check_escalations(database, settings, now=t0 + timedelta(hours=100)) == []


# --- webhooks and replies ----------------------------------------------------------------------


@pytest.fixture
def api(test_settings: Settings) -> Iterator[TestClient]:
    app = create_app(
        test_settings.model_copy(
            update={"edge_shared_secret": None, "postmark_webhook_basic_auth": SecretStr(AUTH)}
        )
    )
    with TestClient(app) as client:
        yield client


def _auth(value: str = AUTH) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(value.encode()).decode()}


def store_events(api: TestClient, path: str, payload: dict[str, Any]) -> uuid.UUID:
    response = api.post(f"/api/webhooks/postmark/{path}", json=payload, headers=_auth())
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    database: Database = api.app.state.database  # type: ignore[attr-defined]
    with database.worker_session() as session:
        return session.scalars(
            select(WebhookEvent.id).order_by(WebhookEvent.received_at.desc()).limit(1)
        ).one()


def test_webhooks_need_basic_auth(api: TestClient, database: Database) -> None:
    payload = postmark("inbound_removed.json")
    for headers in ({}, _auth("postmark:wrong"), {"Authorization": "Bearer x"}):
        response = api.post("/api/webhooks/postmark/inbound", json=payload, headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic")
    assert api.post("/api/webhooks/postmark/bounce", json={}, headers={}).status_code == 401
    with database.worker_session() as session:
        assert session.scalars(select(WebhookEvent)).all() == []
    bad = api.post(
        "/api/webhooks/postmark/inbound",
        content=b"not json",
        headers={**_auth(), "content-type": "application/json"},
    )
    assert bad.status_code == 400


def test_the_api_only_stores_the_event_and_its_job(api: TestClient, database: Database) -> None:
    event_id = store_events(api, "inbound", postmark("inbound_removed.json"))
    with database.worker_session() as session:
        event = session.get_one(WebhookEvent, event_id)
        assert (event.source, event.processed_at, event.signature_ok) == (
            WebhookSource.POSTMARK_INBOUND,
            None,
            True,
        )
        # Attachment contents are not kept.
        assert "Content" not in event.payload["Attachments"][0]
        assert session.scalars(select(Message)).all() == []
    assert jobs(database, PROCESS_WEBHOOK_EVENT) == [{"event_id": str(event_id)}]


def _reply(category: str, confidence: float = 0.92) -> dict[str, Any]:
    message = recorded("message_verdict.json")
    message["content"][1]["text"] = json.dumps(
        {"category": category, "confidence": confidence, "summary": "A short summary."}
    )
    return message


def _process(
    database: Database, settings: Settings, store: ObjectStore, event_id: uuid.UUID
) -> webhooks.Processed:
    return webhooks.process_webhook_event(
        database,
        settings,
        lambda: (store, BUCKET),
        event_id,
        now=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("fixture", "category", "sender"),
    [
        ("inbound_removed.json", "removed", REGISTRAR),
        ("inbound_needs_info.json", "needs_info", HOSTING),
        ("inbound_not_our_customer.json", "not_our_customer", HOSTING),
        ("inbound_auto_reply.json", "auto_reply", "noreply@registrar.example"),
    ],
)
def test_an_inbound_reply_is_linked_classified_and_acted_on(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    api: TestClient,
    fixture: str,
    category: str,
    sender: str,
) -> None:
    settings = live(test_settings)
    case = notified_case(database, object_store, test_settings)
    notices = {n.channel: n for n in notices_of(database, case.id) if not n.dry_run}
    target = notices[Channel.REGISTRAR if sender != HOSTING else Channel.HOSTING]
    payload = postmark(
        fixture,
        number=case.number.lower(),
        number_upper=case.number,
        in_reply_to=target.provider_message_id or "<none@example.test>",
    )
    event_id = store_events(api, "inbound", payload)
    processed = _process(database, settings, object_store, event_id)
    assert processed.outcome is webhooks.Outcome.CLASSIFY
    assert processed.message_id == event_id

    fake = FakeApi().queue((200, _reply(category)))
    action = classify_reply(database, fake.client(), settings, event_id, now=datetime.now(UTC))
    assert action is not None
    assert action.category.value == category
    [body] = fake.bodies()
    prompt = body["messages"][0]["content"][0]["text"]
    assert "<untrusted_email>" in prompt
    assert body["model"] == settings.ai_triage_model

    with database.worker_session() as session:
        message = session.get_one(Message, event_id)
        assert message.case_id == case.id
        assert message.notice_id == target.id
        assert message.raw_key is not None
        assert message.raw_key.endswith(f"messages/{event_id}.eml")
        notice = session.get_one(Notice, target.id)
        event = session.get_one(WebhookEvent, event_id)
        assert (event.processed_at is not None, event.error) == (True, None)
    assert object_store.read_bytes(message.raw_key).startswith(b"From: ")
    replies = [e for e in events(database, case.id) if e.type is EventType.REPLY_RECEIVED]
    assert [e.data["classification"] for e in replies] == [category]

    if category == "removed":
        assert notice.status is NoticeStatus.ACKNOWLEDGED
        assert jobs(database, RECHECK)[-1]["case_id"] == str(case.id)
    elif category == "needs_info":
        assert notice.status is NoticeStatus.SENT
        [task] = tasks(database, case.id, TaskKind.NEEDS_INFO)
        assert task.notice_id == target.id
    elif category == "not_our_customer":
        assert (notice.status, notice.failure_reason) == (
            NoticeStatus.REJECTED,
            "the recipient says the site is not its customer",
        )
        [task] = tasks(database, case.id, TaskKind.REPLY)
        assert "Not tried yet: registry" in (task.reason or "")
    else:
        assert notice.status is NoticeStatus.SENT
        assert tasks(database, case.id, TaskKind.REPLY) == []
        assert tasks(database, case.id, TaskKind.NEEDS_INFO) == []
    # A redelivered message asks nothing again.
    assert classify_reply(database, None, settings, event_id, now=datetime.now(UTC)) is None


def test_a_retried_postmark_post_is_a_duplicate(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    api: TestClient,
) -> None:
    settings = live(test_settings)
    case = notified_case(database, object_store, test_settings)
    payload = postmark(
        "inbound_needs_info.json",
        number=case.number.lower(),
        number_upper=case.number,
        message_id="pm-same",
    )
    first = store_events(api, "inbound", payload)
    assert _process(database, settings, object_store, first).outcome is webhooks.Outcome.CLASSIFY
    second = store_events(api, "inbound", payload)
    assert _process(database, settings, object_store, second).outcome is (
        webhooks.Outcome.DUPLICATE
    )
    assert _process(database, settings, object_store, first).outcome is webhooks.Outcome.SKIPPED


def test_a_weak_or_failed_classification_goes_to_the_operator(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    api: TestClient,
) -> None:
    settings = live(test_settings)
    case = notified_case(database, object_store, test_settings)
    payload = postmark("inbound_removed.json", number=case.number.lower(), number_upper=case.number)
    event_id = store_events(api, "inbound", payload)
    _process(database, settings, object_store, event_id)
    fake = FakeApi().queue((200, _reply("removed", 0.3)))
    action = classify_reply(database, fake.client(), settings, event_id, now=datetime.now(UTC))
    assert action is not None
    assert action.category is ReplyClass.OTHER
    assert jobs(database, RECHECK) == []
    assert len(tasks(database, case.id, TaskKind.REPLY)) == 1


def test_a_reply_without_a_case_waits_on_the_replies_page(
    database: Database, object_store: ObjectStore, test_settings: Settings, api: TestClient
) -> None:
    settings = live(test_settings)
    payload = postmark("inbound_removed.json", number="nothing", number_upper="no number")
    payload["ToFull"] = [{"Email": "abuse-reports@example.test", "MailboxHash": ""}]
    payload["OriginalRecipient"] = "abuse-reports@example.test"
    payload["MailboxHash"] = ""
    event_id = store_events(api, "inbound", payload)
    assert _process(database, settings, object_store, event_id).outcome is (
        webhooks.Outcome.UNMATCHED
    )
    with database.worker_session() as session:
        event = session.get_one(WebhookEvent, event_id)
        assert (event.processed_at is not None, event.error) == (True, "unmatched")
        assert session.scalars(select(Message)).all() == []


def test_a_bounce_fails_its_notice_and_opens_a_task(
    database: Database,
    object_store: ObjectStore,
    test_settings: Settings,
    api: TestClient,
) -> None:
    settings = live(test_settings)
    case = notified_case(database, object_store, test_settings)
    provider_id = str(uuid.uuid4())
    with database.worker_session() as session:
        notice = session.scalars(
            select(Notice).where(
                Notice.case_id == case.id,
                Notice.channel == Channel.REGISTRAR,
                Notice.dry_run.is_(False),
            )
        ).one()
        notice.external_ref = provider_id
        notice_id = notice.id
    event_id = store_events(
        api, "bounce", postmark("bounce.json", provider_id=provider_id, number_upper=case.number)
    )
    assert _process(database, settings, object_store, event_id).outcome is (
        webhooks.Outcome.BOUNCED
    )
    with database.worker_session() as session:
        notice = session.get_one(Notice, notice_id)
        assert notice.status is NoticeStatus.FAILED
        assert (notice.failure_reason or "").startswith("bounce: HardBounce")
        assert "Content" not in session.get_one(WebhookEvent, event_id).payload
    [task] = tasks(database, case.id, TaskKind.BOUNCE)
    assert (task.notice_id, task.status) == (notice_id, TaskStatus.OPEN)
    # Unknown message: unmatched.
    other = store_events(api, "bounce", postmark("bounce.json", provider_id="unknown"))
    assert _process(database, settings, object_store, other).outcome is webhooks.Outcome.UNMATCHED


def test_the_reconciler_picks_up_a_forgotten_event(database: Database, api: TestClient) -> None:
    from kalkan.scheduler import reconciler

    event_id = store_events(api, "bounce", postmark("bounce.json"))
    sent: list[tuple[str, dict[str, Any]]] = []
    later = datetime.now(UTC) + timedelta(minutes=11)
    result = reconciler.reconcile(database, lambda actor, **kw: sent.append((actor, kw)), later)
    assert result.webhooks == 1
    assert ("process_webhook_event", {"event_id": str(event_id)}) in sent


def test_the_reconciler_plans_a_reopened_round(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    from kalkan.scheduler import reconciler

    case = notified_case(database, object_store, test_settings)
    at = datetime.now(UTC)
    _taken_down(database, case.id, at)
    with database.worker_session() as session:
        found = session.get_one(Case, case.id)
        case_state.transition(session, found, CaseState.NOTIFIED, actor="test", at=at)
    assert reconciler.unplanned_cases(database, at + timedelta(minutes=30)) == []
    assert reconciler.unplanned_cases(database, at + timedelta(hours=2)) == [str(case.id)]
    with database.worker_session() as session:
        assert session.get_one(Customer, case.customer_id).is_demo is False
