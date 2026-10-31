from __future__ import annotations

import json
from decimal import Decimal
from ipaddress import ip_network

import pytest
from pydantic import ValidationError

from kalkan.config import REQUIRED_BY_SERVICE, ConfigError
from tests.support import make_settings

pytestmark = pytest.mark.usefixtures("clean_env")


def test_env_is_required() -> None:
    with pytest.raises(ValidationError, match="env"):
        make_settings()


def test_env_must_be_known() -> None:
    with pytest.raises(ValidationError):
        make_settings(env="staging")


def test_defaults_follow_architecture() -> None:
    settings = make_settings(env="local")
    assert settings.case_prefix == "KLK"
    assert settings.notices_dry_run is True
    assert settings.ai_triage_model == "claude-haiku-5-5"
    assert settings.ai_confirm_model == "claude-sonnet-5-5"
    assert settings.ai_monthly_budget_usd == Decimal(50)
    assert settings.proxy_default_country == "PL"
    assert settings.proxy_daily_gb_limit == 2
    assert settings.proxy_map == {}
    assert settings.czds_tlds == []
    assert settings.ssrf_allow_cidrs == []


def test_reads_environment_variables(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("ENV", "local")
    clean_env.setenv("REDIS_URL", "redis://example:6379/0")
    clean_env.setenv("CZDS_TLDS", "xyz, .Top ,online,")
    clean_env.setenv("PROXY_MAP", '{"PL": "http://user:pass@proxy:8080"}')
    clean_env.setenv("SSRF_ALLOW_CIDRS", "172.30.0.0/16, ::1/128")
    clean_env.setenv("NOTICES_DRY_RUN", "false")

    settings = make_settings()

    assert settings.redis_url == "redis://example:6379/0"
    assert settings.czds_tlds == ["xyz", "top", "online"]
    assert settings.proxy_map["PL"].get_secret_value() == "http://user:pass@proxy:8080"
    assert settings.ssrf_allow_cidrs == [ip_network("172.30.0.0/16"), ip_network("::1/128")]
    assert settings.notices_dry_run is False


def test_empty_values_are_treated_as_unset(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("ENV", "local")
    clean_env.setenv("EDGE_SHARED_SECRET", "")
    clean_env.setenv("SENTRY_DSN", "")
    settings = make_settings()
    assert settings.edge_shared_secret is None
    assert settings.sentry_dsn is None


def test_secrets_are_not_exposed_in_repr() -> None:
    settings = make_settings(
        env="local",
        database_url_api="postgresql+psycopg://kalkan_api:hunter2@db/kalkan",
        edge_shared_secret="edge-secret-value",
        proxy_map={"PL": "http://user:proxy-pass@proxy:8080"},
        smtp_url="smtps://user:smtp-pass@relay.example:465",
    )
    rendered = repr(settings) + str(settings.model_dump())
    for secret in ("hunter2", "edge-secret-value", "proxy-pass", "smtp-pass"):
        assert secret not in rendered


def test_worker_browser_needs_no_database_cloud_or_proxy_settings() -> None:
    # Without PROXY_MAP the worker starts and every capture job waits (fail closed, §7).
    settings = make_settings(env="prod", redis_url="redis://redis:6379/0")
    assert settings.missing_for("worker-browser") == []
    assert REQUIRED_BY_SERVICE["worker-browser"] == ("redis_url",)


def test_proxy_map_errors_never_show_the_value() -> None:
    with pytest.raises(ValidationError) as raised:
        make_settings(env="local", proxy_map={"pl": "http://user:topsecret@proxy:8080"})
    assert "topsecret" not in str(raised.value)
    with pytest.raises(ValidationError) as raised:
        make_settings(env="local", proxy_map={"PL": "http://user:topsecret@proxy:8080/x"})
    assert "PROXY_MAP[PL]" in str(raised.value)
    assert "topsecret" not in str(raised.value)


@pytest.mark.parametrize("key", ["pl", "POL", "P1"])
def test_proxy_map_keys_must_be_country_codes(key: str) -> None:
    with pytest.raises(ValidationError, match="PROXY_MAP"):
        make_settings(env="local", proxy_map={key: "http://proxy:8080"})


def test_case_prefix_is_validated() -> None:
    with pytest.raises(ValidationError):
        make_settings(env="local", case_prefix="klk-")


def test_prod_rejects_test_only_ssrf_allow_list() -> None:
    with pytest.raises(ValidationError, match="SSRF_ALLOW_CIDRS"):
        make_settings(env="prod", ssrf_allow_cidrs="10.0.0.0/8")


def test_prod_rejects_storage_emulator() -> None:
    with pytest.raises(ValidationError, match="STORAGE_EMULATOR_HOST"):
        make_settings(env="prod", storage_emulator_host="http://fake-gcs:4443")


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"edge_shared_secret": "change-me"}, "EDGE_SHARED_SECRET is still a placeholder"),
        ({"edge_shared_secret": "short-but-real"}, "at least 32 characters"),
        ({"totp_encryption_key": "change-me"}, "TOTP_ENCRYPTION_KEY"),
        (
            {"database_url_api": "postgresql+psycopg://kalkan_api:change-me@db/kalkan"},
            "placeholder password",
        ),
    ],
)
def test_prod_refuses_placeholder_secrets(values: dict[str, str], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(env="prod", **values)
    make_settings(env="local", **values)  # the local stack may use anything


def test_prod_accepts_a_real_edge_secret() -> None:
    settings = make_settings(env="prod", edge_shared_secret="a" * 64)
    assert settings.edge_shared_secret is not None


def test_local_allows_test_only_settings() -> None:
    settings = make_settings(
        env="local", ssrf_allow_cidrs="172.30.0.0/16", storage_emulator_host="http://x:4443"
    )
    assert settings.ssrf_allow_cidrs == [ip_network("172.30.0.0/16")]


@pytest.mark.parametrize(
    ("service", "missing"),
    [
        ("api", ["DATABASE_URL_API"]),
        ("worker-default", ["DATABASE_URL_WORKER", "REDIS_URL"]),
        ("scheduler", ["DATABASE_URL_WORKER", "REDIS_URL"]),
        ("ct-consumer", ["DATABASE_URL_WORKER", "REDIS_URL", "CERTSTREAM_URL"]),
        ("worker-browser", ["REDIS_URL"]),
        (
            "ops",
            ["DATABASE_URL_WORKER", "REDIS_URL", "TOTP_ENCRYPTION_KEY", "GCS_EVIDENCE_BUCKET"],
        ),
        (
            "worker-ai",
            ["DATABASE_URL_WORKER", "REDIS_URL", "ANTHROPIC_API_KEY", "GCS_EVIDENCE_BUCKET"],
        ),
    ],
)
def test_missing_for_lists_required_variables(service: str, missing: list[str]) -> None:
    settings = make_settings(env="local")
    assert settings.missing_for(service) == missing
    with pytest.raises(ConfigError, match=missing[0]):
        settings.require_for(service)


def test_every_service_passes_when_configured() -> None:
    settings = make_settings(
        env="local",
        database_url_api="postgresql+psycopg://a@db/k",
        database_url_worker="postgresql+psycopg://w@db/k",
        redis_url="redis://redis:6379/0",
        certstream_url="ws://certstream:8080/",
        totp_encryption_key="key",
        gcs_evidence_bucket="evidence",
        anthropic_api_key="sk-ant-test",
    )
    for service in REQUIRED_BY_SERVICE:
        settings.require_for(service)


def test_api_requires_edge_secret_in_prod() -> None:
    settings = make_settings(env="prod", database_url_api="postgresql+psycopg://a@db/k")
    assert settings.missing_for("api") == ["EDGE_SHARED_SECRET"]
    local = make_settings(env="local", database_url_api="postgresql+psycopg://a@db/k")
    assert local.missing_for("api") == []


def test_unknown_service_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown service"):
        make_settings(env="local").missing_for("web")


def test_ai_prices_default_to_the_architecture_table_and_cover_the_models() -> None:
    settings = make_settings(env="local")
    assert sorted(settings.ai_prices) == [
        "claude-haiku-4-5",
        "claude-haiku-5-5",
        "claude-sonnet-5-5",
    ]
    with pytest.raises(ValidationError, match="no price for claude-other"):
        make_settings(env="local", ai_triage_model="claude-other")


def test_ai_prices_are_read_as_json(clean_env: pytest.MonkeyPatch) -> None:
    rates = {"input": "3", "cache_write_5m": "3.75", "cache_write_1h": "6", "cache_read": "0.3"}
    rates |= {"output": "15", "batch_input": "1.5", "batch_output": "7.5"}
    table = dict.fromkeys(("claude-haiku-5-5", "claude-sonnet-5-5"), rates)
    clean_env.setenv("AI_PRICES", json.dumps(table))
    settings = make_settings(env="local")
    assert settings.ai_prices["claude-sonnet-5-5"].output == Decimal(15)
    assert "claude-haiku-4-5" not in settings.ai_prices
