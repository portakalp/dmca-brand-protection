"""``make e2e``: the pipeline end to end (architecture.md §25; K148).

1. The fixture clone (``mintera-login.test``) is injected as a candidate. The HTTP pre-check,
   a real Chromium through the dev-proxy and ``persist_capture`` run on a Dramatiq worker; the
   signals, the AI triage (a recorded answer), the decision and the operator's approval follow
   in-process. With dry-run off (the local provider is Mailpit), the notices land in Mailpit
   addressed to their real recipients. Then the site is shut down (its name stops resolving)
   and time moves on: every six-hour check sees it down and the case is taken down 72 hours
   after the first down check; the takedown time is measured from the first notice.
2. The same clone with the customer in dry-run: previews go to the operator's mailbox only;
   turning dry-run off lifts the gate and the real first wave goes out.

Needs ``make up-infra`` and Chromium (``uv run playwright install chromium``).
"""

from __future__ import annotations

import functools
import ipaddress
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import select

from kalkan.config import Settings
from kalkan.db.models import Customer, Domain, Notice
from kalkan.db.settings_store import write_setting
from kalkan.decision.apply import StaffAction, decide, staff_decision
from kalkan.domain.enums import (
    CaseState,
    Channel,
    EventType,
    NoticeBasis,
    NoticeStatus,
    RecheckResult,
)
from kalkan.email.provider import SmtpProvider
from kalkan.followup import recheck
from kalkan.followup.recheck import Target
from kalkan.net.safe_http import NoSuchHostError, SafeHttpClient
from kalkan.notices.blocks import clear_blocked_cases
from kalkan.signals.compute import compute_signals
from kalkan.workers import scanning as scanning_worker
from tests.ai_support import FakeApi
from tests.integration.test_capture_pipeline import (
    FIXTURE_HOSTS,
    FIXTURE_PORT,
    Pipeline,
    _brand,
    _case,
    _state,
    chromium,  # noqa: F401 - a fixture
    http,  # noqa: F401 - a fixture
    pipeline,  # noqa: F401 - a fixture
)
from tests.integration.test_followup import events, row
from tests.integration.test_notices import (
    REDIRECT,
    drain,
    notice_settings,
    notices_of,
    unflagged_legal,  # noqa: F401 - a fixture
)

MAILPIT = "http://localhost:8025/api/v1"
SMTP = "smtp://localhost:2525"
SENDER = "abuse-reports@example.test"
REGISTRAR = "abuse@registrar.example"
HOSTING = "abuse@host.example"

pytestmark = pytest.mark.usefixtures("unflagged_legal")


@pytest.fixture
def mailpit() -> Iterator[httpx2.Client]:
    with httpx2.Client() as client:
        client.delete(f"{MAILPIT}/messages")
        yield client


