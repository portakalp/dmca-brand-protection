from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import redis
from alembic import command
from alembic.config import Config
from dramatiq.brokers.redis import RedisBroker
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsError
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from kalkan.admin import create_staff_user
from kalkan.config import Settings, get_settings
from kalkan.db.engine import create_db_engine
from kalkan.db.models import GLOBAL_TABLES, TENANT_TABLES
from kalkan.db.session import Database
from kalkan.db.settings_store import SPECS
from kalkan.ops.app import create_app
from kalkan.ops.web import Services
from kalkan.storage.gcs import ObjectStore
from kalkan.workers.broker import setup_broker
from tests.integration.ops_support import EMAIL, PASSWORD, sign_in
from tests.support import redis_url_for_tests

SERVICES_HINT = "start the services with `make up-infra` (and create .env from .env.example)"
TEST_DATABASE = "kalkan_test"
# Separate buckets so tests never write into the development storage.
TEST_EVIDENCE_BUCKET = "kalkan-test-evidence"
TEST_REPORTS_BUCKET = "kalkan-test-reports"
BACKEND_DIR = Path(__file__).resolve().parents[2]
# Rows the migration inserts; they stay between tests.
KEPT_TABLES = {"settings"}


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Settings of the development stack (.env); the dev database is only used read-only."""
    try:
        settings = get_settings()
    except ValidationError as exc:
        pytest.fail(f"invalid configuration ({exc.error_count()} errors); {SERVICES_HINT}")
    except SettingsError as exc:
        pytest.fail(f"invalid configuration ({exc}); {SERVICES_HINT}")
    missing = settings.missing_for("api") + settings.missing_for("worker-default")
    if settings.database_url_migrate is None:
        missing.append("DATABASE_URL_MIGRATE")
    if missing:
        pytest.fail(f"missing settings {missing}; {SERVICES_HINT}")
    return settings


def _engine(url: SecretStr | None) -> Iterator[Engine]:
    assert url is not None  # checked by the settings fixture
    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        engine.dispose()
        pytest.fail(f"cannot connect to Postgres ({type(exc).__name__}); {SERVICES_HINT}")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def api_engine(settings: Settings) -> Iterator[Engine]:
    yield from _engine(settings.database_url_api)


@pytest.fixture(scope="session")
def worker_engine(settings: Settings) -> Iterator[Engine]:
    yield from _engine(settings.database_url_worker)


@pytest.fixture(scope="session")
def owner_engine(settings: Settings) -> Iterator[Engine]:
    yield from _engine(settings.database_url_migrate)


@pytest.fixture
def redis_broker() -> Iterator[RedisBroker]:
    """The test broker on its own Redis database, emptied before and after each test."""
    url = redis_url_for_tests()
    broker = setup_broker(url, prometheus=False)
    try:
        redis.Redis.from_url(url).ping()
    except redis.ConnectionError:
        pytest.fail(f"cannot connect to Redis; {SERVICES_HINT}")
    broker.flush_all()
    yield broker
    broker.flush_all()


# --------------------------------------------------------------------------------------------
# Test database (kalkan_test): migrated from scratch once per session, emptied after each test
# --------------------------------------------------------------------------------------------


def _test_url(url: SecretStr | None) -> SecretStr:
    assert url is not None
    parts = urlsplit(url.get_secret_value())
    return SecretStr(urlunsplit(parts._replace(path=f"/{TEST_DATABASE}")))


@pytest.fixture(scope="session")
def test_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "database_url_api": _test_url(settings.database_url_api),
            "database_url_worker": _test_url(settings.database_url_worker),
            "database_url_migrate": _test_url(settings.database_url_migrate),
            "gcs_evidence_bucket": TEST_EVIDENCE_BUCKET,
            "gcs_reports_bucket": TEST_REPORTS_BUCKET,
        }
    )


def alembic_config(owner: Engine) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["engine"] = owner
    return config


def run_alembic(owner: Engine, action: str, revision: str) -> None:
    config = alembic_config(owner)
    with owner.begin() as connection:
        config.attributes["connection"] = connection
        getattr(command, action)(config, revision)


@pytest.fixture(scope="session")
def test_owner_engine(test_settings: Settings) -> Iterator[Engine]:
    url = test_settings.database_url_migrate
    assert url is not None
    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        engine.dispose()
        pytest.fail(
            f"cannot connect to the {TEST_DATABASE} database ({type(exc).__name__}). It is "
            "created when the postgres volume is first initialised; recreate it with: "
            "make down && docker volume rm kalkan_postgres-data && make up"
        )
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def migrated(test_owner_engine: Engine) -> Engine:
    """kalkan_test at head, rebuilt from an empty schema for every test session."""
    run_alembic(test_owner_engine, "downgrade", "base")
    run_alembic(test_owner_engine, "upgrade", "head")
    return test_owner_engine


@pytest.fixture
def database(migrated: Engine, test_settings: Settings) -> Iterator[Database]:
    db = Database(test_settings)
    yield db
    db.dispose()
    tables = [name for name in (*TENANT_TABLES, *GLOBAL_TABLES) if name not in KEPT_TABLES]
    with migrated.begin() as connection:
        connection.execute(text(f"TRUNCATE {', '.join(tables)}, customers CASCADE"))
        # The settings rows stay (the migration wrote them); a test's changes do not.
        for key, spec in SPECS.items():
            connection.execute(
                text("UPDATE settings SET value = CAST(:value AS jsonb) WHERE key = :key"),
                {"key": key, "value": json.dumps(spec.default)},
            )


@pytest.fixture(scope="session")
def object_store(test_settings: Settings) -> Iterator[ObjectStore]:
    """The local storage emulator with the test buckets; emptied when the session ends."""
    store = ObjectStore.from_settings(test_settings)
    store.ensure_buckets(test_settings, TEST_EVIDENCE_BUCKET, TEST_REPORTS_BUCKET)
    yield store
    client = store._client
    for bucket in (TEST_EVIDENCE_BUCKET, TEST_REPORTS_BUCKET):
        for blob in client.list_blobs(bucket):
            blob.delete()


# --------------------------------------------------------------------------------------------
# The ops panel: its services on the test database, storage and Redis database
# --------------------------------------------------------------------------------------------


@pytest.fixture
def ops_services(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> Iterator[Services]:
    client = redis.Redis.from_url(redis_url_for_tests())
    client.flushdb()
    yield Services(test_settings, database, object_store, client)
    client.flushdb()


@pytest.fixture
def ops_client(ops_services: Services) -> Iterator[TestClient]:
    """Over https: the panel's cookies are Secure."""
    app = create_app(ops_services.settings, ops_services)
    with TestClient(app, base_url="https://testserver") as client:
        yield client


@pytest.fixture
def totp_secret(ops_services: Services) -> str:
    """A staff user (ops_support.EMAIL, PASSWORD); returns the TOTP secret."""
    with ops_services.database.worker_session() as session:
        created = create_staff_user(
            session, ops_services.settings, email=EMAIL, password=PASSWORD, actor="test"
        )
    return created.totp_secret


@pytest.fixture
def signed_in(ops_client: TestClient, totp_secret: str) -> tuple[TestClient, str]:
    """A signed-in client and the session's CSRF token."""
    return ops_client, sign_in(ops_client, totp_secret)
