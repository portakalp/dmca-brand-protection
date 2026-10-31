"""The filesystem storage backend (storage/filesystem.py) and its settings."""

from __future__ import annotations

import errno
import os
import stat
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from kalkan.scanning.persist import put_once
from kalkan.storage.filesystem import (
    META_DIR,
    STALE_TEMP_SECONDS,
    TMP_DIR,
    FileObjectStore,
    sweep_stale_temp_files,
)
from kalkan.storage.gcs import ObjectExistsError, ObjectStore, sha256_hex
from tests.support import make_settings

BUCKET = "kalkan-evidence"
REPORTS = "kalkan-reports"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "objects"
    path.mkdir()
    return path


@pytest.fixture
def store(root: Path) -> FileObjectStore:
    return FileObjectStore(root)


def _files(directory: Path) -> list[str]:
    return sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file())


# --------------------------------------------------------------------------------------------
# Writing and reading
# --------------------------------------------------------------------------------------------


def test_put_returns_key_and_hash_and_reads_back(store: FileObjectStore, root: Path) -> None:
    stored = store.put(BUCKET, "cases/1/page.html", b"<html></html>", "text/html")
    assert stored.key == f"gs://{BUCKET}/cases/1/page.html"
    assert stored.sha256 == sha256_hex(b"<html></html>")
    assert stored.size == 13
    assert stored.content_type == "text/html"
    assert (root / BUCKET / "cases/1/page.html").read_bytes() == b"<html></html>"
    assert store.read_bytes(stored.key) == b"<html></html>"
    assert store.stored_sha256(stored.key) == stored.sha256
    with store.open_stream(stored.key) as stream:
        assert stream.read(6) == b"<html>"


def test_default_content_type_and_empty_object(store: FileObjectStore) -> None:
    stored = store.put(BUCKET, "empty.bin", b"")
    assert stored.content_type == "application/octet-stream"
    assert stored.sha256 == sha256_hex(b"")
    assert store.read_bytes(stored.key) == b""


def test_objects_are_written_once(store: FileObjectStore) -> None:
    store.put(BUCKET, "a.txt", b"first")
    with pytest.raises(ObjectExistsError, match=f"gs://{BUCKET}/a.txt"):
        store.put(BUCKET, "a.txt", b"second")
    assert store.read_bytes(f"gs://{BUCKET}/a.txt") == b"first"
    assert store.stored_sha256(f"gs://{BUCKET}/a.txt") == sha256_hex(b"first")


