"""Helpers that store evidence and record it in ``artifacts`` (architecture.md §11)."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from kalkan.db.models import Artifact, Case
from kalkan.domain.enums import ArtifactKind, Profile
from kalkan.storage.gcs import ObjectStore, StoredObject


def record_artifact(
    session: Session,
    case: Case,
    stored: StoredObject,
    *,
    kind: ArtifactKind,
    scan_id: uuid.UUID | None = None,
    profile: Profile | None = None,
) -> Artifact:
    artifact = Artifact(
        customer_id=case.customer_id,
        case_id=case.id,
        scan_id=scan_id,
        kind=kind,
        profile=profile,
        storage_key=stored.key,
        sha256=stored.sha256,
        bytes=stored.size,
        content_type=stored.content_type,
    )
    session.add(artifact)
    return artifact


def store_artifact(
    session: Session,
    store: ObjectStore,
    case: Case,
    *,
    bucket: str,
    path: str,
    data: bytes,
    content_type: str,
    kind: ArtifactKind,
    scan_id: uuid.UUID | None = None,
    profile: Profile | None = None,
) -> Artifact:
    """Upload ``data`` once and add its ``artifacts`` row to ``session``."""
    stored = store.put(bucket, path, data, content_type)
    return record_artifact(session, case, stored, kind=kind, scan_id=scan_id, profile=profile)
