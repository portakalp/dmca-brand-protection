"""test_storage.py's checks against the filesystem backend instead of the GCS emulator.

The bucket names and keys are the test settings' GCS ones: the filesystem backend keeps the
``gs://<bucket>/<path>`` keys, so artifacts rows look the same with either backend.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from kalkan.config import Settings
from kalkan.db.models import Artifact, Case
from kalkan.db.session import Database
from kalkan.domain.enums import ArtifactKind, Profile
from kalkan.storage import paths
from kalkan.storage.artifacts import store_artifact
from kalkan.storage.filesystem import FileObjectStore
from kalkan.storage.gcs import ObjectExistsError, ObjectStore, sha256_hex
from tests import factories


@pytest.fixture
def file_store(test_settings: Settings, tmp_path: Path) -> ObjectStore:
    """The store ``STORAGE_BACKEND=filesystem`` gives, with the test buckets created."""
    settings = test_settings.model_copy(
        update={"storage_backend": "filesystem", "storage_root": tmp_path}
    )
    store = ObjectStore.from_settings(settings)
    assert isinstance(store, FileObjectStore)
    assert settings.gcs_evidence_bucket
    assert settings.gcs_reports_bucket
    store.ensure_buckets(settings, settings.gcs_evidence_bucket, settings.gcs_reports_bucket)
    return store


def test_put_returns_key_and_hash_and_reads_back(
    file_store: ObjectStore, test_settings: Settings
) -> None:
    bucket = test_settings.gcs_evidence_bucket
    assert bucket
    path = f"tests/{uuid.uuid4()}/page.html"
    stored = file_store.put(bucket, path, b"<html></html>", "text/html")
    assert stored.key == f"gs://{bucket}/{path}"
    assert stored.sha256 == sha256_hex(b"<html></html>")
    assert stored.size == 13
    assert file_store.read_bytes(stored.key) == b"<html></html>"
    assert file_store.stored_sha256(stored.key) == stored.sha256
    with file_store.open_stream(stored.key) as stream:
        assert stream.read(6) == b"<html>"


def test_objects_are_written_once(file_store: ObjectStore, test_settings: Settings) -> None:
    bucket = test_settings.gcs_evidence_bucket
    assert bucket
    path = f"tests/{uuid.uuid4()}.txt"
    file_store.put(bucket, path, b"first")
    with pytest.raises(ObjectExistsError):
        file_store.put(bucket, path, b"second")
    assert file_store.read_bytes(f"gs://{bucket}/{path}") == b"first"


def test_missing_object(file_store: ObjectStore, test_settings: Settings) -> None:
    with pytest.raises(FileNotFoundError):
        file_store.open_stream(f"gs://{test_settings.gcs_evidence_bucket}/missing/{uuid.uuid4()}")


def test_only_demo_objects_can_be_deleted(file_store: ObjectStore, test_settings: Settings) -> None:
    reports = test_settings.gcs_reports_bucket
    assert reports
    with pytest.raises(ValueError, match="demo/"):
        file_store.delete_demo_objects(reports, "cases/")
    prefix = paths.demo(uuid.uuid4(), "")
    file_store.put(reports, f"{prefix}a.png", b"a")
    assert file_store.delete_demo_objects(reports, prefix) == 1


def test_store_artifact_records_the_hash(
    database: Database, file_store: ObjectStore, test_settings: Settings
) -> None:
    bucket = test_settings.gcs_evidence_bucket
    assert bucket
    with database.worker_session() as session:
        owner = factories.customer(session)
        case = factories.case(session, owner, factories.brand(session, owner))
        scan_id = uuid.uuid4()
        artifact = store_artifact(
            session,
            file_store,
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
    assert stored.storage_key == (f"gs://{bucket}/cases/{case.id}/{scan_id}/html-desktop.html")
    assert file_store.read_bytes(stored.storage_key) == b"<html>clone</html>"
    assert file_store.stored_sha256(stored.storage_key) == stored.sha256