def test_concurrent_writers_one_wins(store: FileObjectStore) -> None:
    outcomes: list[bytes] = []
    barrier = threading.Barrier(8)

    def write(data: bytes) -> None:
        barrier.wait()
        try:
            store.put(BUCKET, "race.txt", data)
            outcomes.append(data)
        except ObjectExistsError:
            pass

    threads = [threading.Thread(target=write, args=(f"w{i}".encode(),)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(outcomes) == 1
    key = f"gs://{BUCKET}/race.txt"
    assert store.read_bytes(key) == outcomes[0]
    assert store.stored_sha256(key) == sha256_hex(outcomes[0])


def test_no_temporary_files_are_left(store: FileObjectStore, root: Path) -> None:
    store.put(BUCKET, "x.txt", b"x")
    with pytest.raises(ObjectExistsError):
        store.put(BUCKET, "x.txt", b"y")
    assert _files(root / TMP_DIR) == []


def test_files_are_not_world_readable(store: FileObjectStore, root: Path) -> None:
    store.put(BUCKET, "secret.eml", b"mail")
    mode = stat.S_IMODE((root / BUCKET / "secret.eml").stat().st_mode)
    assert mode == 0o640


def test_missing_object(store: FileObjectStore, root: Path) -> None:
    with pytest.raises(FileNotFoundError):
        store.open_stream(f"gs://{BUCKET}/missing")
    with pytest.raises(FileNotFoundError):
        store.stored_sha256(f"gs://{BUCKET}/missing")
    # A "directory" is not an object, as in GCS.
    store.put(BUCKET, "dir/file", b"x")
    with pytest.raises(FileNotFoundError):
        store.open_stream(f"gs://{BUCKET}/dir")


def test_missing_metadata_is_backfilled_from_the_bytes(store: FileObjectStore, root: Path) -> None:
    stored = store.put(BUCKET, "a.txt", b"a")
    meta = root / META_DIR / BUCKET / "a.txt.json"
    meta.unlink()
    assert store.stored_sha256(stored.key) == sha256_hex(b"a")
    assert meta.is_file()
    assert store.stored_sha256(stored.key) == sha256_hex(b"a")
    assert _files(root / TMP_DIR) == []


def test_a_failed_metadata_write_does_not_block_the_retry(
    store: FileObjectStore, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace

    def full_disk(*_args: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "replace", full_disk)
    with pytest.raises(OSError, match="No space"):
        store.put(BUCKET, "c/1/shot.png", b"png")
    monkeypatch.setattr(os, "replace", real_replace)
    assert _files(root / TMP_DIR) == []
    stored = put_once(store, BUCKET, "c/1/shot.png", b"png", "image/png")
    assert stored.sha256 == sha256_hex(b"png")
    with pytest.raises(ObjectExistsError):
        put_once(store, BUCKET, "c/1/shot.png", b"other", "image/png")


def test_stale_temporary_files_are_swept(root: Path) -> None:
    tmp_dir = root / TMP_DIR
    tmp_dir.mkdir()
    old, fresh = tmp_dir / "put-old", tmp_dir / "put-fresh"
    old.write_bytes(b"x")
    fresh.write_bytes(b"y")
    hours_ago = time.time() - 2 * STALE_TEMP_SECONDS
    os.utime(old, (hours_ago, hours_ago))
    FileObjectStore(root)
    assert _files(tmp_dir) == ["put-fresh"]
    assert sweep_stale_temp_files(root / "missing") == 0


def test_metadata_tree_does_not_collide_with_objects(store: FileObjectStore) -> None:
    store.put(BUCKET, "a", b"a")
    stored = store.put(BUCKET, "a.json", b"b")
    assert store.stored_sha256(stored.key) == sha256_hex(b"b")
    assert store.stored_sha256(f"gs://{BUCKET}/a") == sha256_hex(b"a")


@pytest.mark.parametrize("key", ["s3://bucket/x", "gs://bucket", "gs:///path"])
def test_invalid_keys(store: FileObjectStore, key: str) -> None:
    with pytest.raises(ValueError, match="key"):
        store.open_stream(key)


# --------------------------------------------------------------------------------------------
# Path traversal
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bucket", ["..", ".", ".meta", ".tmp", "a", "Upper-case", "a/b", "a..b", "-ab", "x" * 64, ""]
)
def test_invalid_bucket_names_are_refused(store: FileObjectStore, root: Path, bucket: str) -> None:
    with pytest.raises(ValueError, match="bucket"):
        store.put(bucket, "x.txt", b"x")
    with pytest.raises(ValueError, match=r"bucket|key"):
        store.open_stream(f"gs://{bucket}/x.txt")
    assert _files(root) == []


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "../x",
        "a/../../x",
        "a/..",
        "./a",
        "a/./b",
        "a//b",
        "a/",
        "a\\..\\b",
        "a\x00b",
    ],
)
def test_invalid_paths_are_refused(store: FileObjectStore, root: Path, path: str) -> None:
    with pytest.raises(ValueError, match="path"):
        store.put(BUCKET, path, b"x")
    assert _files(root.parent) == []


def test_traversal_through_keys_is_refused(store: FileObjectStore) -> None:
    for key in (f"gs://{BUCKET}/../../etc/passwd", "gs://../etc/passwd", f"gs://{BUCKET}//x"):
        with pytest.raises(ValueError, match="invalid"):
            store.read_bytes(key)
        with pytest.raises(ValueError, match="invalid"):
            store.stored_sha256(key)


def test_symlinks_out_of_the_root_are_refused(store: FileObjectStore, root: Path) -> None:
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    (root / BUCKET).mkdir()
    os.symlink(outside, root / BUCKET / "link")
    with pytest.raises(ValueError, match="escapes"):
        store.read_bytes(f"gs://{BUCKET}/link/secret")
    with pytest.raises(ValueError, match="escapes"):
        store.put(BUCKET, "link/new", b"x")
    assert _files(outside) == ["secret"]


def test_relative_root_is_refused() -> None:
    with pytest.raises(ValueError, match="absolute"):
        FileObjectStore(Path("objects"))


# --------------------------------------------------------------------------------------------
# Demo deletion
# --------------------------------------------------------------------------------------------


