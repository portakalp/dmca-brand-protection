"""Settings loaded from the environment (architecture.md §26).

Every variable in §26 has a field here. Only ``ENV`` is required for all processes; the
variables a process needs are checked at its start with :meth:`Settings.require_for`, so a
process never receives (or needs) credentials it does not use.
"""

from __future__ import annotations

import ipaddress
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from kalkan.ai.prices import Price, default_prices
from kalkan.scanning.proxy import ProxyTemplateError, validate_template

# backend/src/kalkan/config.py -> repository root. The file only exists in a source checkout
# (host tools and tests); containers get their configuration from the environment.
REPO_ROOT = Path(__file__).resolve().parents[3]
DOTENV_PATH = REPO_ROOT / ".env"

Environment = Literal["local", "prod"]
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Variables each long-running process needs before it starts (field names).
REQUIRED_BY_SERVICE: dict[str, tuple[str, ...]] = {
    "api": ("database_url_api",),
    "worker-default": ("database_url_worker", "redis_url"),
    "scheduler": ("database_url_worker", "redis_url"),
    "ct-consumer": ("database_url_worker", "redis_url", "certstream_url"),
    # No database and no cloud credentials (K6). PROXY_MAP is not required: without it
    # every capture job waits (fail closed, §7) instead of the worker failing to start.
    "worker-browser": ("redis_url",),
    # The ops panel: every tenant (kalkan_worker), queue depths, TOTP and uploads (§19).
    "ops": ("database_url_worker", "redis_url", "totp_encryption_key", "gcs_evidence_bucket"),
    # Claude calls: screenshots and brand assets are read from the evidence bucket (§13).
    "worker-ai": ("database_url_worker", "redis_url", "anthropic_api_key", "gcs_evidence_bucket"),
}
# Additional variables a process needs when ENV=prod.
# The placeholder .env.example files use; refused in secrets when ENV=prod.
PLACEHOLDER_PREFIX = "change-me"
MIN_EDGE_SECRET_LENGTH = 32

REQUIRED_IN_PROD: dict[str, tuple[str, ...]] = {
    "api": ("edge_shared_secret",),
}


