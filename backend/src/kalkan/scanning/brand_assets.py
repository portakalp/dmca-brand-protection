"""Official favicons of a brand (architecture.md §10, §11, §12).

For an official domain the home page is fetched through the SSRF-safe client, its icon
(``<link rel=icon>`` or ``/favicon.ico``) is stored once in the evidence bucket under
``brands/{brand_id}/assets/`` and recorded in ``brand_assets`` with its mmh3 hash, which the
pre-check compares with candidates' favicons. The asset id is derived from the brand and the
file's SHA-256, and the brand row is locked while it is recorded, so concurrent or repeated
jobs store an icon once (K72).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select

from kalkan.db.models import Brand, BrandAsset, OfficialDomain
from kalkan.db.session import Database
from kalkan.domain.enums import BrandAssetKind
from kalkan.logging import get_logger
from kalkan.net.safe_http import FetchError, SafeHttpClient
from kalkan.scanning.favicon import MAX_FAVICON_BYTES, favicon_hash, image_type
from kalkan.scanning.html import decode, parse_page
from kalkan.scanning.precheck import request_headers
from kalkan.storage import paths
from kalkan.storage.gcs import ObjectExistsError, ObjectStore, make_key

# One official domain's page and icon requests share this budget.
DOMAIN_SECONDS = 30.0

log = get_logger(__name__)


class UnknownBrandError(LookupError):
    pass


@dataclass(frozen=True)
class Favicon:
    domain: str
    url: str
    data: bytes
    extension: str
    content_type: str


def fetch_official_favicon(
    client: SafeHttpClient, domain: str, markets: list[str]
) -> Favicon | None:
    deadline = client.deadline(DOMAIN_SECONDS)
    headers = request_headers(markets)
    candidates: list[str] = []
    for start in (f"https://{domain}/", f"http://{domain}/"):
        try:
            page = client.get(start, headers, deadline=deadline)
        except FetchError:
            continue
        parsed = parse_page(decode(page.body, page.headers.get("content-type")), page.url)
        parts = urlsplit(page.url)
        fallback = urlunsplit((parts.scheme, parts.netloc, "/favicon.ico", "", ""))
        candidates = [*parsed.icons[:3], fallback]
        break
    for url in candidates:
        try:
            response = client.get(url, headers, deadline=deadline)
        except FetchError:
            continue
        if response.status != 200 or not response.body or len(response.body) > MAX_FAVICON_BYTES:
            continue
        kind = image_type(response.body)
        if kind is not None:
            return Favicon(domain, response.url, response.body, *kind)
    return None


def refresh_brand_favicons(
    database: Database,
    client: SafeHttpClient,
    store: ObjectStore,
    bucket: str,
    brand_id: uuid.UUID,
    *,
    now: Callable[[], datetime],
    domains: Sequence[str] | None = None,
) -> int:
    """Store the brand's official favicons it does not have yet; returns how many were added.

    ``domains`` limits the work to some of the brand's official domains; by default all.
    """
    with database.worker_session() as session:
        brand = session.get(Brand, brand_id)
        if brand is None:
            raise UnknownBrandError(f"no brand with id {brand_id}")
        markets = list(brand.target_markets)
        official = set(
            session.scalars(
                select(OfficialDomain.domain).where(OfficialDomain.brand_id == brand_id)
            )
        )
    wanted = sorted(official if domains is None else official & set(domains))
    added = 0
    for domain in wanted:
        icon = fetch_official_favicon(client, domain, markets)
        if icon is None:
            log.info("brand.favicon_missing", brand_id=str(brand_id), domain=domain)
            continue
        if _store(database, store, bucket, brand_id, icon, now()):
            added += 1
    return added


def _store(
    database: Database,
    store: ObjectStore,
    bucket: str,
    brand_id: uuid.UUID,
    icon: Favicon,
    at: datetime,
) -> bool:
    digest = hashlib.sha256(icon.data).hexdigest()
    asset_id = uuid.uuid5(brand_id, f"favicon:{digest}")
    path = paths.brand_asset(brand_id, asset_id, icon.extension)
    with database.worker_session() as session:
        brand = session.get(Brand, brand_id, with_for_update={"key_share": True})
        if brand is None:
            return False
        known = session.scalar(
            select(BrandAsset.id).where(
                BrandAsset.brand_id == brand_id,
                BrandAsset.kind == BrandAssetKind.FAVICON,
                BrandAsset.sha256 == digest,
            )
        )
        if known is not None:
            return False
        try:
            key = store.put(bucket, path, icon.data, icon.content_type).key
        except ObjectExistsError:  # an earlier attempt stored it, then failed
            key = make_key(bucket, path)
            if store.stored_sha256(key) != digest:
                raise
        session.add(
            BrandAsset(
                id=asset_id,
                customer_id=brand.customer_id,
                brand_id=brand_id,
                kind=BrandAssetKind.FAVICON,
                storage_key=key,
                sha256=digest,
                favicon_mmh3=favicon_hash(icon.data),
                source_url=icon.url,
                captured_at=at,
            )
        )
    return True
