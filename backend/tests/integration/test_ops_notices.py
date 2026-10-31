"""The ops panel's outbox, notice preview, manual tasks and re-check, replies and
follow-up tasks."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from kalkan.db.models import AuditLog, JobOutbox, Message, Notice, ReviewTask, WebhookEvent
from kalkan.db.outbox import CLASSIFY_REPLY
from kalkan.db.settings_store import write_setting
from kalkan.domain.enums import (
    Channel,
    NoticeStatus,
    ReplyClass,
    TaskKind,
    TaskStatus,
    WebhookSource,
)
from kalkan.followup.webhooks import UNMATCHED
from kalkan.ops.web import Services
from tests.integration.test_notices import (
    FakeProvider,
    approved_case,
    notice_settings,
    notices_of,
    plan_and_send,
)
from tests.unit.test_replies import postmark


def test_outbox_preview_and_dry_run_tasks(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, csrf = signed_in
    settings = notice_settings(ops_services.settings)
    case = approved_case(ops_services.database, ops_services.store, settings)
    plan_and_send(ops_services.database, settings, FakeProvider(), case)

    outbox = client.get("/outbox")
    assert outbox.status_code == 200
    assert case.number in outbox.text
    assert "dry run" in outbox.text
    assert "(preview sent)" in outbox.text
    assert client.get("/outbox?status=blocked").status_code == 200
    registrar = next(
        n for n in notices_of(ops_services.database, case.id) if n.channel is Channel.REGISTRAR
    )
    preview = client.get(f"/outbox/{registrar.id}")
    assert registrar.subject is not None
    assert registrar.subject in preview.text
    assert "Reported URL" in preview.text

    tasks = client.get("/tasks")
    assert "dry run" in tasks.text
    assert "disabled" in tasks.text
    with ops_services.database.worker_session() as session:
        task = session.scalars(select(ReviewTask).where(ReviewTask.case_id == case.id)).one()
    refused = client.post(
        f"/tasks/{task.id}/submitted", data={"csrf": csrf}, follow_redirects=False
    )
    assert refused.status_code == 303
    with ops_services.database.worker_session() as session:
        assert session.get_one(ReviewTask, task.id).status is TaskStatus.OPEN


def test_submitting_a_real_web_form_marks_the_notice_sent(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, csrf = signed_in
    settings = notice_settings(ops_services.settings, notices_dry_run=False)
    with ops_services.database.worker_session() as session:
        write_setting(session, "sending_enabled", True, actor="test")
    case = approved_case(
        ops_services.database,
        ops_services.store,
        settings,
        dry_run=False,
        loa_signed_at=datetime.now(UTC),
    )
    with ops_services.database.worker_session() as session:
        google = Notice(
            customer_id=case.customer_id,
            case_id=case.id,
            channel=Channel.GOOGLE,
            method="manual_form",
            recipient="https://safebrowsing.google.com/safebrowsing/report_phish/",
            template="phishing",
            dry_run=False,
            status=NoticeStatus.MANUAL_PENDING,
            subject="[X] Phishing report",
            body="Body",
        )
        session.add(google)
        session.flush()
        session.add(
            ReviewTask(
                customer_id=case.customer_id,
                case_id=case.id,
                notice_id=google.id,
                kind="manual_channel",
                reason="Submit the google report",
                status=TaskStatus.OPEN,
            )
        )
    with ops_services.database.worker_session() as session:
        task = session.scalars(select(ReviewTask).where(ReviewTask.case_id == case.id)).one()
    page = client.get("/tasks")
    assert 'data-copy="body-' in page.text
    done = client.post(f"/tasks/{task.id}/submitted", data={"csrf": csrf}, follow_redirects=False)
    assert done.status_code == 303
    with ops_services.database.worker_session() as session:
        notice = session.get_one(Notice, google.id)
        assert notice.status is NoticeStatus.SENT
        assert session.get_one(ReviewTask, task.id).status is TaskStatus.DONE
        actions = list(session.scalars(select(AuditLog.action)))
    assert "task.submitted" in actions


def test_recheck_blocked_cases_needs_confirmation_and_is_audited(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, csrf = signed_in
    unconfirmed = client.post(
        "/settings/recheck-blocked", data={"csrf": csrf}, follow_redirects=False
    )
    assert unconfirmed.status_code == 303
    with ops_services.database.worker_session() as session:
        assert session.scalars(select(JobOutbox)).all() == []
    client.post(
        "/settings/recheck-blocked", data={"csrf": csrf, "confirm": "yes"}, follow_redirects=False
    )
    with ops_services.database.worker_session() as session:
        assert [job.kind for job in session.scalars(select(JobOutbox))] == ["clear_blocked_cases"]
        assert "notices.recheck_blocked" in list(session.scalars(select(AuditLog.action)))


# --- replies and follow-up tasks -----------------------------------------------------------------


def _unmatched_reply(services: Services) -> WebhookEvent:
    payload = postmark("inbound_needs_info.json", number="nothing", number_upper="no number")
    payload["Subject"] = "<script>alert(1)</script> about your report"
    with services.database.worker_session() as session:
        event = WebhookEvent(
            source=WebhookSource.POSTMARK_INBOUND,
            payload=payload,
            signature_ok=True,
            processed_at=datetime.now(UTC),
            error=UNMATCHED,
        )
        session.add(event)
        session.flush()
        session.expunge(event)
        return event


def test_an_unmatched_reply_is_linked_and_its_class_corrected(
    signed_in: tuple[TestClient, str], ops_services: Services
) -> None:
    client, csrf = signed_in
    settings = notice_settings(ops_services.settings)
    case = approved_case(ops_services.database, ops_services.store, settings)
    event = _unmatched_reply(ops_services)

    page = client.get("/replies")
    assert page.status_code == 200
    assert "statement of authority" in page.text
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text

    unknown = client.post(f"/replies/{event.id}/link", data={"csrf": csrf, "number": "KLK-999999"})
    assert "no case with that number" in unknown.text
    linked = client.post(f"/replies/{event.id}/link", data={"csrf": csrf, "number": case.number})
    assert linked.status_code == 200
    with ops_services.database.worker_session() as session:
        message = session.get_one(Message, event.id)
        assert message.case_id == case.id
        assert session.get_one(WebhookEvent, event.id).error is None
        [job] = session.scalars(select(JobOutbox).where(JobOutbox.kind == CLASSIFY_REPLY)).all()
        assert job.payload["message_id"] == str(event.id)
    again = client.post(f"/replies/{event.id}/link", data={"csrf": csrf, "number": case.number})
    assert "already linked" in again.text

    corrected = client.post(
        f"/messages/{event.id}/classification",
        data={"csrf": csrf, "classification": "needs_info"},
    )
    assert "Classified as needs info" in corrected.text
    with ops_services.database.worker_session() as session:
        message = session.get_one(Message, event.id)
        assert message.classification is ReplyClass.NEEDS_INFO
        task = session.scalars(
            select(ReviewTask).where(
                ReviewTask.case_id == case.id, ReviewTask.kind == TaskKind.NEEDS_INFO
            )
        ).one()
        actions = {row.action for row in session.scalars(select(AuditLog))}
    assert {"reply.linked", "reply.classified"} <= actions

    # The task list filters by kind and closes a reply task with "Done".
    listed = client.get("/tasks?kind=needs_info")
    assert "asks for more information" in listed.text
    assert ">Done<" in listed.text
    done = client.post(f"/tasks/{task.id}/submitted", data={"csrf": csrf})
    assert "Marked as done." in done.text
    with ops_services.database.worker_session() as session:
        assert session.get_one(ReviewTask, task.id).status is TaskStatus.DONE
