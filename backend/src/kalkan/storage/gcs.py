"""Write-once object storage on Google Cloud Storage (architecture.md §11).

Objects are addressed by full ``gs://<bucket>/<path>`` keys. Every object is written once:
its SHA-256 is computed before upload, stored in the object metadata and returned, and a
second write to the same path fails. Locally the same code talks to fake-gcs-server through
``STORAGE_EMULATOR_HOST``. With ``STORAGE_BACKEND=filesystem``, :meth:`ObjectStore.from_settings`
returns a :class:`~kalkan.storage.filesystem.FileObjectStore` with the same interface and keys.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import IO, cast

from google.api_core.exceptions import PreconditionFailed
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage

from kalkan.config import ConfigError, Settings

LOCAL_PROJECT = "kalkan-local"
# Prefix under the reports bucket that holds demo data; the only place objects may be removed.
DEMO_PREFIX = "demo/"


class ObjectExistsError(RuntimeError):
    """An object already exists at this path; stored objects are never overwritten."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    sha256: str
    size: int
    content_type: str


def make_key(bucket: str, path: str) -> str:
    return f"gs://{bucket}/{path}"


def split_key(key: str) -> tuple[str, str]:
    if not key.startswith("gs://"):
        raise ValueError(f"not a gs:// key: {key}")
    bucket, _, path = key.removeprefix("gs://").partition("/")
    if not bucket or not path:
        raise ValueError(f"not a gs://<bucket>/<path> key: {key}")
    return bucket, path


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ObjectStore:
    def __init__(self, client: storage.Client) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings) -> ObjectStore:
        """The store ``STORAGE_BACKEND`` names: GCS (or its emulator) or the filesystem."""
        if settings.storage_backend == "filesystem":
            # Imported here: filesystem.py subclasses ObjectStore from this module.
            from kalkan.storage.filesystem import FileObjectStore

            return FileObjectStore(settings.storage_root)
        if settings.storage_emulator_host:
            client = storage.Client(
                project=LOCAL_PROJECT,
                credentials=AnonymousCredentials(),
                client_options={"api_endpoint": settings.storage_emulator_host},
            )
        else:
            client = storage.Client()
        return cls(client)

    def put(
        self, bucket: str, path: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> StoredObject:
        """Upload ``data`` once and return its key and SHA-256."""
        digest = sha256_hex(data)
        blob = self._client.bucket(bucket).blob(path)
        blob.metadata = {"sha256": digest}
        try:
            blob.upload_from_string(data, content_type=content_type, if_generation_match=0)
        except PreconditionFailed as exc:
            raise ObjectExistsError(make_key(bucket, path)) from exc
        return StoredObject(make_key(bucket, path), digest, len(data), content_type)

    def open_stream(self, key: str) -> IO[bytes]:
        """A readable file object for ``key``; callers stream it instead of loading it.

        The blob is not reloaded first: its ``mediaLink`` would point at the emulator's
        external URL, which only containers can resolve. Downloads use the client endpoint.
        """
        bucket, path = split_key(key)
        blob = self._client.bucket(bucket).blob(path)
        if not blob.exists():
            raise FileNotFoundError(key)
        return cast(IO[bytes], blob.open("rb"))

    def read_bytes(self, key: str) -> bytes:
        with self.open_stream(key) as stream:
            return stream.read()

    def stored_sha256(self, key: str) -> str | None:
        bucket, path = split_key(key)
        blob = self._client.bucket(bucket).get_blob(path)
        if blob is None:
            raise FileNotFoundError(key)
        return (blob.metadata or {}).get("sha256")

    def delete_demo_objects(self, reports_bucket: str, prefix: str) -> int:
        """Remove demo objects under ``demo/`` in the reports bucket. Evidence is never deleted."""
        if not prefix.startswith(DEMO_PREFIX):
            raise ValueError("only objects under demo/ can be deleted")
        blobs = list(self._client.list_blobs(reports_bucket, prefix=prefix))
        for blob in blobs:
            blob.delete()
        return len(blobs)

    def ensure_buckets(self, settings: Settings, *buckets: str) -> None:
        """Create missing buckets on the local emulator. In prod, Terraform owns the buckets."""
        if not settings.storage_emulator_host or settings.env != "local":
            raise ConfigError("buckets are only created on the local storage emulator")
        for name in buckets:
            if self._client.lookup_bucket(name) is None:
                self._client.create_bucket(name)
