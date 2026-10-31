"""Reading a job folder written by the untrusted browser (K82)."""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kalkan.domain.enums import ArtifactKind, Profile
from kalkan.scanning.capture_contract import (
    META_FILE,
    META_MAX_BYTES,
    ButtonInfo,
    CaptureMeta,
    CaptureRecord,
    FormInfo,
    LinkInfo,
    Outcome,
    ProfileResult,
    Purpose,
)
from kalkan.scanning.persist import (
    PNG_MAGIC,
    InvalidCaptureError,
    JobFolder,
    _clean,
    browser_links,
    read_meta,
    remove_job_dir,
    remove_stale_job_dirs,
)

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def _record(job_id: uuid.UUID) -> CaptureRecord:
    return CaptureRecord(
        job_id=job_id,
        purpose=Purpose.CASE,
        customer_id=uuid.uuid4(),
        brand_id=uuid.uuid4(),
        case_id=uuid.uuid4(),
        scan_ids={Profile.DESKTOP: uuid.uuid4(), Profile.MOBILE: uuid.uuid4()},
        urls=["https://mintera-login.test/"],
        issued_at=NOW,
    )


def _result(profile: Profile = Profile.DESKTOP, **values: object) -> ProfileResult:
    return ProfileResult.model_validate(
        {"profile": profile, "outcome": Outcome.OK, "started_at": NOW, "finished_at": NOW} | values
    )


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / str(uuid.uuid4())
    folder.mkdir()
    return folder


