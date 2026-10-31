"""Operator CLI commands against the test database."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from kalkan import cli
from kalkan.config import Settings
from kalkan.db.models import (
    AuditLog,
    Brand,
    Customer,
    OfficialDomain,
    StaffUser,
    WhitelistEntry,
)
from kalkan.db.session import Database
from kalkan.domain.enums import CustomerStatus, WhitelistKind
from kalkan.security import decrypt_totp_secret, verify_password
from kalkan.storage.gcs import ObjectStore

runner = CliRunner()


@pytest.fixture(autouse=True)
def _use_test_database(
    monkeypatch: pytest.MonkeyPatch, database: Database, test_settings: Settings
) -> None:
    monkeypatch.setattr(cli, "get_database", lambda: database)
    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)


def _invoke(*args: str, prompt_input: str | None = None) -> str:
    result = runner.invoke(cli.app, list(args), input=prompt_input)
    assert result.exit_code == 0, result.output
    return result.output


def _customer(database: Database, slug: str) -> Customer:
    with database.worker_session() as session:
        return session.scalars(select(Customer).where(Customer.slug == slug)).one()


@pytest.mark.parametrize(
    "command",
    [
        ["--help"],
        ["customer", "create", "--help"],
        ["brand", "add", "--help"],
        ["official-domain", "add", "--help"],
        ["whitelist", "import", "--help"],
        ["staff", "create", "--help"],
        ["case", "show", "--help"],
        ["seed", "--help"],
        ["customer", "set-status", "--help"],
    ],
)
def test_every_command_has_help(command: list[str]) -> None:
    result = runner.invoke(cli.app, command)
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_customer_create_uses_safe_defaults(database: Database) -> None:
    output = _invoke("customer", "create", "PayLumo Wallet", "--timezone", "Europe/Warsaw")
    assert "paylumo-wallet" in output
    customer = _customer(database, "paylumo-wallet")
    assert customer.status is CustomerStatus.TRIAL
    assert customer.dry_run is True
    assert customer.sending_enabled is False
    assert customer.monitor_only is False
    assert customer.is_demo is False
    with database.worker_session() as session:
        actions = session.scalars(select(AuditLog.action)).all()
    assert actions == ["customer.create"]


def test_customer_create_flags_and_duplicates(database: Database) -> None:
    _invoke("customer", "create", "Watch Me", "--monitor-only", "--demo")
    customer = _customer(database, "watch-me")
    assert (customer.monitor_only, customer.is_demo) == (True, True)
    result = runner.invoke(cli.app, ["customer", "create", "Watch Me"])
    assert result.exit_code == 1
    assert "already taken" in result.output


def test_brand_official_domain_and_whitelist_flow(database: Database, tmp_path: Path) -> None:
    _invoke("customer", "create", "Mintera Bank", "--slug", "mintera")
    output = _invoke(
        "brand", "add", "Mintera Bank", "--customer", "mintera",
        "--keyword", "mintera", "--variant", "mint era", "--market", "pl", "--language", "pl",
    )  # fmt: skip
    with database.worker_session() as session:
        brand = session.scalars(select(Brand)).one()
    assert str(brand.id) in output
    assert brand.target_markets == ["PL"]

    _invoke("official-domain", "add", "Mintera.COM", "--brand", str(brand.id))
    _invoke("official-domain", "add", "mintera.com", "--brand", str(brand.id))  # idempotent
    with database.worker_session() as session:
        officials = session.scalars(select(OfficialDomain.domain)).all()
        listed = session.scalars(select(WhitelistEntry)).all()
    assert officials == ["mintera.com"]
    assert [(e.pattern, e.kind) for e in listed] == [("mintera.com", WhitelistKind.OFFICIAL)]

    bad = tmp_path / "bad.csv"
    bad.write_text(
        "pattern,kind,note\npartners.mintera-referrals.com,affiliate,ok\n"
        "https://evil.example/path,affiliate,url\nmirror.example,unknown,kind\n"
        "partners.mintera-referrals.com,affiliate,dup\n"
    )
    result = runner.invoke(cli.app, ["whitelist", "import", str(bad), "--customer", "mintera"])
    assert result.exit_code == 1
    assert "line 3:" in result.output
    assert "line 4:" in result.output
    assert "line 5:" in result.output
    assert "nothing imported" in result.output

    good = tmp_path / "good.csv"
    good.write_text(
        "pattern,kind,note\npartners.mintera-referrals.com,affiliate,ok\nmintera.com,official,\n"
        "reviews.example.org,,\n"
    )
    output = _invoke("whitelist", "import", str(good), "--customer", "mintera")
    assert "Imported 3; already listed 0." in output
    output = _invoke("whitelist", "import", str(good), "--customer", "mintera")
    assert "Imported 0; already listed 3." in output
    with database.worker_session() as session:
        kinds = {
            entry.pattern: entry.kind
            for entry in session.scalars(
                select(WhitelistEntry).where(WhitelistEntry.brand_id.is_(None))
            )
        }
    assert kinds["reviews.example.org"] is WhitelistKind.OTHER


def test_staff_create_shows_totp_once_and_stores_it_encrypted(
    database: Database, test_settings: Settings
) -> None:
    output = _invoke(
        "staff", "create", "ops@kalkan.example", prompt_input="long-password-123\n" * 2
    )
    secret = output.split("TOTP secret: ", 1)[1].split()[0]
    assert "otpauth://totp/Kalkan:ops%40kalkan.example" in output
    with database.worker_session() as session:
        user = session.scalars(select(StaffUser)).one()
    assert verify_password(user.password_hash, "long-password-123")
    assert secret not in user.totp_secret_enc
    assert decrypt_totp_secret(test_settings, user.totp_secret_enc) == secret
    assert "long-password-123" not in output


def test_staff_create_rejects_short_passwords(database: Database) -> None:
    result = runner.invoke(
        cli.app, ["staff", "create", "ops@kalkan.example"], input="short\nshort\n"
    )
    assert result.exit_code == 1
    assert "at least 12 characters" in result.output


def test_seed_and_case_show(
    database: Database, object_store: ObjectStore, tmp_path: Path, test_settings: Settings
) -> None:
    credentials = tmp_path / "demo.json"
    output = _invoke("seed", "--credentials-file", str(credentials))
    assert "236 cases" in output
    assert json.loads(credentials.read_text())["customer"] == "mintera-demo"
    assert "password" not in output.lower()

    with database.worker_session() as session:
        from kalkan.db.models import Case, Domain

        number = session.scalars(
            select(Case.number).join(Domain).where(Domain.name == "mintera-login.com")
        ).one()
    output = _invoke("case", "show", number.lower())
    assert "mintera-login.com" in output
    assert "notified (Notice sent)" in output
    assert "cloudflare  acknowledged" in output
    assert "notice_sent" in output

    missing = runner.invoke(cli.app, ["case", "show", "KLK-999999"])
    assert missing.exit_code == 1


def test_brand_keywords_are_checked_and_names_folded(database: Database) -> None:
    _invoke("customer", "create", "\u015eube Bankas\u0131")
    customer = _customer(database, "sube-bankasi")
    assert customer.name == "\u015eube Bankas\u0131"

    short = runner.invoke(
        cli.app, ["brand", "add", "Pay", "--customer", "sube-bankasi", "--keyword", "pay"]
    )
    assert short.exit_code == 1
    assert "at least 4 characters" in short.output
    derived = runner.invoke(cli.app, ["brand", "add", "Pay", "--customer", "sube-bankasi"])
    assert derived.exit_code == 1

    _invoke("brand", "add", "\u015eube Bankas\u0131", "--customer", "sube-bankasi")
    with database.worker_session() as session:
        assert session.scalars(select(Brand.keywords)).one() == ["subebankasi"]


def test_suspends_and_reactivates_a_customer(database: Database) -> None:
    _invoke("customer", "create", "Mintera Bank", "--slug", "mintera")
    assert _invoke("customer", "set-status", "mintera", "active").strip() == "mintera: active"
    assert (
        _invoke("customer", "set-status", "mintera", "active").strip()
        == "mintera: active (unchanged)"
    )
    assert _invoke("customer", "set-status", "mintera", "suspended").strip() == "mintera: suspended"
    assert _customer(database, "mintera").status is CustomerStatus.SUSPENDED
    unknown = runner.invoke(cli.app, ["customer", "set-status", "mintera", "past_due"])
    assert unknown.exit_code == 1
    assert "unknown status" in unknown.output
    assert _invoke("customer", "set-status", "mintera", "active").strip() == "mintera: active"
    with database.worker_session() as session:
        changes = session.scalars(
            select(AuditLog.data).where(AuditLog.action == "customer.status").order_by(AuditLog.at)
        ).all()
    assert [(row["old"], row["new"]) for row in changes] == [
        ("trial", "active"),
        ("active", "suspended"),
        ("suspended", "active"),
    ]
