from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from kalkan.config import Settings
from kalkan.db.models import Artifact, Case
from kalkan.db.session import Database
from kalkan.domain.enums import ArtifactKind, Profile
from kalkan.storage import paths
from kalkan.storage.artifacts import store_artifact
from kalkan.storage.gcs import ObjectExistsError, ObjectStore, sha256_hex, split_key
from tests import factories


def test_put_returns_key_and_hash_and_reads_back(
    object_store: ObjectStore, test_settings: Settings
) -> None:
    bucket = test_settings.gcs_evidence_bucket
    assert bucket
    path = f"tests/{uuid.uuid4()}/page.html"
    stored = object_store.put(bucket, path, b"<html></html>", "text/html")
    assert stored.key == f"gs://{bucket}/{path}"
    assert stored.sha256 == sha256_hex(b"<html></html>")
    assert stored.size == 13
    assert object_store.read_bytes(stored.key) == b"<html></html>"
    assert object_store.stored_sha256(stored.key) == stored.sha256
    with object_store.open_stream(stored.key) as stream:
        assert stream.read(6) == b"<html>"


def test_objects_are_written_once(object_store: ObjectStore, test_settings: Settings) -> None:
    bucket = test_settings.gcs_evidence_bucket
    assert bucket
    path = f"tests/{uuid.uuid4()}.txt"
    object_store.put(bucket, path, b"first")
    with pytest.raises(ObjectExistsError):
        object_store.put(bucket, path, b"second")
    assert object_store.read_bytes(f"gs://{bucket}/{path}") == b"first"


def test_missing_object(object_store: ObjectStore, test_settings: Settings) -> None:
    with pytest.raises(FileNotFoundError):
        object_store.open_stream(f"gs://{test_settings.gcs_evidence_bucket}/missing/{uuid.uuid4()}")


def test_only_demo_objects_can_be_deleted(
    object_store: ObjectStore, test_settings: Settings
) -> None:
    reports = test_settings.gcs_reports_bucket
    assert reports
    with pytest.raises(ValueError, match="demo/"):
        object_store.delete_demo_objects(reports, "cases/")
    prefix = paths.demo(uuid.uuid4(), "")
    object_store.put(reports, f"{prefix}a.png", b"a")
    assert object_store.delete_demo_objects(reports, prefix) == 1


@pytest.mark.parametrize("key", ["s3://bucket/x", "gs://bucket", "gs:///path"])
def test_invalid_keys(key: str) -> None:
    with pytest.raises(ValueError, match="key"):
        split_key(key)


def test_store_artifact_records_the_hash(
    database: Database, object_store: ObjectStore, test_settings: Settings
) -> None:
    bucket = test_settings.gcs_evidence_bucket
    assert bucket
    with database.worker_session() as session:
        owner = factories.customer(session)
        case = factories.case(session, owner, factories.brand(session, owner))
        scan_id = uuid.uuid4()
        artifact = store_artifact(
            session,
            object_store,
            case,
            bucket=bucket,
            path=paths.case_artifact(case.id, scan_id, ArtifactKind.HTML, Profile.DESKTOP, "html"),
            data=b"<html>clone</html>",
            content_type="text/html",
            kind=ArtifactKind.HTML,
            profile=Profile.DESKTOP,
        )
    with database.worker_session() as session:
        stored = session.scalars(select(Artifact).where(Artifact.case_id == case.id)).one()
        assert session.get_one(Case, case.id).id == case.id
    assert stored.id == artifact.id
    assert stored.sha256 == sha256_hex(b"<html>clone</html>")
    assert stored.storage_key.endswith(f"cases/{case.id}/{scan_id}/html-desktop.html")
    assert object_store.read_bytes(stored.storage_key) == b"<html>clone</html>"