def mails(client: httpx2.Client, number: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = client.get(f"{MAILPIT}/search", params={"query": number}).json()[
        "messages"
    ]
    return found


def http_client(down: set[str]) -> SafeHttpClient:
    """Stage 1's view of the fixture sites; a host in ``down`` no longer resolves."""

    def resolve(host: str) -> list[str]:
        if host in FIXTURE_HOSTS and host not in down:
            return ["127.0.0.1"]
        raise NoSuchHostError(host)

    return SafeHttpClient(
        allow_cidrs=[ipaddress.ip_network("127.0.0.1/32")],
        resolver=resolve,
        port_map={80: FIXTURE_PORT, 443: FIXTURE_PORT + 363},
    )


def _at(value: datetime) -> datetime:
    return value


def no_browser(target: Target, now: datetime) -> uuid.UUID | None:
    """A confirmation job that is still waiting: the HTTP check stands (K142)."""
    return None


def captured_clone(line: Pipeline, settings: Settings, **customer: Any) -> uuid.UUID:
    """The injected clone through pre-check, browser, signals, triage, decision, approval."""
    database = line.database
    brand = _brand(database)
    with database.worker_session() as session:
        owner = session.get_one(Customer, brand.customer_id)
        owner.sending_enabled = True
        owner.loa_signed_at = datetime.now(UTC)
        for name, value in customer.items():
            setattr(owner, name, value)
        write_setting(session, "sending_enabled", True, actor="e2e")
    case_id = _case(database, brand, "mintera-login.test")
    with database.worker_session() as session:
        domain = session.scalars(select(Domain).where(Domain.name == "mintera-login.test")).one()
        domain.registrar_abuse_email = REGISTRAR
        domain.registrar_iana_id = 1068
        domain.hosting_abuse_email = HOSTING
        domain.hosting_country = "PL"
        domain.asn = 64500

    scanning_worker.http_precheck.send(str(case_id))
    line.run()
    assert _state(database, case_id) is CaseState.DETECTED
    assert compute_signals(database, line.store, case_id).value == "computed"

    from kalkan.ai.assess import Stage, assess
    from kalkan.ai.client import DatabaseRecorder

    api = FakeApi().queue("message_verdict.json")
    client = api.client(recorder=DatabaseRecorder(database))
    assess(database, line.store, client, settings, case_id, Stage.TRIAGE)
    decide(database, case_id, now=datetime.now(UTC))
    # Automatic approval is off until the evaluation allows it: the operator approves.
    assert _state(database, case_id) is CaseState.IN_REVIEW
    with database.worker_session() as session:
        staff_decision(
            session,
            case_id,
            StaffAction.APPROVE,
            actor="operator@example.test",
            ip=None,
            now=datetime.now(UTC),
            basis=NoticeBasis.PHISHING,
        )
    assert _state(database, case_id) is CaseState.APPROVED
    return case_id


def test_a_clone_is_reported_and_its_takedown_is_measured(
    pipeline: Pipeline,  # noqa: F811
    test_settings: Settings,
    mailpit: httpx2.Client,
) -> None:
    settings = notice_settings(test_settings, notices_dry_run=False, smtp_url=SMTP)
    case_id = captured_clone(pipeline, settings, dry_run=False)
    database = pipeline.database
    drain(database, settings, SmtpProvider(SMTP, SENDER))

    case = row(database, case_id)
    assert case.state is CaseState.NOTIFIED
    assert case.first_notice_at is not None
    sent = mails(mailpit, case.number)
    assert sorted(to["Address"] for mail in sent for to in mail["To"]) == [HOSTING, REGISTRAR]
    assert all(not mail["Subject"].startswith("[DRY RUN]") for mail in sent)
    real = {n.channel: n.status for n in notices_of(database, case_id) if not n.dry_run}
    assert real[Channel.REGISTRAR] is NoticeStatus.SENT
    assert real[Channel.HOSTING] is NoticeStatus.SENT

    t0 = case.first_notice_at
    up, down = http_client(set()), http_client({"mintera-login.test"})
    try:
        seen = recheck.run_recheck(
            database, up, case_id, now=lambda: t0 + timedelta(hours=6), issue_browser=no_browser
        )
        assert seen.observation is not None
        assert seen.observation.result is RecheckResult.ONLINE
        # The fixture site is shut down; every six hours the follow-up sees it down.
        hours = 12
        while row(database, case_id).state is CaseState.NOTIFIED:
            at = t0 + timedelta(hours=hours)
            recheck.run_recheck(
                database, down, case_id, now=functools.partial(_at, at), issue_browser=no_browser
            )
            hours += 6
            assert hours <= 12 + 72 + 6, "the case was never taken down"
    finally:
        up.close()
        down.close()

    case = row(database, case_id)
    assert case.state is CaseState.TAKEN_DOWN
    assert case.offline_since == t0 + timedelta(hours=12)
    assert case.taken_down_at == t0 + timedelta(hours=84)
    # Median takedown time (product.md §12): from the first notice to the start of the window.
    assert case.offline_since - t0 == timedelta(hours=12)
    kinds = [event.type for event in events(database, case_id)]
    assert kinds.index(EventType.OFFLINE_DETECTED) < kinds.index(EventType.TAKEN_DOWN)


def test_a_dry_run_preview_then_the_real_first_wave(
    pipeline: Pipeline,  # noqa: F811
    test_settings: Settings,
    mailpit: httpx2.Client,
) -> None:
    preview = notice_settings(test_settings, smtp_url=SMTP)
    case_id = captured_clone(pipeline, preview, dry_run=True)
    database = pipeline.database
    provider = SmtpProvider(SMTP, SENDER)
    drain(database, preview, provider)
    number = row(database, case_id).number
    previews = mails(mailpit, number)
    assert len(previews) == 3
    assert {to["Address"] for mail in previews for to in mail["To"]} == {REDIRECT}
    assert row(database, case_id).state is CaseState.APPROVED

    live = notice_settings(test_settings, notices_dry_run=False, smtp_url=SMTP)
    with database.worker_session() as session:
        customer_id = session.get_one(Notice, notices_of(database, case_id)[0].id).customer_id
        session.get_one(Customer, customer_id).dry_run = False
    assert clear_blocked_cases(database, customer_id) == 1
    drain(database, live, provider)
    real = [mail for mail in mails(mailpit, number) if not mail["Subject"].startswith("[DRY RUN]")]
    assert sorted(to["Address"] for mail in real for to in mail["To"]) == [HOSTING, REGISTRAR]
    found = row(database, case_id)
    assert found.state is CaseState.NOTIFIED
    assert found.first_notice_at is not None
