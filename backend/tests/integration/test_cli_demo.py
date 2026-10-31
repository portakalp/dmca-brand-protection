"""``kalkan demo setup`` and the non-interactive ``staff create`` against the test database."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pyotp
import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from kalkan import cli
from kalkan.config import Settings
from kalkan.db.base import utcnow
from kalkan.db.models import AuditLog, StaffSession, StaffUser
from kalkan.db.session import Database
from kalkan.security import decrypt_totp_secret, hash_token, verify_password
from kalkan.seed.local_demo import DEMO_OPERATOR_EMAIL

runner = CliRunner()


@pytest.fixture(autouse=True)
def _use_test_database(
    monkeypatch: pytest.MonkeyPatch, database: Database, test_settings: Settings
) -> None:
    monkeypatch.setattr(cli, "get_database", lambda: database)
    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)


def _staff(database: Database, email: str) -> StaffUser:
    with database.worker_session() as session:
        return session.scalars(select(StaffUser).where(StaffUser.email == email)).one()


def test_staff_create_reads_the_password_from_stdin(database: Database) -> None:
    result = runner.invoke(
        cli.app,
        ["staff", "create", "ops@kalkan.example", "--password-stdin"],
        input="long-password-123\nignored\n",
    )
    assert result.exit_code == 0, result.output
    assert "Password" not in result.output
    assert "long-password-123" not in result.output
    assert verify_password(
        _staff(database, "ops@kalkan.example").password_hash, "long-password-123"
    )


def test_staff_create_from_stdin_still_checks_the_length(database: Database) -> None:
    result = runner.invoke(
        cli.app, ["staff", "create", "ops@kalkan.example", "--password-stdin"], input="short\n"
    )
    assert result.exit_code == 1
    assert "at least 12 characters" in result.output


def test_staff_create_generates_a_password_and_shows_it_once(database: Database) -> None:
    result = runner.invoke(
        cli.app, ["staff", "create", "ops@kalkan.example", "--generate-password"]
    )
    assert result.exit_code == 0, result.output
    password = result.output.split("Password (shown once): ", 1)[1].split()[0]
    assert len(password) >= 12
    assert verify_password(_staff(database, "ops@kalkan.example").password_hash, password)


@pytest.mark.usefixtures("object_store")
def test_demo_setup_seeds_and_renews_the_operator(
    database: Database, tmp_path: Path, test_settings: Settings
) -> None:
    credentials = tmp_path / "local" / "demo-credentials.json"
    first = runner.invoke(cli.app, ["demo", "setup", "--credentials-file", str(credentials)])
    assert first.exit_code == 0, first.output
    assert "236 cases" in first.output
    data = json.loads(credentials.read_text())
    [operator] = data["staff"]
    assert operator["email"] == DEMO_OPERATOR_EMAIL
    assert [user["email"] for user in data["users"]] == [
        "security@mintera.example",
        "analyst@mintera.example",
    ]
    for entry in (*data["users"], operator):
        assert entry["password"] in first.output
        assert f"secret={entry['totp_secret']}" in first.output

    user = _staff(database, DEMO_OPERATOR_EMAIL)
    assert verify_password(user.password_hash, operator["password"])
    assert decrypt_totp_secret(test_settings, user.totp_secret_enc) == operator["totp_secret"]
    with database.worker_session() as session:
        session.add(
            StaffSession(
                token_hash=hash_token("old-session"),
                staff_user_id=user.id,
                expires_at=utcnow() + timedelta(hours=1),
            )
        )

    code = runner.invoke(
        cli.app, ["demo", "code", DEMO_OPERATOR_EMAIL, "--credentials-file", str(credentials)]
    )
    assert code.exit_code == 0, code.output
    assert pyotp.TOTP(operator["totp_secret"]).verify(code.stdout.strip(), valid_window=1)

    # A second run: the same account with new secrets; the old ones and their sessions end.
    second = runner.invoke(cli.app, ["demo", "setup", "--credentials-file", str(credentials)])
    assert second.exit_code == 0, second.output
    [renewed] = json.loads(credentials.read_text())["staff"]
    assert renewed["password"] != operator["password"]
    assert renewed["totp_secret"] != operator["totp_secret"]
    user = _staff(database, DEMO_OPERATOR_EMAIL)
    assert verify_password(user.password_hash, renewed["password"])
    assert not verify_password(user.password_hash, operator["password"])
    with database.worker_session() as session:
        assert session.scalar(select(func.count()).select_from(StaffUser)) == 1
        assert session.scalar(select(func.count()).select_from(StaffSession)) == 0
        actions = session.scalars(
            select(AuditLog.action)
            .where(AuditLog.target == DEMO_OPERATOR_EMAIL)
            .order_by(AuditLog.at)
        ).all()
    assert actions == ["staff.create", "staff.reset"]