def test_only_demo_objects_can_be_deleted(store: FileObjectStore, root: Path) -> None:
    store.put(REPORTS, "reports/c/2026-09/report-2026-09.pdf", b"r")
    store.put(BUCKET, "demo/c1/a.png", b"evidence bucket")
    for prefix in ("cases/", "", "reports/", "demo", "Demo/"):
        with pytest.raises(ValueError, match="demo/"):
            store.delete_demo_objects(REPORTS, prefix)
    with pytest.raises(ValueError, match="path"):
        store.delete_demo_objects(REPORTS, "demo/../reports/")

    store.put(REPORTS, "demo/c1/a.png", b"a")
    store.put(REPORTS, "demo/c1/brand/b.png", b"b")
    store.put(REPORTS, "demo/c2/a.png", b"other customer")
    assert store.delete_demo_objects(REPORTS, "demo/c1/") == 2

    assert _files(root / REPORTS) == ["demo/c2/a.png", "reports/c/2026-09/report-2026-09.pdf"]
    assert _files(root / BUCKET) == ["demo/c1/a.png"]
    assert not (root / META_DIR / REPORTS / "demo/c1/a.png.json").exists()
    assert not (root / REPORTS / "demo/c1").exists()
    assert (root / REPORTS / "demo").is_dir()
    with pytest.raises(FileNotFoundError):
        store.read_bytes(f"gs://{REPORTS}/demo/c1/a.png")
    # Deleted demo paths can be written again (the demo seed reuses them).
    store.put(REPORTS, "demo/c1/a.png", b"again")


def test_demo_prefix_is_a_string_prefix_like_gcs(store: FileObjectStore, root: Path) -> None:
    store.put(REPORTS, "demo/ab/x.png", b"x")
    store.put(REPORTS, "demo/abc/y.png", b"y")
    store.put(REPORTS, "demo/b/z.png", b"z")
    assert store.delete_demo_objects(REPORTS, "demo/ab") == 2
    assert _files(root / REPORTS) == ["demo/b/z.png"]
    assert store.delete_demo_objects(REPORTS, "demo/nothing/") == 0
    assert store.delete_demo_objects(REPORTS, "demo/") == 1
    assert _files(root / REPORTS) == []


def test_demo_deletion_does_not_follow_symlinks(store: FileObjectStore, root: Path) -> None:
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "keep.png").write_bytes(b"keep")
    store.put(REPORTS, "demo/c1/a.png", b"a")
    os.symlink(outside / "keep.png", root / REPORTS / "demo/c1/link.png")
    os.symlink(outside, root / REPORTS / "demo/c1/dir")
    assert store.delete_demo_objects(REPORTS, "demo/c1/") == 1
    assert _files(outside) == ["keep.png"]


# --------------------------------------------------------------------------------------------
# Buckets and settings
# --------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_env")
def test_ensure_buckets_creates_directories_in_any_env(store: FileObjectStore, root: Path) -> None:
    settings = make_settings(env="prod", storage_backend="filesystem", storage_root=root)
    store.ensure_buckets(settings, BUCKET, REPORTS)
    assert (root / BUCKET).is_dir()
    assert (root / REPORTS).is_dir()
    with pytest.raises(ValueError, match="bucket"):
        store.ensure_buckets(settings, "../escape")


@pytest.mark.usefixtures("clean_env")
def test_from_settings_picks_the_backend(root: Path) -> None:
    settings = make_settings(env="prod", storage_backend="filesystem", storage_root=root)
    store = ObjectStore.from_settings(settings)
    assert isinstance(store, FileObjectStore)
    assert store.root == root

    emulator = make_settings(env="local", storage_emulator_host="http://localhost:4443")
    assert emulator.storage_backend == "gcs"
    gcs = ObjectStore.from_settings(emulator)
    assert type(gcs) is ObjectStore


@pytest.mark.usefixtures("clean_env")
def test_storage_settings(clean_env: pytest.MonkeyPatch) -> None:
    defaults = make_settings(env="local")
    assert defaults.storage_backend == "gcs"
    assert defaults.storage_root == Path("/data/objects")

    clean_env.setenv("STORAGE_BACKEND", "filesystem")
    clean_env.setenv("STORAGE_ROOT", "/srv/kalkan/objects")
    prod = make_settings(env="prod")
    assert prod.storage_backend == "filesystem"
    assert prod.storage_root == Path("/srv/kalkan/objects")
    assert prod.storage_emulator_host is None


@pytest.mark.usefixtures("clean_env")
@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"storage_backend": "s3"}, "storage_backend"),
        ({"storage_root": "objects"}, "absolute"),
        ({"storage_root": "./data"}, "absolute"),
    ],
)
def test_invalid_storage_settings(values: dict[str, str], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(env="local", **values)


@pytest.mark.usefixtures("clean_env")
def test_prod_with_filesystem_still_rejects_the_emulator(root: Path) -> None:
    with pytest.raises(ValidationError, match="STORAGE_EMULATOR_HOST"):
        make_settings(
            env="prod",
            storage_backend="filesystem",
            storage_root=root,
            storage_emulator_host="http://fake-gcs:4443",
        )
