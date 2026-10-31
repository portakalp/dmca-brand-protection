"""The local demo commands (``kalkan demo``) and the non-interactive ``staff create`` options."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyotp
import pytest
from typer.testing import CliRunner

from kalkan import cli
from kalkan.seed.local_demo import (
    DemoCredentialsError,
    DemoOperator,
    add_staff,
    current_code,
    load_credentials,
    totp_secret_for,
)
from tests.support import make_settings

runner = CliRunner()

OWNER_SECRET = pyotp.random_base32()
OPERATOR_SECRET = pyotp.random_base32()


@pytest.fixture(autouse=True)
def local_env(clean_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    clean_env.delenv("DEMO_CREDENTIALS_FILE", raising=False)
    clean_env.setattr(cli, "get_settings", lambda: make_settings(env="local"))
    # `demo code` waits for a fresh window near the end of one; never really sleep here.
    clean_env.setattr(cli.time, "sleep", lambda _seconds: None)
    return clean_env


@pytest.fixture
def credentials(tmp_path: Path) -> Path:
    path = tmp_path / "demo-credentials.json"
    data: dict[str, Any] = {
        "customer": "mintera-demo",
        "users": [
            {
                "role": "owner",
                "email": "security@mintera.example",
                "password": "owner-password",
                "totp_secret": OWNER_SECRET,
                "recovery_codes": ["a-b"],
            }
        ],
        "staff": [
            {
                "role": "operator",
                "email": "operator@kalkan.local",
                "password": "operator-password",
                "totp_secret": OPERATOR_SECRET,
            }
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_current_code_matches_an_authenticator() -> None:
    at = datetime(2026, 10, 31, 12, 0, 10, tzinfo=UTC)
    code, remaining = current_code(OWNER_SECRET, at)
    assert code == pyotp.TOTP(OWNER_SECRET).at(at)
    assert remaining == 20


def test_demo_code_prints_the_current_code_alone(credentials: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["demo", "code", "Security@Mintera.example", "--credentials-file", str(credentials)],
    )
    assert result.exit_code == 0, result.output
    code = result.stdout.strip()
    assert pyotp.TOTP(OWNER_SECRET).verify(code, valid_window=1)
    assert "valid for" in result.stderr
    assert "owner-password" not in result.output


def test_demo_code_on_one_line(credentials: Path) -> None:
    result = runner.invoke(
        cli.app,
        [
            "demo",
            "code",
            "--one-line",
            "security@mintera.example",
            "--credentials-file",
            str(credentials),
        ],
    )
    assert result.exit_code == 0, result.output
    code, _, hint = result.stdout.strip().partition("  ")
    assert pyotp.TOTP(OWNER_SECRET).verify(code, valid_window=1)
    assert hint.startswith("(valid for ")
    assert result.stderr == "" or "waiting" in result.stderr


def test_demo_code_waits_for_a_fresh_window(
    credentials: Path, local_env: pytest.MonkeyPatch
) -> None:
    codes = iter([("111111", 2), ("222222", 30)])
    slept: list[float] = []
    local_env.setattr(cli, "current_code", lambda _secret: next(codes))
    local_env.setattr(cli.time, "sleep", slept.append)
    result = runner.invoke(
        cli.app,
        ["demo", "code", "security@mintera.example", "--credentials-file", str(credentials)],
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "222222"
    assert slept == [2]
    assert "waiting 2 s" in result.stderr


def test_demo_code_reads_operators_and_the_environment_path(
    credentials: Path, local_env: pytest.MonkeyPatch
) -> None:
    local_env.setenv("DEMO_CREDENTIALS_FILE", str(credentials))
    result = runner.invoke(cli.app, ["demo", "code", "operator@kalkan.local"])
    assert result.exit_code == 0, result.output
    assert pyotp.TOTP(OPERATOR_SECRET).verify(result.stdout.strip(), valid_window=1)


def test_demo_code_names_the_known_accounts(credentials: Path) -> None:
    result = runner.invoke(
        cli.app, ["demo", "code", "nobody@example.com", "--credentials-file", str(credentials)]
    )
    assert result.exit_code == 1
    assert "security@mintera.example" in result.output
    assert OWNER_SECRET not in result.output


def test_demo_code_without_a_credentials_file(tmp_path: Path) -> None:
    missing = tmp_path / "none.json"
    result = runner.invoke(
        cli.app, ["demo", "code", "a@b.example", "--credentials-file", str(missing)]
    )
    assert result.exit_code == 1
    assert "run the demo setup first" in result.output


@pytest.mark.parametrize(
    "command",
    [
        ["demo", "code", "security@mintera.example"],
        ["demo", "show"],
        ["demo", "setup"],
    ],
)
def test_demo_commands_refuse_prod(
    command: list[str], credentials: Path, local_env: pytest.MonkeyPatch
) -> None:
    local_env.setattr(cli, "get_settings", lambda: make_settings(env="prod"))
    local_env.setattr(cli, "seed_demo", _never)
    result = runner.invoke(cli.app, [*command, "--credentials-file", str(credentials)])
    assert result.exit_code == 1
    assert "refuse ENV=prod" in result.output
    assert OWNER_SECRET not in result.output


def _never(*_: object, **__: object) -> None:
    raise AssertionError("must not run")


def test_demo_show_lists_every_login_with_its_otpauth_uri(credentials: Path) -> None:
    result = runner.invoke(cli.app, ["demo", "show", "--credentials-file", str(credentials)])
    assert result.exit_code == 0, result.output
    assert "panel (owner): security@mintera.example" in result.output
    assert "ops panel (operator): operator@kalkan.local" in result.output
    assert "owner-password" in result.output
    assert f"secret={OPERATOR_SECRET}" in result.output


def test_add_staff_replaces_the_entry_and_keeps_the_file_private(credentials: Path) -> None:
    add_staff(credentials, DemoOperator("operator@kalkan.local", "new-pw", "NEWSECRET", False))
    data = load_credentials(credentials)
    assert [entry["password"] for entry in data["staff"]] == ["new-pw"]
    assert data["users"][0]["email"] == "security@mintera.example"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert totp_secret_for(data, "OPERATOR@kalkan.local") == "NEWSECRET"
    assert list(credentials.parent.iterdir()) == [credentials]


def test_load_credentials_refuses_other_json(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(DemoCredentialsError):
        load_credentials(path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(DemoCredentialsError, match="cannot read"):
        load_credentials(path)


def test_staff_create_password_options_exclude_each_other() -> None:
    result = runner.invoke(
        cli.app,
        ["staff", "create", "ops@kalkan.example", "--password-stdin", "--generate-password"],
        input="long-password-123\n",
    )
    assert result.exit_code == 1
    assert "cannot be used together" in result.output


def test_seed_credentials_narrow_an_existing_wider_file(tmp_path: Path) -> None:
    from kalkan.seed import demo

    path = tmp_path / ".local" / "demo-credentials.json"
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)
    demo._write_credentials(path, {"users": []})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {"users": []}
