"""The customer API against the test database: an app, accounts and sign-in."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pyotp
import pytest
from fastapi.testclient import TestClient

from kalkan.api.app import create_app
from kalkan.config import Settings
from kalkan.db.models import Brand, Case, Customer, Domain, OfficialDomain, User
from kalkan.db.session import Database
from kalkan.domain.case_state import transition
from kalkan.domain.enums import CaseState, Category, OfficialDomainKind, UserRole
from kalkan.email.provider import Email, Sent
from kalkan.security import encrypt_totp_secret, hash_password, new_totp_secret
from kalkan.storage.gcs import ObjectStore
from tests import factories

PASSWORD = "correct horse battery staple"
CSRF = {"X-Kalkan-CSRF": "1"}


@dataclass
class Outbox:
    sent: list[Email] = field(default_factory=list)

    def send(self, email: Email) -> Sent:
        self.sent.append(email)
        return Sent(f"<{uuid.uuid4().hex}@example.test>", None)


@dataclass
class Person:
    email: str
    password: str
    secret: str | None
    role: UserRole
    user_id: uuid.UUID

    def code(self) -> str:
        assert self.secret is not None
        return pyotp.TOTP(self.secret).now()


@dataclass
class Account:
    customer_id: uuid.UUID
    brand_id: uuid.UUID
    owner: Person
    viewer: Person
    case_id: uuid.UUID


def api_settings(test_settings: Settings) -> Settings:
    return test_settings.model_copy(
        update={
            "edge_shared_secret": None,
            "app_base_url": "https://app.example.test",
            "mail_from": "abuse-reports@example.test",
        }
    )


@pytest.fixture
def mailbox() -> Outbox:
    return Outbox()


@pytest.fixture
def api(
    test_settings: Settings, object_store: ObjectStore, mailbox: Outbox, database: Database
) -> Iterator[TestClient]:
    app = create_app(api_settings(test_settings))
    app.state.mailer = mailbox
    app.state.store = object_store
    with TestClient(app, headers=CSRF) as client:
        yield client


def person(
    database: Database,
    settings: Settings,
    customer_id: uuid.UUID,
    role: UserRole,
    *,
    totp: bool = True,
    email: str | None = None,
) -> Person:
    secret = new_totp_secret() if totp else None
    address = email or f"{role.value}-{uuid.uuid4().hex[:8]}@customer.example"
    with database.worker_session() as session:
        user = User(
            customer_id=customer_id,
            email=address,
            password_hash=hash_password(PASSWORD),
            role=role,
            totp_secret_enc=encrypt_totp_secret(settings, secret) if secret else None,
            totp_enabled_at=datetime.now(UTC) - timedelta(days=1) if secret else None,
        )
        session.add(user)
        session.flush()
        return Person(address, PASSWORD, secret, role, user.id)


def account(
    database: Database, settings: Settings, *, slug: str | None = None, **customer: Any
) -> Account:
    """A customer with a brand, an owner, a viewer and one verified case."""
    with database.worker_session() as session:
        owner_row = factories.customer(session, slug, **customer)
        brand = factories.brand(session, owner_row)
        # A plain row: add_official_domain would also generate the brand's permutations.
        session.add(
            OfficialDomain(
                customer_id=owner_row.id,
                brand_id=brand.id,
                domain=f"{owner_row.slug}-official.example",
                kind=OfficialDomainKind.PRIMARY,
            )
        )
        case = factories.case(session, owner_row, brand)
        session.get_one(Domain, case.domain_id).hosting_org = "Hosting Ltd"
        case.category = Category.PHISHING_CLONE
        transition(session, case, CaseState.VERIFIED, actor="test")
        customer_id, brand_id, case_id = owner_row.id, brand.id, case.id
    return Account(
        customer_id=customer_id,
        brand_id=brand_id,
        owner=person(database, settings, customer_id, UserRole.OWNER),
        viewer=person(database, settings, customer_id, UserRole.VIEWER),
        case_id=case_id,
    )


def sign_in(client: TestClient, who: Person) -> TestClient:
    """Password and TOTP; the client keeps the session cookie."""
    client.cookies.clear()
    first = client.post("/api/v1/auth/login", json={"email": who.email, "password": who.password})
    assert first.status_code == 200, first.text
    assert first.json() == {"mfa": "verify"}
    second = client.post("/api/v1/auth/mfa", json={"code": who.code()})
    assert second.status_code == 204, second.text
    return client


def case_row(database: Database, case_id: uuid.UUID) -> Case:
    with database.worker_session() as session:
        found = session.get_one(Case, case_id)
        session.expunge(found)
        return found


def customer_row(database: Database, customer_id: uuid.UUID) -> Customer:
    with database.worker_session() as session:
        found = session.get_one(Customer, customer_id)
        session.expunge(found)
        return found


def brand_row(database: Database, brand_id: uuid.UUID) -> Brand:
    with database.worker_session() as session:
        found = session.get_one(Brand, brand_id)
        session.expunge(found)
        return found