class ConfigError(RuntimeError):
    """Raised when a process is missing required configuration."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=DOTENV_PATH,
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
        # Validation errors name the variable, never its value (PROXY_MAP holds credentials).
        hide_input_in_errors=True,
    )

    env: Environment
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    git_sha: str | None = None

    database_url_api: SecretStr | None = None
    database_url_worker: SecretStr | None = None
    database_url_migrate: SecretStr | None = None
    redis_url: str | None = None

    gcs_evidence_bucket: str | None = None
    gcs_reports_bucket: str | None = None
    storage_emulator_host: str | None = None
    # gcs: Google Cloud Storage (or fake-gcs via STORAGE_EMULATOR_HOST); filesystem: objects
    # under STORAGE_ROOT, for self-hosting without a cloud account (storage/filesystem.py).
    storage_backend: Literal["gcs", "filesystem"] = "gcs"
    storage_root: Path = Path("/data/objects")

    app_base_url: str | None = None
    # Where worker-browser opens the report print page when that differs from the public
    # address, e.g. the internal http://web:3000 of a self-hosted server; else APP_BASE_URL.
    report_print_base_url: str | None = None
    evidence_base_url: str | None = None
    edge_shared_secret: SecretStr | None = None
    totp_encryption_key: SecretStr | None = None
    case_prefix: str = Field(default="KLK", pattern=r"^[A-Z]{2,5}$")

    anthropic_api_key: SecretStr | None = None
    # Whether worker-default and the scheduler hand work to worker-ai (``use_ai``). They never
    # see ANTHROPIC_API_KEY, so compose tells them; unset means "this process has the key".
    ai_enabled: bool | None = None
    ai_triage_model: str = "claude-haiku-5-5"
    ai_confirm_model: str = "claude-sonnet-5-5"
    ai_text_model: str = "claude-sonnet-5-5"
    ai_reply_model: str = "claude-haiku-5-5"
    ai_monthly_budget_usd: Decimal = Field(default=Decimal(50), ge=0)
    # The alarm line of a day's AI spend (§23); the scheduler exports it next to the spend.
    alert_ai_daily_usd: Decimal = Field(default=Decimal(5), ge=0)
    ai_prices: dict[str, Price] = Field(default_factory=default_prices)

    proxy_map: dict[str, SecretStr] = Field(default_factory=dict)
    proxy_default_country: str | None = Field(default="PL", pattern=r"^[A-Z]{2}$")
    proxy_daily_gb_limit: float = Field(default=2, ge=0)

    # smtp (SMTP_URL) in every ENV; postmark needs POSTMARK_SERVER_TOKEN (email/provider.py).
    email_provider: Literal["smtp", "postmark"] = "smtp"
    postmark_server_token: SecretStr | None = None
    postmark_webhook_basic_auth: SecretStr | None = None
    smtp_url: SecretStr | None = None
    # With ENV=prod, plain smtp:// is accepted only for a relay on this host or network
    # (loopback, a private address or a compose service name) unless this is true.
    smtp_allow_plaintext: bool = False
    mail_from: str | None = None
    reply_domain: str | None = None
    notices_dry_run: bool = True
    dry_run_redirect_email: str | None = None
    ops_alert_email: str | None = None

    webrisk_api_key: SecretStr | None = None
    czds_username: SecretStr | None = None
    czds_password: SecretStr | None = None
    czds_tlds: Annotated[list[str], NoDecode] = Field(default_factory=list)
    certstream_url: str | None = None

    sentry_dsn: SecretStr | None = None
    ssrf_allow_cidrs: Annotated[list[IPNetwork], NoDecode] = Field(default_factory=list)

    @field_validator("czds_tlds", mode="before")
    @classmethod
    def _split_tlds(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [tld.strip().lower().lstrip(".") for tld in value.split(",") if tld.strip()]
        return value

    @field_validator("ssrf_allow_cidrs", mode="before")
    @classmethod
    def _split_cidrs(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [cidr.strip() for cidr in value.split(",") if cidr.strip()]
        return value

    @field_validator("storage_root")
    @classmethod
    def _absolute_storage_root(cls, value: Path) -> Path:
        # A relative root would move with each process's working directory.
        if not value.is_absolute():
            raise ValueError("STORAGE_ROOT must be an absolute path")
        return value

    @field_validator("proxy_map")
    @classmethod
    def _country_codes(cls, value: dict[str, SecretStr]) -> dict[str, SecretStr]:
        invalid = [key for key in value if len(key) != 2 or not key.isalpha() or not key.isupper()]
        if invalid:
            raise ValueError("PROXY_MAP keys must be ISO 3166-1 alpha-2 country codes")
        for country, template in value.items():
            try:
                validate_template(template.get_secret_value())
            except ProxyTemplateError as exc:
                # The message names the country only; the value holds credentials.
                raise ValueError(f"PROXY_MAP[{country}]: {exc}") from None
        return value

    @model_validator(mode="after")
    def _priced_models(self) -> Settings:
        # A call without a price could not be counted against the monthly budget (§13).
        models = (self.ai_triage_model, self.ai_confirm_model, self.ai_text_model)
        unpriced = sorted({m for m in (*models, self.ai_reply_model) if m not in self.ai_prices})
        if unpriced:
            raise ValueError(f"AI_PRICES has no price for {', '.join(unpriced)}")
        return self

    @model_validator(mode="after")
    def _local_only_settings(self) -> Settings:
        if self.env == "prod":
            if self.ssrf_allow_cidrs:
                raise ValueError("SSRF_ALLOW_CIDRS must be empty when ENV=prod")
            if self.storage_emulator_host:
                raise ValueError("STORAGE_EMULATOR_HOST must not be set when ENV=prod")
            self._refuse_placeholder_secrets()
        return self

    def _refuse_placeholder_secrets(self) -> None:
        """A copied .env.example (``change-me``) or a short edge secret must not reach prod."""
        named = {
            "EDGE_SHARED_SECRET": self.edge_shared_secret,
            "TOTP_ENCRYPTION_KEY": self.totp_encryption_key,
            "POSTMARK_WEBHOOK_BASIC_AUTH": self.postmark_webhook_basic_auth,
        }
        for name, secret in named.items():
            if secret is not None and PLACEHOLDER_PREFIX in secret.get_secret_value().lower():
                raise ValueError(f"{name} is still a placeholder; generate a real secret")
        edge = self.edge_shared_secret
        if edge is not None and len(edge.get_secret_value()) < MIN_EDGE_SECRET_LENGTH:
            raise ValueError(
                f"EDGE_SHARED_SECRET must have at least {MIN_EDGE_SECRET_LENGTH} characters"
                " (openssl rand -hex 32)"
            )
        for url in (self.database_url_api, self.database_url_worker, self.database_url_migrate):
            if url is not None and PLACEHOLDER_PREFIX in url.get_secret_value().lower():
                raise ValueError("a database URL still holds a placeholder password")

    @property
    def use_ai(self) -> bool:
        """Whether AI work is queued for worker-ai; otherwise it goes to an operator.

        ``AI_ENABLED`` wins when set; without it, a process uses AI when it has the key, so a
        setup that never configured Anthropic runs without it (``kalkan.ai.queue``).
        """
        if self.ai_enabled is not None:
            return self.ai_enabled
        return self.anthropic_api_key is not None

    def missing_for(self, service: str) -> list[str]:
        """Return the environment variable names ``service`` needs but are not set."""
        if service not in REQUIRED_BY_SERVICE:
            raise ValueError(f"unknown service: {service}")
        required = REQUIRED_BY_SERVICE[service]
        if self.env == "prod":
            required += REQUIRED_IN_PROD.get(service, ())
        return [name.upper() for name in required if getattr(self, name) is None]

    def require_for(self, service: str) -> None:
        missing = self.missing_for(service)
        if missing:
            raise ConfigError(f"{service} is missing required settings: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment and the .env file
