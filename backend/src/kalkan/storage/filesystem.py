"""Write-once object storage on a local filesystem, for self-hosting without a cloud account.

``STORAGE_BACKEND=filesystem`` makes :meth:`ObjectStore.from_settings` return a
:class:`FileObjectStore` rooted at ``STORAGE_ROOT`` (a Docker volume in self-hosting).

Keys stay ``gs://<bucket>/<path>``. They are stored in ``artifacts.storage_key`` and several
other columns, built with :func:`make_key` in many modules and parsed by :func:`split_key`, so
keeping the format means no caller, migration or API change; here the scheme only names the
logical object, never Google Cloud. Moving between backends is a copy of the bucket trees.

Layout under the root (bucket names start with a letter or digit, so they never collide with
the dot directories)::

    <root>/<bucket>/<path>              the object's bytes
    <root>/.meta/<bucket>/<path>.json   {"sha256", "size", "content_type"} written with it
    <root>/.tmp/                        temporary files, renamed into place

The guarantees match the GCS backend:

* **Write-once.** Bytes go to a temporary file first, are fsynced, then ``os.link`` publishes
  them under the final name. ``link`` fails if the name exists, atomically, which is the
  filesystem's ``O_EXCL`` and the equivalent of GCS's ``if_generation_match=0``: of two
  concurrent writers exactly one wins, the other gets :class:`ObjectExistsError`, and a
  reader never sees a partial object. ``os.replace`` would silently overwrite instead.
* **SHA-256.** Computed from the bytes before writing and returned, as in GCS; the metadata
  file records it for :meth:`FileObjectStore.stored_sha256`. Only the writer that won the
  link writes the metadata. If that write fails (a full disk, a crash), the object is left
  without metadata; :meth:`FileObjectStore.stored_sha256` then hashes the object's bytes and
  writes the missing file, so a retry of the same write still sees a matching digest.
* **Temporary files** older than an hour (left by a crash) are removed when the first store
  for a root is created in a process.
* **No path traversal.** Bucket names follow GCS naming rules; paths are relative,
  ``/``-separated segments without ``.``, ``..``, empty segments, NUL or backslashes; and
  the resolved location must stay under the bucket directory (this also catches symlinks).
* **Deletes only under ``demo/``** of the given bucket; evidence is never removed.

Unlike GCS, a filesystem cannot hold both an object ``a`` and an object ``a/b``; Kalkan's
object paths (``paths.py``) never do this.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import IO

from kalkan.config import Settings
from kalkan.storage.gcs import (
    DEMO_PREFIX,
    ObjectExistsError,
    ObjectStore,
    StoredObject,
    make_key,
    sha256_hex,
    split_key,
)

META_DIR = ".meta"
TMP_DIR = ".tmp"
# GCS bucket naming (lower-case letters, digits, '-', '_', '.'; 3-63 characters, starting and
# ending with a letter or digit) also keeps a bucket from being '.', '..' or a dot directory.
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
# Objects and their metadata are readable by the group (the services share one volume).
_FILE_MODE = 0o640
# A temporary file this old belongs to a writer that died; no write takes that long.
STALE_TEMP_SECONDS = 3600
_HASH_CHUNK = 1024 * 1024
# Roots whose stale temporary files this process has already removed.
_swept_roots: set[Path] = set()


def _check_bucket(bucket: str) -> str:
    if not _BUCKET.fullmatch(bucket) or ".." in bucket:
        raise ValueError(f"invalid bucket name: {bucket!r}")
    return bucket


def _segments(path: str, *, prefix: bool = False) -> list[str]:
    """The ``/``-separated segments of an object path, refusing anything that could escape.

    A ``prefix`` may end with ``/`` (``demo/<id>/``), as GCS listing prefixes do.
    """
    if not path or path.startswith("/") or "\x00" in path or "\\" in path:
        raise ValueError(f"invalid object path: {path!r}")
    segments = path.split("/")
    if prefix and segments[-1] == "":
        segments = segments[:-1]
    if not segments or any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(f"invalid object path: {path!r}")
    return segments


def sweep_stale_temp_files(root: Path, *, now: float | None = None) -> int:
    """Remove ``<root>/.tmp/put-*`` files older than :data:`STALE_TEMP_SECONDS`."""
    cutoff = (time.time() if now is None else now) - STALE_TEMP_SECONDS
    removed = 0
    try:
        candidates = list((root / TMP_DIR).glob("put-*"))
    except OSError:
        return 0
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                candidate.unlink()
                removed += 1
        except OSError:
            continue  # gone already, or not ours to remove
    return removed


def _fsync_dir(directory: Path) -> None:
    """Make a new directory entry durable; a no-op where directories cannot be opened."""
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:  # pragma: no cover - platforms without O_DIRECTORY semantics
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class FileObjectStore(ObjectStore):
    """:class:`ObjectStore` on a local directory; see the module docstring for the layout.

    It subclasses :class:`ObjectStore` so every ``ObjectStore`` annotation in the code base
    accepts it unchanged. It overrides every public method and never creates a GCS client.
    """

    def __init__(self, root: Path) -> None:
        # No super().__init__(): this backend has no GCS client.
        if not root.is_absolute():
            raise ValueError("the storage root must be an absolute path")
        self._root = root
        if root not in _swept_roots:
            _swept_roots.add(root)
            sweep_stale_temp_files(root)

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------------------------
    # Path mapping
    # ------------------------------------------------------------------------------------

    def _bucket_dir(self, bucket: str) -> Path:
        return self._root / _check_bucket(bucket)

    def _contained(self, base: Path, candidate: Path) -> Path:
        """``candidate`` resolved, or ``ValueError`` if it leaves ``base`` (e.g. a symlink)."""
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(base.resolve(strict=False)):
            raise ValueError(f"object path escapes the storage root: {candidate}")
        return resolved

    def _object_path(self, bucket: str, path: str) -> Path:
        base = self._bucket_dir(bucket)
        return self._contained(base, base.joinpath(*_segments(path)))

    def _meta_path(self, bucket: str, path: str) -> Path:
        base = self._root / META_DIR / _check_bucket(bucket)
        segments = _segments(path)
        segments[-1] += ".json"
        return self._contained(base, base.joinpath(*segments))

    def _key_paths(self, key: str) -> tuple[Path, Path]:
        bucket, path = split_key(key)
        return self._object_path(bucket, path), self._meta_path(bucket, path)

    # ------------------------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------------------------

    def _temp_file(self, data: bytes) -> Path:
        """``data`` in a new, fsynced file under ``.tmp`` on the same filesystem as the root."""
        tmp_dir = self._root / TMP_DIR
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=tmp_dir, prefix="put-")
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), _FILE_MODE)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
        return Path(name)

    def put(
        self, bucket: str, path: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> StoredObject:
        """Write ``data`` once and return its key and SHA-256."""
        target = self._object_path(bucket, path)
        meta_target = self._meta_path(bucket, path)
        key = make_key(bucket, path)
        digest = sha256_hex(data)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._temp_file(data)
        try:
            os.link(tmp, target)
        except FileExistsError as exc:
            raise ObjectExistsError(key) from exc
        finally:
            tmp.unlink(missing_ok=True)
        _fsync_dir(target.parent)
        # This writer won the link, so it is the only one writing this object's metadata.
        meta = {"sha256": digest, "size": len(data), "content_type": content_type}
        meta_target.parent.mkdir(parents=True, exist_ok=True)
        meta_tmp = self._temp_file(json.dumps(meta).encode())
        try:
            os.replace(meta_tmp, meta_target)
        except BaseException:
            meta_tmp.unlink(missing_ok=True)
            raise
        _fsync_dir(meta_target.parent)
        return StoredObject(key, digest, len(data), content_type)

    # ------------------------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------------------------

    def open_stream(self, key: str) -> IO[bytes]:
        """A readable file object for ``key``; callers stream it instead of loading it."""
        target, _ = self._key_paths(key)
        if not target.is_file():
            raise FileNotFoundError(key)
        return target.open("rb")

    def read_bytes(self, key: str) -> bytes:
        with self.open_stream(key) as stream:
            return stream.read()

    def stored_sha256(self, key: str) -> str | None:
        """The SHA-256 recorded when ``key`` was written; ``None`` if it has no metadata."""
        target, meta_path = self._key_paths(key)
        if not target.is_file():
            raise FileNotFoundError(key)
        try:
            meta = json.loads(meta_path.read_bytes())
        except FileNotFoundError:
            return self._backfill_meta(target, meta_path)
        digest = meta.get("sha256") if isinstance(meta, dict) else None
        return digest if isinstance(digest, str) else None

    def _backfill_meta(self, target: Path, meta_path: Path) -> str:
        """Hash an object whose metadata write failed and record it.

        Objects are write-once, so their bytes are authoritative. The metadata is published
        with ``os.link`` like an object: if another reader backfills it first, both wrote the
        same digest.
        """
        hasher = hashlib.sha256()
        size = 0
        with target.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK):
                hasher.update(chunk)
                size += len(chunk)
        digest = hasher.hexdigest()
        meta = {"sha256": digest, "size": size, "content_type": "application/octet-stream"}
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_tmp = self._temp_file(json.dumps(meta).encode())
        try:
            os.link(meta_tmp, meta_path)
        except FileExistsError:
            pass
        finally:
            meta_tmp.unlink(missing_ok=True)
        _fsync_dir(meta_path.parent)
        return digest

    # ------------------------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------------------------

    def delete_demo_objects(self, reports_bucket: str, prefix: str) -> int:
        """Remove demo objects under ``demo/`` in the reports bucket. Evidence is never deleted.

        ``prefix`` is a string prefix, as in a GCS listing: ``demo/a`` also matches
        ``demo/ab/x.png``.
        """
        if not prefix.startswith(DEMO_PREFIX):
            raise ValueError("only objects under demo/ can be deleted")
        segments = _segments(prefix, prefix=True)
        bucket_dir = self._bucket_dir(reports_bucket)
        # The deepest directory that holds every match: the prefix itself when it ends with
        # '/', otherwise its parent. Both are under demo/.
        search = segments if prefix.endswith("/") else segments[:-1]
        start = self._contained(bucket_dir, bucket_dir.joinpath(*search))
        if not start.is_dir():
            return 0
        deleted = 0
        for found in sorted(start.rglob("*")):
            if not found.is_file() or found.is_symlink():
                continue
            relative = found.relative_to(bucket_dir.resolve()).as_posix()
            if not relative.startswith(prefix):
                continue
            found.unlink()
            self._meta_path(reports_bucket, relative).unlink(missing_ok=True)
            deleted += 1
        self._prune_empty_dirs(start, stop=bucket_dir / DEMO_PREFIX.rstrip("/"))
        return deleted

    def _prune_empty_dirs(self, start: Path, *, stop: Path) -> None:
        """Remove directories left empty under ``start``, never ``stop`` or above it."""
        stop = stop.resolve(strict=False)
        for directory in sorted(
            (p for p in start.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
        ):
            if not any(directory.iterdir()):
                directory.rmdir()
        if start != stop and start.is_relative_to(stop) and not any(start.iterdir()):
            start.rmdir()

    def ensure_buckets(self, settings: Settings, *buckets: str) -> None:
        """Create the bucket directories. Allowed in every ENV: no Terraform owns a folder."""
        del settings  # the GCS backend needs it to refuse prod; directories are always safe
        for name in buckets:
            self._bucket_dir(name).mkdir(parents=True, exist_ok=True)
