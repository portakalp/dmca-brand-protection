"""Perceptual hashes of a brand's assets (architecture.md §5, §12).

``hash_brand_assets`` fills ``brand_assets.phash`` and ``dhash`` for the brand's logos,
favicons and reference screenshots that have none yet. It runs when an asset is stored
(official favicons, reference screenshots; logo uploads in later prompts) and again at the
start of ``compute_signals``, so a case never waits for a hash job that has not run. Reference
screenshots are prepared like candidate screenshots (:func:`visual.image_hashes`). A file that
cannot be hashed (an SVG icon) keeps empty hashes and is read again next time.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, update

from kalkan.db.models import BrandAsset
from kalkan.db.session import Database
from kalkan.domain.enums import BrandAssetKind
from kalkan.logging import get_logger
from kalkan.signals.visual import image_hashes
from kalkan.storage.gcs import ObjectStore

SCREENSHOTS = frozenset({BrandAssetKind.REFERENCE_DESKTOP, BrandAssetKind.REFERENCE_MOBILE})
# Larger files are not decoded: they are no logo, icon or viewport screenshot.
MAX_ASSET_BYTES = 20 * 1024 * 1024

log = get_logger(__name__)


def hash_brand_assets(database: Database, store: ObjectStore, brand_id: uuid.UUID) -> int:
    """Hash the brand's assets that have no pHash yet; returns how many were hashed."""
    with database.worker_session() as session:
        pending = list(
            session.execute(
                select(BrandAsset.id, BrandAsset.kind, BrandAsset.storage_key)
                .where(BrandAsset.brand_id == brand_id, BrandAsset.phash.is_(None))
                .order_by(BrandAsset.id)
            )
        )
    hashed = 0
    for asset_id, kind, key in pending:
        data = store.read_bytes(key)
        found = (
            image_hashes(data, screenshot=kind in SCREENSHOTS)
            if len(data) <= MAX_ASSET_BYTES
            else None
        )
        if found is None:
            log.info("brand.asset_not_hashed", asset_id=str(asset_id), kind=kind)
            continue
        with database.worker_session() as session:
            session.execute(
                update(BrandAsset)
                .where(BrandAsset.id == asset_id, BrandAsset.phash.is_(None))
                .values(phash=found.phash, dhash=found.dhash)
            )
        hashed += 1
    return hashed
