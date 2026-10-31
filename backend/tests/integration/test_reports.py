"""Monthly reports against the database: who gets one and for which
month, the print token (single use, 60 minutes, one report, never logged), the hand-off to the
printer and back, the stored PDF and CSV, the owners' e-mail and the CSV columns.

The browser is replaced by writing a PDF into the hand-off folder; messages are recorded.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kalkan.config import Settings
from kalkan.db.models import Customer, JobOutbox, Report, User
from kalkan.db.outbox import BUILD_REPORT
from kalkan.db.session import Database
from kalkan.domain.enums import CaseState, CustomerStatus, UserRole
from kalkan.logging import configure_logging
from kalkan.reports import build, handoff, persist
from kalkan.reports.content import CSV_COLUMNS, month_period, previous_month
from kalkan.security import hash_password
from kalkan.seed.demo import seed_demo
from kalkan.storage.gcs import ObjectStore
from kalkan.workers import report_print
from tests import factories
from tests.integration.api_support import api, mailbox  # noqa: F401 - fixtures
from tests.integration.test_notices import FakeProvider
from tests.integration.test_seed import DESIGN_NOW

NOW = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
PDF = b"%PDF-1.7\n% a printed report\n"


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, str]]]:
    """Messages to the printer and back, by actor name."""
    messages: list[tuple[str, dict[str, str]]] = []

    def record(queue: str, actor: str, delay: timedelta | None = None, **kwargs: str) -> None:
        messages.append((actor, kwargs))

    monkeypatch.setattr(handoff, "_send", record)
    return messages


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "reports"


@pytest.fixture
def settings_(test_settings: Settings) -> Settings:
    return test_settings.model_copy(
        update={"app_base_url": "https://app.example.test", "mail_from": "r@example.test"}
    )


def customer(database: Database, slug: str, month: str = "2026-10", **values: Any) -> uuid.UUID:
    """A customer with an owner and three threats first seen on the 12th of ``month``."""
    year, number = (int(part) for part in month.split("-"))
    with database.worker_session() as session:
        row = factories.customer(session, slug, **values)
        session.add(
            User(
                customer_id=row.id,
                email=f"owner@{slug}.example",
                password_hash=hash_password("correct horse battery"),
                role=UserRole.OWNER,
            )
        )
        brand = factories.brand(session, row)
        for state in (CaseState.TAKEN_DOWN, CaseState.NOTIFIED, CaseState.WATCHING):
            case = factories.case(session, row, brand, state)
            case.first_seen_at = datetime(year, number, 12, 9, 0, tzinfo=UTC)
            if state is CaseState.TAKEN_DOWN:
                case.verified_at = datetime(year, number, 12, 10, 0, tzinfo=UTC)
                case.first_notice_at = datetime(year, number, 12, 11, 0, tzinfo=UTC)
                case.offline_since = datetime(year, number, 13, 17, 30, tzinfo=UTC)
        return row.id


def report_of(database: Database, report_id: uuid.UUID) -> Report:
    with database.worker_session() as session:
        found = session.get_one(Report, report_id)
        session.expunge(found)
        return found


def printed(report_id: uuid.UUID, root: Path, data: bytes = PDF) -> None:
    """What worker-browser leaves behind: the token read, the PDF written."""
    handoff.take_token(report_id, root)
    handoff.write_pdf(report_id, data, root)


# --- who and which month ----------------------------------------------------------------------


def test_last_month_is_reported_once_and_handed_to_the_printer(
    database: Database, root: Path, sent: list[tuple[str, dict[str, str]]]
) -> None:
    customer_id = customer(database, "mint-r1", timezone="Europe/Warsaw")
    built = build.build_report(database, customer_id, None, now=NOW, root=root)
    assert built is not None
    assert built.created
    report = report_of(database, built.report_id)
    assert report.period == "2026-10"
    assert report.print_token_expires_at == NOW + timedelta(minutes=60)
    assert sent == [("print_report", {"report_id": str(built.report_id), "attempt": "1"})]
    token_file = root / str(built.report_id) / handoff.TOKEN_FILE
    assert token_file.stat().st_mode & 0o777 == 0o600
    # The message carries no token; only its hash is stored.
    assert report.print_token_hash is not None
    assert report.print_token_hash not in json.dumps(sent)
    assert token_file.read_text() not in json.dumps(sent)

    again = build.build_report(database, customer_id, "2026-10", now=NOW, root=root)
    assert again is not None
    assert again.report_id == built.report_id
    assert not again.created


@pytest.mark.parametrize(
    ("values", "period", "error"),
    [
        ({"is_demo": True}, None, "the demo account"),
        ({"status": CustomerStatus.SUSPENDED}, None, "suspended"),
        ({}, "2026-11", "has not ended"),
        ({}, "2026-13", "YYYY-MM"),
    ],
)
def test_no_report_for_the_demo_a_suspended_customer_or_an_open_month(
    database: Database,
    root: Path,
    sent: list[tuple[str, dict[str, str]]],
    values: dict[str, Any],
    period: str | None,
    error: str,
) -> None:
    customer_id = customer(database, f"refused-{uuid.uuid4().hex[:6]}", **values)
    with pytest.raises(build.ReportError, match=error):
        build.build_report(database, customer_id, period, now=NOW, root=root)
    assert sent == []
    if values:
        assert customer_id not in build.reportable_customers(database)


def test_the_month_is_the_customers_own(database: Database, root: Path, sent: list[Any]) -> None:
    # 1 November, 00:30 in Warsaw is still 31 October in UTC: October has ended there.
    customer_id = customer(database, "mint-tz", timezone="Europe/Warsaw")
    early = datetime(2026, 10, 31, 23, 30, tzinfo=UTC)
    built = build.build_report(database, customer_id, None, now=early, root=root)
    assert built is not None
    assert report_of(database, built.report_id).period == "2026-10"


# --- printed, stored, e-mailed ----------------------------------------------------------------


def test_a_printed_report_is_stored_with_its_csv_and_the_owners_are_told(
    database: Database,
    object_store: ObjectStore,
    settings_: Settings,
    root: Path,
    sent: list[tuple[str, dict[str, str]]],
) -> None:
    customer_id = customer(database, "mint-r2")
    built = build.build_report(database, customer_id, "2026-10", now=NOW, root=root)
    assert built is not None
    printed(built.report_id, root)
    outbox = FakeProvider()
    store_bucket = settings_.gcs_reports_bucket or ""

    def run() -> persist.Result:
        return persist.persist_report(
            database,
            settings_,
            object_store,
            store_bucket,
            lambda: outbox,
            built.report_id,
            now=NOW + timedelta(minutes=3),
            root=root,
        )

    result = run()
    assert result == persist.Result(persist.Persisted.STORED, 1)
    report = report_of(database, built.report_id)
    assert report.generated_at == NOW + timedelta(minutes=3)
    assert report.print_token_hash is None
    assert object_store.read_bytes(report.pdf_key or "") == PDF
    rows = list(csv.reader(io.StringIO(object_store.read_bytes(report.csv_key or "").decode())))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert sorted(row[3] for row in rows[1:]) == ["notice_sent", "taken_down", "watching"]
    down = next(row for row in rows[1:] if row[3] == "taken_down")
    assert down[7] == "30.5"  # first notice to takedown, hours
    assert not (root / str(built.report_id)).exists()

    (email,) = outbox.sent
    assert email.to == "owner@mint-r2.example"
    assert email.subject == "Your October 2026 brand protection report"
    assert "Taken down:            1" in email.text
    assert "https://app.example.test/reports" in email.text

    # A redelivery changes nothing and sends nothing.
    assert run().status is persist.Persisted.ALREADY
    assert len(outbox.sent) == 1
    # A month that has its PDF is not printed again.
    assert build.build_report(database, customer_id, "2026-10", now=NOW, root=root) is None


def test_a_failed_print_gets_a_new_token_until_the_attempts_run_out(
    database: Database, root: Path, sent: list[tuple[str, dict[str, str]]]
) -> None:
    customer_id = customer(database, "mint-r3")
    built = build.build_report(database, customer_id, "2026-10", now=NOW, root=root)
    assert built is not None
    first = report_of(database, built.report_id).print_token_hash
    assert build.print_failed(database, built.report_id, 1, now=NOW, root=root)
    second = report_of(database, built.report_id).print_token_hash
    assert second not in (None, first)
    assert sent[-1] == ("print_report", {"report_id": str(built.report_id), "attempt": "2"})
    assert build.print_failed(database, built.report_id, 2, now=NOW, root=root)
    assert not build.print_failed(database, built.report_id, 3, now=NOW, root=root)
    assert report_of(database, built.report_id).print_token_hash is None
    assert not (root / str(built.report_id)).exists()
    assert len(sent) == 3


def test_a_missing_or_broken_pdf_is_not_stored(
    database: Database,
    object_store: ObjectStore,
    settings_: Settings,
    root: Path,
    sent: list[Any],
) -> None:
    customer_id = customer(database, "mint-r4")
    built = build.build_report(database, customer_id, "2026-10", now=NOW, root=root)
    assert built is not None
    printed(built.report_id, root, b"<html>not a pdf</html>")
    result = persist.persist_report(
        database,
        settings_,
        object_store,
        settings_.gcs_reports_bucket or "",
        FakeProvider,
        built.report_id,
        now=NOW,
        root=root,
    )
    assert result.status is persist.Persisted.BAD_PDF
    assert report_of(database, built.report_id).pdf_key is None


def test_the_printer_hands_back_a_pdf_or_a_failure(
    database: Database, root: Path, sent: list[tuple[str, dict[str, str]]], monkeypatch: Any
) -> None:
    from kalkan.reports import printing

    customer_id = customer(database, "mint-r5")
    built = build.build_report(database, customer_id, "2026-10", now=NOW, root=root)
    assert built is not None
    report_id = str(built.report_id)
    seen: list[str] = []

    def fake_print(report: str, token: str, base_url: str) -> tuple[bytes, printing.Blocked]:
        seen.append(token)
        return PDF, printing.Blocked()

    monkeypatch.setattr(printing, "print_pdf", fake_print)
    report_print.handle(report_id, "1", base_url="https://app.example.test", root=root)
    assert sent[-1] == ("persist_report", {"report_id": report_id})
    assert handoff.read_pdf(built.report_id, root) == PDF
    assert not (root / report_id / handoff.TOKEN_FILE).exists()

    # The token is gone after one read: a redelivery cannot print again.
    report_print.handle(report_id, "1", base_url="https://app.example.test", root=root)
    assert sent[-1] == ("report_print_failed", {"report_id": report_id, "attempt": "1"})
    assert len(seen) == 1

    def broken(report: str, token: str, base_url: str) -> tuple[bytes, printing.Blocked]:
        raise RuntimeError("net::ERR_CONNECTION_REFUSED")

    build.print_failed(database, built.report_id, 1, now=NOW, root=root)
    monkeypatch.setattr(printing, "print_pdf", broken)
    report_print.handle(report_id, "2", base_url="https://app.example.test", root=root)
    assert sent[-1] == ("report_print_failed", {"report_id": report_id, "attempt": "2"})


# --- the print token on the API ---------------------------------------------------------------


def _token(root: Path, report_id: uuid.UUID) -> str:
    return (root / str(report_id) / handoff.TOKEN_FILE).read_text()


def test_the_token_works_once_for_its_report_within_60_minutes(
    api: TestClient,  # noqa: F811
    database: Database,
    root: Path,
    sent: list[Any],
) -> None:
    # The database checks the expiry against its own clock: real time here.
    now = datetime.now(UTC)
    month = previous_month(now, UTC)
    before = previous_month(now.replace(day=1) - timedelta(days=1), UTC)
    first = customer(database, "mint-t1", month)
    other = customer(database, "mint-t2", month)
    one = build.build_report(database, first, month, now=now, root=root)
    two = build.build_report(database, other, month, now=now, root=root)
    late = build.build_report(database, first, before, now=now - timedelta(minutes=61), root=root)
    assert one is not None
    assert two is not None
    assert late is not None
    client = TestClient(api.app)

    def data(report_id: uuid.UUID, token: str) -> Any:
        return client.get(f"/api/v1/reports/{report_id}/data", headers={"X-Report-Token": token})

    # Another report's token, a stale token, no token: refused.
    assert data(two.report_id, _token(root, one.report_id)).status_code == 401
    assert data(late.report_id, _token(root, late.report_id)).status_code == 401
    assert client.get(f"/api/v1/reports/{two.report_id}/data").status_code == 401

    token = _token(root, two.report_id)
    answer = data(two.report_id, token)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    period = month_period(month)
    assert body["overview"]["period"] == {
        "start": period.start.isoformat(),
        "end": period.end.isoformat(),
    }
    assert body["overview"]["kpis"]["detected"] == 3
    (closed,) = body["taken_down"]
    assert closed["takedown_hours"] == 30.5
    # Second use: refused.
    assert data(two.report_id, token).status_code == 401


def test_the_token_never_reaches_a_log_line(
    api: TestClient,  # noqa: F811
    database: Database,
    object_store: ObjectStore,
    settings_: Settings,
    root: Path,
    sent: list[Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: Any,
) -> None:
    from kalkan.reports import printing

    configure_logging("test", "DEBUG")
    now = datetime.now(UTC)
    month = previous_month(now, UTC)
    customer_id = customer(database, "mint-t3", month)
    built = build.build_report(database, customer_id, month, now=now, root=root)
    assert built is not None
    token = _token(root, built.report_id)
    client = TestClient(api.app)

    def fake_print(report: str, token: str, base_url: str) -> tuple[bytes, printing.Blocked]:
        client.get(f"/api/v1/reports/{report}/data", headers={"X-Report-Token": token})
        raise RuntimeError("the page answered 500")

    monkeypatch.setattr(printing, "print_pdf", fake_print)
    report_print.handle(str(built.report_id), "1", base_url="https://app.example.test", root=root)
    build.print_failed(database, built.report_id, 1, now=datetime.now(UTC), root=root)
    second = _token(root, built.report_id)
    printed(built.report_id, root)
    persist.persist_report(
        database,
        settings_,
        object_store,
        settings_.gcs_reports_bucket or "",
        FakeProvider,
        built.report_id,
        now=datetime.now(UTC),
        root=root,
    )
    output = capsys.readouterr()
    logged = output.out + output.err
    assert "report.print_failed" in logged
    assert "report.stored" in logged
    assert token not in logged
    assert second not in logged


# --- operator and demo ------------------------------------------------------------------------


def test_the_operator_queues_a_report_and_is_audited(database: Database) -> None:
    customer_id = customer(database, "mint-q1")
    with database.worker_session() as session:
        row = session.get_one(Customer, customer_id)
        assert build.queue_build(session, row, "2026-10", now=NOW, actor="ops@example.test") == (
            "2026-10"
        )
        with pytest.raises(build.ReportError, match="has not ended"):
            build.queue_build(session, row, "2026-11", now=NOW, actor="ops@example.test")
    with database.worker_session() as session:
        job = session.scalars(select(JobOutbox).where(JobOutbox.kind == BUILD_REPORT)).one()
        assert job.payload == {
            "customer_id": str(customer_id),
            "period": "2026-10",
            "actor": "ops@example.test",
        }


@pytest.fixture
def demo(
    database: Database, object_store: ObjectStore, test_settings: Settings, tmp_path: Path
) -> dict[str, Any]:
    credentials = tmp_path / "demo.json"
    seed_demo(database, object_store, test_settings, now=DESIGN_NOW, credentials_file=credentials)
    data: dict[str, Any] = json.loads(credentials.read_text())
    return data


def test_the_demo_has_last_months_csv_and_a_print_token(
    demo: dict[str, Any], database: Database, object_store: ObjectStore
) -> None:
    report = demo["report"]
    assert report["period"] == "2026-09"
    stored = report_of(database, uuid.UUID(report["id"]))
    assert stored.pdf_key is None
    rows = list(csv.reader(io.StringIO(object_store.read_bytes(stored.csv_key or "").decode())))
    assert tuple(rows[0]) == CSV_COLUMNS
    down = [row for row in rows[1:] if row[3] == "taken_down"]
    assert down
    assert all(row[6] and row[7] and row[8] for row in down)  # takedown time, hours, channel
    assert all(not row[6] and not row[8] for row in rows[1:] if row[3] != "taken_down")
    assert stored.print_token_hash is not None