def test_files_are_read_only_as_single_regular_files(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    (folder / "ok.png").write_bytes(PNG_MAGIC + b"rest")
    (folder / "page.html").write_text("<html>not a png</html>")
    (tmp_path / "secret").write_text("worker-default's environment")
    (folder / "link.png").symlink_to(tmp_path / "secret")
    os.link(tmp_path / "secret", folder / "hard.png")
    os.mkfifo(folder / "fifo.png")
    with (folder / "sparse.html").open("wb") as sparse:
        sparse.truncate(11 * 1024 * 1024)
    with JobFolder(folder) as job:
        assert job.read("ok.png", 100, PNG_MAGIC) == PNG_MAGIC + b"rest"
        job.check("ok.png", 100, PNG_MAGIC)
        for name, message in (
            ("link.png", "cannot be opened"),  # O_NOFOLLOW
            ("hard.png", "other links"),
            ("fifo.png", "not a regular file"),  # and the open did not block
            ("missing.png", "is missing"),
        ):
            with pytest.raises(InvalidCaptureError, match=message):
                job.read(name, 1000)
        with pytest.raises(InvalidCaptureError, match="not a PNG"):
            job.check("page.html", 100, PNG_MAGIC)
        with pytest.raises(InvalidCaptureError, match="larger than"):
            job.check("sparse.html", 10 * 1024 * 1024)
        with pytest.raises(InvalidCaptureError, match="larger than"):
            job.read("ok.png", 4)


def test_the_folder_itself_must_be_a_directory(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    link = tmp_path / str(uuid.uuid4())
    link.symlink_to(folder)
    with pytest.raises(InvalidCaptureError, match="cannot be opened"):
        JobFolder(link)
    with pytest.raises(InvalidCaptureError, match="missing"):
        JobFolder(tmp_path / "gone")


def test_meta_is_validated_against_the_record(tmp_path: Path) -> None:
    job_id = uuid.uuid4()
    record = _record(job_id)
    folder = _folder(tmp_path)

    def meta_with(**values: object) -> None:
        meta = {"job_id": str(job_id), "profiles": []} | values
        (folder / META_FILE).write_text(CaptureMeta.model_validate(meta).model_dump_json())

    meta_with(profiles=[_result().model_dump(mode="json")], proxy_country="PL")
    with JobFolder(folder) as job:
        meta = read_meta(job, record)
    assert meta.proxy_country == "PL"
    assert meta.profiles[0].outcome is Outcome.OK

    cases: list[tuple[str, str]] = [
        ('{"job_id": ', "meta.json is invalid"),
        (f'{{"job_id": "{job_id}", "extra": 1}}', "extra_forbidden"),
        (f'{{"job_id": "{job_id}", "error": "{"x" * 3000}"}}', "string_too_long"),
        (f'{{"job_id": "{uuid.uuid4()}"}}', "another job"),
    ]
    for raw, message in cases:
        (folder / META_FILE).write_text(raw)
        with JobFolder(folder) as job, pytest.raises(InvalidCaptureError, match=message):
            read_meta(job, record)

    for profiles, message in (
        ([_result(), _result()], "twice"),
        ([_result(http_status=99999)], "HTTP status"),
        ([_result(files=[ArtifactKind.HTML, ArtifactKind.HTML])], "file twice"),
        ([_result(files=[ArtifactKind.DNS])], "does not write"),  # not a browser file
    ):
        meta_with(profiles=[result.model_dump(mode="json") for result in profiles])
        with JobFolder(folder) as job, pytest.raises(InvalidCaptureError, match=message):
            read_meta(job, record)

    desktop_only = record.model_copy(update={"scan_ids": {Profile.DESKTOP: uuid.uuid4()}})
    meta_with(profiles=[_result(Profile.MOBILE).model_dump(mode="json")])
    with JobFolder(folder) as job, pytest.raises(InvalidCaptureError, match="did not ask"):
        read_meta(job, desktop_only)

    with (folder / META_FILE).open("wb") as big:
        big.truncate(META_MAX_BYTES + 1)
    with JobFolder(folder) as job, pytest.raises(InvalidCaptureError, match="larger than"):
        read_meta(job, record)


def test_browser_links_use_the_precheck_tags() -> None:
    result = _result(
        redirect_chain=["http://mintera-door.test/"],
        final_url="https://mintera-door.test/home",
        forms=[
            FormInfo(
                action="https://collect.example/post",
                buttons=[ButtonInfo(text="Wpłać", target="https://pay.example/")],
            )
        ],
        buttons=[ButtonInfo(text="Doğrula", target="https://mintera-login.test/"), ButtonInfo()],
        links=[
            LinkInfo(url="https://quick-loans.example/", text="Claim refund", tag="onclick"),
            LinkInfo(url="https://next.example/", tag="meta-refresh"),
            LinkInfo(url="https://frame.example/", tag="iframe"),
        ],
    )
    assert [(link.url, link.text, link.tag) for link in browser_links(result)] == [
        ("http://mintera-door.test/", "", "redirect"),
        ("https://mintera-door.test/home", "", "redirect"),
        ("https://collect.example/post", "", "form"),
        ("https://pay.example/", "Wpłać", "button"),
        ("https://mintera-login.test/", "Doğrula", "button"),
        ("https://quick-loans.example/", "Claim refund", "button"),
        ("https://next.example/", "", "redirect"),
        ("https://frame.example/", "", "iframe"),
    ]


def test_text_is_cleaned_for_postgres() -> None:
    assert _clean("a\x00b\ud800c") == "ab?c"
    assert _clean({"k": ["\x00"], "n": 1}) == {"k": [""], "n": 1}


def test_stale_job_folders_are_removed(tmp_path: Path) -> None:
    old, young, partial = (str(uuid.uuid4()) for _ in range(3))
    for name in (old, young, f"{partial}.partial", "not-a-job"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "meta.json").write_text("{}")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = str(uuid.uuid4())
    (tmp_path / linked).symlink_to(outside)
    now = time.time()
    day_ago = now - timedelta(hours=25).total_seconds()
    for name in (old, f"{partial}.partial", "not-a-job", linked):
        os.utime(tmp_path / name, (day_ago, day_ago), follow_symlinks=False)
    assert remove_stale_job_dirs(tmp_path, now=now) == 3
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        ["not-a-job", "outside", young]
    )
    assert remove_stale_job_dirs(tmp_path / "absent", now=now) == 0


def test_a_linked_job_folder_is_unlinked_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_text("x")
    job_id = uuid.uuid4()
    (root / str(job_id)).symlink_to(target)
    remove_job_dir(job_id, root)
    assert not (root / str(job_id)).exists()
    assert (target / "keep").exists()
    remove_job_dir(job_id, root)  # already gone
