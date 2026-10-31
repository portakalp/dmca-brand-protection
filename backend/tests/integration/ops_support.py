"""Helpers of the ops panel tests: sign-in through the pages and a captured case."""

from __future__ import annotations

import re
import uuid
from typing import Any

import pyotp
from fastapi.testclient import TestClient

from kalkan.config import REPO_ROOT, Settings
from kalkan.db.base import utcnow
from kalkan.db.models import Artifact, BrandAsset, Case, Customer, Domain, Scan
from kalkan.db.session import Database
from kalkan.domain.enums import ArtifactKind, BrandAssetKind, Profile, ScanStage
from kalkan.ops import auth
from kalkan.storage.gcs import ObjectStore
from tests import factories

PASSWORD = "correct horse battery"
EMAIL = "operator@kalkan.test"
SCREENSHOT = (REPO_ROOT / "fixtures" / "screenshots" / "clone-desktop.png").read_bytes()
CSRF = re.compile(r'name="csrf" value="([^"]+)"')


def csrf_of(html: str) -> str:
    found = CSRF.search(html)
    assert found is not None, "no CSRF field on the page"
    return found.group(1)


def password_step(client: TestClient, password: str = PASSWORD) -> Any:
    page = client.get("/login")
    return client.post(
        "/login",
        data={"email": EMAIL, "password": password, "csrf": csrf_of(page.text)},
        follow_redirects=False,
    )


def sign_in(client: TestClient, secret: str) -> str:
    """Signs in through both steps; returns the session's CSRF token."""
    assert password_step(client).headers["location"] == "/login/totp"
    page = client.get("/login/totp")
    done = client.post(
        "/login/totp",
        data={"code": pyotp.TOTP(secret).now(), "csrf": csrf_of(page.text)},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert done.headers["location"] == "/"
    return auth.csrf_for(client.cookies[auth.SESSION_COOKIE])


def captured_case(
    database: Database,
    store: ObjectStore,
    settings: Settings,
    *,
    owner: Customer | None = None,
    domain: str | None = None,
) -> Case:
    bucket = settings.gcs_evidence_bucket
    assert bucket is not None
    with database.worker_session() as session:
        customer = owner or factories.customer(session)
        brand = factories.brand(session, customer)
        case = factories.case(session, customer, brand)
        if domain:
            session.get_one(Domain, case.domain_id).name = domain
        scan = Scan(
            customer_id=customer.id,
            case_id=case.id,
            stage=ScanStage.BROWSER,
            profile=Profile.DESKTOP,
            outcome="ok",
            final_url=f"https://{domain or 'clone.test'}/",
            title="Mintera Bank | Login",
            started_at=utcnow(),
        )
        session.add(scan)
        session.flush()
        stored = store.put(
            bucket, f"cases/{case.id}/{scan.id}/screenshot-desktop.png", SCREENSHOT, "image/png"
        )
        session.add(
            Artifact(
                customer_id=customer.id,
                case_id=case.id,
                scan_id=scan.id,
                kind=ArtifactKind.SCREENSHOT,
                profile=Profile.DESKTOP,
                storage_key=stored.key,
                sha256=stored.sha256,
                bytes=stored.size,
                content_type="image/png",
            )
        )
        reference = store.put(
            bucket, f"brands/{brand.id}/refs/{uuid.uuid4()}/desktop.png", SCREENSHOT, "image/png"
        )
        session.add(
            BrandAsset(
                customer_id=customer.id,
                brand_id=brand.id,
                kind=BrandAssetKind.REFERENCE_DESKTOP,
                storage_key=reference.key,
                sha256=reference.sha256,
                captured_at=utcnow(),
            )
        )
    return case
