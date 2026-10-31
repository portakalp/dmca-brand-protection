from __future__ import annotations

import uuid

import pytest
from typer.testing import CliRunner

from kalkan import __version__, cli
from kalkan.config import Settings
from kalkan.seed.demo import DemoResult
from tests.support import make_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def _settings_from_environment_only(
    clean_env: pytest.MonkeyPatch,
) -> pytest.MonkeyPatch:
    """The CLI reads settings from the process environment only (not the repo's .env)."""

    def settings() -> Settings:
        return make_settings()

    clean_env.setattr(cli, "get_settings", settings)
    return clean_env


def test_help_lists_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.output
    assert "check-config" in result.output


def test_version(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("ENV", "local")
    clean_env.setenv("GIT_SHA", "abc1234")
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"kalkan {__version__} (abc1234)"


def test_version_without_configuration() -> None:
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"kalkan {__version__}"


def test_check_config_reports_invalid_values_without_printing_them(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("ENV", "staging")
    clean_env.setenv("PROXY_MAP", '{"pl": "http://user:topsecret@proxy:1"}')
    result = runner.invoke(cli.app, ["check-config"])
    assert result.exit_code == 2
    assert "ENV" in result.output
    assert "PROXY_MAP" in result.output
    assert "topsecret" not in result.output


def test_check_config_lists_missing_variables(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("ENV", "local")
    clean_env.setenv("DATABASE_URL_API", "postgresql+psycopg://kalkan_api:pw-123@db/kalkan")
    result = runner.invoke(cli.app, ["check-config"])
    assert result.exit_code == 1
    assert "api: ok" in result.output
    assert "worker-default: missing DATABASE_URL_WORKER, REDIS_URL" in result.output
    assert "pw-123" not in result.output


def test_check_config_single_service_ok(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("ENV", "local")
    clean_env.setenv("DATABASE_URL_API", "postgresql+psycopg://kalkan_api:pw@db/kalkan")
    result = runner.invoke(cli.app, ["check-config", "--service", "api"])
    assert result.exit_code == 0
    assert result.output.splitlines() == ["ENV=local", "api: ok"]


def test_check_config_unknown_service() -> None:
    result = runner.invoke(cli.app, ["check-config", "--service", "web"])
    assert result.exit_code == 2


def test_check_config_reports_malformed_json_without_printing_it(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("ENV", "local")
    clean_env.setenv("PROXY_MAP", '{"PL": "http://user:topsecret@proxy:1"')
    result = runner.invoke(cli.app, ["check-config"])
    assert result.exit_code == 2
    assert "proxy_map" in result.output
    assert "topsecret" not in result.output

    version = runner.invoke(cli.app, ["version"])
    assert version.exit_code == 0


def test_seed_print_credentials_in_prod(clean_env: pytest.MonkeyPatch) -> None:
    """Refreshing the prod demo: asks first, writes no file, shows the credentials once."""
    calls: list[object] = []

    def fake_seed(*_: object, credentials_file: object) -> DemoResult:
        calls.append(credentials_file)
        user = {
            "role": "owner",
            "email": "security@mintera.example",
            "password": "pw-1",
            "totp_secret": "SECRET",
            "recovery_codes": ["a", "b"],
        }
        return DemoResult(uuid.uuid4(), 3, None, {"users": [user]})

    clean_env.setattr(cli, "get_settings", lambda: make_settings(env="prod"))
    clean_env.setattr(cli, "get_database", lambda: None)
    clean_env.setattr(cli.ObjectStore, "from_settings", lambda _: None)
    clean_env.setattr(cli, "seed_demo", fake_seed)

    refused = runner.invoke(cli.app, ["seed", "--print-credentials"], input="n\n")
    assert refused.exit_code == 1
    assert calls == []

    result = runner.invoke(cli.app, ["seed", "--print-credentials", "--yes"])
    assert result.exit_code == 0, result.output
    assert calls == [None]
    assert "pw-1" in result.output
    assert "SECRET" in result.output
    assert "stored nowhere" in result.output
