"""The brand block's official images and the Files API (architecture.md §13).

The brand block shows the newest light and dark logos and the newest desktop and mobile
reference screenshots of the official site. Each is uploaded to the Files API once and called
by its ``file_id`` (``brand_assets.anthropic_file_id``). Brand assets are write-once rows: a
new logo or a new reference capture is a new row, so a changed image is uploaded again on its
first use. An image Claude cannot read (SVG) is left out; ICO and other Pillow formats are
sent as PNG. When an upload fails, the image goes inline for this call and the upload is tried
again next time.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import anthropic
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from kalkan.ai.images import PreparedImage, prepare
from kalkan.ai.prompting import BrandImage
from kalkan.db.models import BrandAsset, WhitelistEntry
from kalkan.db.session import Database
from kalkan.domain.enums import BrandAssetKind
from kalkan.logging import get_logger
from kalkan.storage.gcs import ObjectStore

BRAND_IMAGES: tuple[tuple[BrandAssetKind, str], ...] = (
    (BrandAssetKind.LOGO_LIGHT, "Official logo (light background)"),
    (BrandAssetKind.LOGO_DARK, "Official logo (dark background)"),
    (BrandAssetKind.REFERENCE_DESKTOP, "Official site, desktop"),
    (BrandAssetKind.REFERENCE_MOBILE, "Official site, mobile"),
)
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}

log = get_logger(__name__)


@dataclass(frozen=True)
class StoredImage:
    asset_id: uuid.UUID
    kind: BrandAssetKind
    label: str
    storage_key: str
    file_id: str | None


def newest_assets(session: Session, brand_id: uuid.UUID) -> list[StoredImage]:
    """The newest asset of each kind in :data:`BRAND_IMAGES`, in that order."""
    found: list[StoredImage] = []
    for kind, label in BRAND_IMAGES:
        asset = session.scalar(
            select(BrandAsset)
            .where(BrandAsset.brand_id == brand_id, BrandAsset.kind == kind)
            .order_by(
                BrandAsset.captured_at.desc().nulls_last(),
                BrandAsset.created_at.desc(),
                BrandAsset.id,
            )
            .limit(1)
        )
        if asset is not None:
            found.append(
                StoredImage(asset.id, kind, label, asset.storage_key, asset.anthropic_file_id)
            )
    return found


def whitelist_summary(session: Session, customer_id: uuid.UUID, brand_id: uuid.UUID) -> list[str]:
    """``host (kind)`` for the customer's entries that cover this brand."""
    entries = session.scalars(
        select(WhitelistEntry)
        .where(
            WhitelistEntry.customer_id == customer_id,
            or_(WhitelistEntry.brand_id.is_(None), WhitelistEntry.brand_id == brand_id),
        )
        .order_by(WhitelistEntry.kind, WhitelistEntry.pattern)
    )
    return [f"{entry.pattern} ({entry.kind.value})" for entry in entries]


def read_image(store: ObjectStore, key: str) -> PreparedImage | None:
    try:
        data = store.read_bytes(key)
    except FileNotFoundError:
        log.warning("ai.image_missing", key=key)
        return None
    return prepare(data)


def _upload(api: anthropic.Anthropic, stored: StoredImage, image: PreparedImage) -> str | None:
    name = f"{stored.kind.value}-{stored.asset_id}.{EXTENSIONS[image.media_type]}"
    try:
        uploaded = api.files.upload(file=(name, image.data, image.media_type))
    except anthropic.APIError as exc:
        log.warning("ai.file_upload_failed", asset_id=str(stored.asset_id), error=str(exc)[:200])
        return None
    return uploaded.id


def brand_images(
    database: Database,
    store: ObjectStore,
    api: anthropic.Anthropic | None,
    stored: Sequence[StoredImage],
) -> list[BrandImage]:
    """The brand block's images; uploads what has no ``file_id`` yet (``api`` None: inline)."""
    images: list[BrandImage] = []
    for item in stored:
        if item.file_id is not None and api is not None:
            images.append(BrandImage(item.label, file_id=item.file_id))
            continue
        prepared = read_image(store, item.storage_key)
        if prepared is None:
            log.info("ai.brand_image_skipped", asset_id=str(item.asset_id), kind=item.kind.value)
            continue
        file_id = _upload(api, item, prepared) if api is not None else None
        if file_id is None:
            images.append(BrandImage(item.label, image=prepared))
            continue
        with database.worker_session() as session:
            session.execute(
                update(BrandAsset)
                .where(BrandAsset.id == item.asset_id, BrandAsset.anthropic_file_id.is_(None))
                .values(anthropic_file_id=file_id)
            )
        log.info("ai.file_uploaded", asset_id=str(item.asset_id), kind=item.kind.value)
        images.append(BrandImage(item.label, file_id=file_id))
    return images
