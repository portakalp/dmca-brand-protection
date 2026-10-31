"""persist_capture: the browser's output becomes evidence (architecture.md §10, §11, §24).

worker-default trusts only its own job record (``capture:job:<job_id>`` in Redis): case,
customer, brand and scan ids come from there, never from the browser (K82). From the job
folder it reads only ``meta.json`` and the file names of ``capture_contract``, each opened
relative to the folder without following links, as a regular file with a single link and
within its size cap; screenshots must be PNG. A folder that breaks these rules is not retried.

Outcomes of a case job (§7):

- a profile met a bot ``challenge``: everything is stored, ``captured`` is written and the
  case goes to review (reason ``challenge``); no signals are computed.
- the browser gave up (``final_attempt``), no profile is ``ok``, or the folder is invalid: the
  scans are stored when they can be, ``job_failed`` is written and the case goes to review
  (reason ``capture_failed``).
- otherwise the scans, artifacts and ``captured`` are written and ``compute_signals`` follows.

A ``recheck`` job (§16) confirms a follow-up check of a ``notified`` or ``taken_down`` case: its
scans and screenshots are stored as new evidence and ``followup.recheck.apply_browser`` moves
the case on; a failed or invalid capture leaves the HTTP check to stand, never a review.

A case that left ``detected`` meanwhile gets nothing, and a job whose scans exist is only
cleaned up, so a redelivered message is harmless. Objects keep their paths across retries
(the scan ids are fixed when the job is issued); an object an earlier attempt uploaded is
reused when its stored SHA-256 matches (K72). The buttons and links the browser saw are
classified like the pre-check's (``cta_targets``, K57, K71) and promoted to candidates.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any

from pydantic import ValidationError
from redis import Redis
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from kalkan.db.models import (
    Brand,
    BrandAsset,
    Case,
    CaseEvent,
    Customer,
    Domain,
    OfficialDomain,
    Scan,
    Signal,
)
from kalkan.db.session import Database
from kalkan.discovery.catalog import profile_for
from kalkan.discovery.matcher import Matcher
from kalkan.domain.case_state import transition
from kalkan.domain.enums import ArtifactKind, CaseState, EventType, Profile, ScanStage
from kalkan.logging import get_logger
from kalkan.scanning.capture_contract import (
    ARTIFACTS_ROOT,
    FILES,
    META_FILE,
    META_MAX_BYTES,
    CaptureMeta,
    CaptureRecord,
    Outcome,
    ProfileResult,
    Purpose,
    file_name,
    job_dir,
)
from kalkan.scanning.capture_jobs import load_record, release_pending
from kalkan.scanning.html import LinkInfo
from kalkan.scanning.precheck import (
    MAX_CTA_CANDIDATES,
    CtaTarget,
    classify_links,
    contains_brand,
    cta_summary,
    host_of,
    is_official,
    promoted_targets,
    record_cta_candidates,
    without_hosts,
)
from kalkan.scanning.references import ASSET_KIND
from kalkan.storage import paths
from kalkan.storage.artifacts import record_artifact
from kalkan.storage.gcs import ObjectExistsError, ObjectStore, StoredObject, make_key, sha256_hex

SYSTEM = "system"
CHALLENGE = "challenge"
CAPTURE_FAILED = "capture_failed"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SCREENSHOTS = frozenset({ArtifactKind.SCREENSHOT, ArtifactKind.SCREENSHOT_FULL})
# The scheduler's janitor removes job folders the hand-off left behind (K98).
STALE_AFTER = timedelta(hours=24)
_JOB_DIR_NAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(\.partial)?$"
)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
# O_NONBLOCK: opening a FIFO planted in the folder must not hang the worker.
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC
# Browser link tags as the pre-check's classification knows them.
_TAGS = {
    "meta-refresh": "redirect",
    "navigation": "redirect",
    "onclick": "button",
    "data-href": "button",
    "area": "a",
}

log = get_logger(__name__)


class InvalidCaptureError(ValueError):
    """The job folder breaks the hand-off contract; persisting it again would not help."""


class PersistStatus(StrEnum):
    STORED = "stored"  # evidence stored; compute_signals comes next
    CHALLENGE = "challenge"  # stored, and the case went to review
    FAILED = "failed"  # the browser gave up: job_failed and review (capture_failed)
    INVALID = "invalid"  # the folder broke the contract: job_failed and review
    SKIPPED = "skipped"  # already persisted, or the case left detected
    UNKNOWN = "unknown"  # no record for this job id
    REFERENCE = "reference"  # a reference job, whatever came of it
    RECHECK = "recheck"  # a follow-up confirmation, whatever came of it


@dataclass(frozen=True)
class PersistResult:
    job_id: uuid.UUID
    status: PersistStatus
    case_id: uuid.UUID | None = None
    new_match_ids: list[uuid.UUID] = field(default_factory=list)
    # A reference job stored screenshots of this brand: hash_brand_assets comes next.
    hashed_brand_id: uuid.UUID | None = None

    @property
    def compute_signals(self) -> bool:
        return self.status is PersistStatus.STORED


# --- reading the job folder -------------------------------------------------------------------


class JobFolder:
    """A job folder opened once; its files are opened relative to it, never through a link."""

    def __init__(self, path: Path) -> None:
        try:
            self._fd = os.open(path, _DIR_FLAGS)
        except FileNotFoundError as exc:
            raise InvalidCaptureError("the job folder is missing") from exc
        except OSError as exc:  # a link (ELOOP) or not a directory
            raise InvalidCaptureError(f"the job folder cannot be opened ({exc.strerror})") from exc

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> JobFolder:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _open(self, name: str, max_bytes: int) -> int:
        try:
            fd = os.open(name, _FILE_FLAGS, dir_fd=self._fd)
        except FileNotFoundError as exc:
            raise InvalidCaptureError(f"{name} is missing") from exc
        except OSError as exc:
            raise InvalidCaptureError(f"{name} cannot be opened ({exc.strerror})") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise InvalidCaptureError(f"{name} is not a regular file")
            if info.st_nlink != 1:
                raise InvalidCaptureError(f"{name} has other links")
            if info.st_size > max_bytes:
                raise InvalidCaptureError(f"{name} is larger than {max_bytes} bytes")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def check(self, name: str, max_bytes: int, magic: bytes = b"") -> None:
        """Type, size and leading bytes, without reading the whole file."""
        with os.fdopen(self._open(name, max_bytes), "rb") as stream:
            if magic and stream.read(len(magic)) != magic:
                raise InvalidCaptureError(f"{name} is not a PNG file")

    def read(self, name: str, max_bytes: int, magic: bytes = b"") -> bytes:
        with os.fdopen(self._open(name, max_bytes), "rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise InvalidCaptureError(f"{name} is larger than {max_bytes} bytes")
        if magic and not data.startswith(magic):
            raise InvalidCaptureError(f"{name} is not a PNG file")
        return data


def read_meta(folder: JobFolder, record: CaptureRecord) -> CaptureMeta:
    raw = folder.read(META_FILE, META_MAX_BYTES)
    try:
        meta = CaptureMeta.model_validate_json(raw)
    except ValidationError as exc:
        # Only the location and type: input values come from the page.
        first = exc.errors(include_input=False, include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"]) or "root"
        raise InvalidCaptureError(
            f"meta.json is invalid ({exc.error_count()} errors; {where}: {first['type']})"
        ) from None
    if meta.job_id != record.job_id:
        raise InvalidCaptureError("meta.json belongs to another job")
    profiles = [result.profile for result in meta.profiles]
    if len(set(profiles)) != len(profiles):
        raise InvalidCaptureError("meta.json lists a profile twice")
    if not set(profiles) <= set(record.scan_ids):
        raise InvalidCaptureError("meta.json has a profile the job did not ask for")
    for result in meta.profiles:
        if len(set(result.files)) != len(result.files):
            raise InvalidCaptureError("meta.json lists a file twice")
        if not set(result.files) <= FILES.keys():
            raise InvalidCaptureError("meta.json lists a file kind the browser does not write")
        if result.http_status is not None and not 100 <= result.http_status <= 999:
            raise InvalidCaptureError("meta.json has an impossible HTTP status")
    return meta


def _magic(kind: ArtifactKind) -> bytes:
    return PNG_MAGIC if kind in SCREENSHOTS else b""


def put_once(
    store: ObjectStore, bucket: str, path: str, data: bytes, content_type: str
) -> StoredObject:
    """Upload ``data``; an object an earlier attempt stored is reused if it is the same file."""
    try:
        return store.put(bucket, path, data, content_type)
    except ObjectExistsError:
        key = make_key(bucket, path)
        digest = sha256_hex(data)
        if store.stored_sha256(key) != digest:
            raise
        return StoredObject(key, digest, len(data), content_type)


def _store_case_files(
    folder: JobFolder,
    store: ObjectStore,
    bucket: str,
    record: CaptureRecord,
    meta: CaptureMeta,
) -> list[tuple[Profile, ArtifactKind, StoredObject]]:
    if record.case_id is None:  # pragma: no cover - checked by the caller
        raise ValueError("a case job needs a case id")
    wanted = [(result.profile, kind) for result in meta.profiles for kind in result.files]
    # Every file is checked before the first upload, so a bad folder stores nothing.
    for profile, kind in wanted:
        folder.check(file_name(kind, profile), FILES[kind].max_bytes, _magic(kind))
    stored = []
    for profile, kind in wanted:
        spec = FILES[kind]
        data = folder.read(file_name(kind, profile), spec.max_bytes, _magic(kind))
        path = paths.case_artifact(
            record.case_id, record.scan_ids[profile], kind, profile, spec.extension
        )
        stored.append((profile, kind, put_once(store, bucket, path, data, spec.content_type)))
    return stored


# --- cleaning up ------------------------------------------------------------------------------


def _remove(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    try:
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        log.warning("persist.cleanup_failed", path=str(path), error=exc.strerror)
        return False
    return True


def remove_job_dir(job_id: uuid.UUID, root: Path = ARTIFACTS_ROOT) -> None:
    """Delete a finished job folder (never the browser's ``.partial`` one)."""
    _remove(job_dir(job_id, root))


def remove_stale_job_dirs(
    root: Path = ARTIFACTS_ROOT, *, now: float, older_than: timedelta = STALE_AFTER
) -> int:
    """Delete job folders (finished or ``.partial``) older than ``older_than``; returns how many.

    Only names that look like job folders are touched.
    """
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        log.warning("persist.no_artifacts_dir", path=str(root))
        return 0
    removed = 0
    for entry in entries:
        if not _JOB_DIR_NAME.match(entry.name):
            continue
        try:
            modified = entry.stat(follow_symlinks=False).st_mtime
        except FileNotFoundError:
            continue
        if now - modified >= older_than.total_seconds() and _remove(Path(entry.path)):
            removed += 1
    return removed


def _finish(redis: Redis, record: CaptureRecord, root: Path) -> None:
    remove_job_dir(record.job_id, root)
    if record.case_id is not None:
        release_pending(redis, record.case_id, record.job_id)


# --- case jobs --------------------------------------------------------------------------------


def _clean(value: Any) -> Any:
    """Text Postgres accepts: no NUL characters, no lone surrogates."""
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8").replace("\x00", "")
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    return value


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _skip_reason(session: Session, record: CaptureRecord, case: Case | None) -> str | None:
    if case is None:
        return "no such case"
    if case.customer_id != record.customer_id:
        return "the record does not match the case"
    if case.state is not CaseState.DETECTED:
        return f"the case is {case.state.value}"
    customer = session.get(Customer, case.customer_id)
    if customer is None or customer.is_demo:
        return "demo workspace"
    persisted = session.scalar(select(exists().where(Scan.id.in_(list(record.scan_ids.values())))))
    return "already persisted" if persisted else None


def _locked_case(session: Session, record: CaptureRecord) -> tuple[Case | None, str | None]:
    case = session.get(Case, record.case_id, with_for_update=True)
    reason = _skip_reason(session, record, case)
    return (case, None) if reason is None else (None, reason)


def _give_up_case(session: Session, case: Case, *, job: str, error: str, at: datetime) -> None:
    """``job_failed`` and review (``capture_failed``): the reconciler leaves the case alone."""
    session.add(
        CaseEvent(
            customer_id=case.customer_id,
            case_id=case.id,
            at=at,
            type=EventType.JOB_FAILED,
            actor=SYSTEM,
            data={"job": job, "error": _clean(error)[:500]},
        )
    )
    transition(session, case, CaseState.IN_REVIEW, actor=SYSTEM, reason=CAPTURE_FAILED, at=at)


def _scan(
    case: Case,
    scan_id: uuid.UUID,
    profile: Profile,
    result: ProfileResult | None,
    meta: CaptureMeta,
    at: datetime,
) -> Scan:
    common: dict[str, Any] = {
        "id": scan_id,
        "customer_id": case.customer_id,
        "case_id": case.id,
        "stage": ScanStage.BROWSER,
        "profile": profile,
        "proxy_country": meta.proxy_country,
    }
    if result is None:
        error = meta.error or "the browser returned no result for this profile"
        return Scan(
            **common,
            started_at=at,
            finished_at=at,
            outcome=Outcome.ERROR.value,
            error=_clean(error),
        )
    return Scan(
        **common,
        started_at=_aware(result.started_at),
        finished_at=_aware(result.finished_at),
        outcome=result.outcome.value,
        http_status=result.http_status,
        final_url=_clean(result.final_url),
        redirect_chain=_clean(list(result.redirect_chain)),
        title=_clean(result.title),
        text_excerpt=_clean(result.text_excerpt) or None,
        forms=_clean([form.model_dump(mode="json") for form in result.forms]),
        buttons=_clean([button.model_dump(mode="json") for button in result.buttons]),
        error=_clean(result.error),
    )


def browser_links(result: ProfileResult) -> list[LinkInfo]:
    """Redirects, form and button targets, then links, in the pre-check's tag names."""
    hops = [*result.redirect_chain, result.final_url]
    links = [LinkInfo(url, "", "redirect") for url in hops if url]
    for form in result.forms:
        if form.action:
            links.append(LinkInfo(form.action, "", "form"))
        links += [LinkInfo(b.target, b.text, "button") for b in form.buttons if b.target]
    links += [LinkInfo(b.target, b.text, "button") for b in result.buttons if b.target]
    links += [LinkInfo(link.url, link.text, _TAGS.get(link.tag, link.tag)) for link in result.links]
    return [LinkInfo(_clean(link.url), _clean(link.text), link.tag) for link in links if link.url]


def _cta_targets(
    session: Session, case: Case, captured: Sequence[tuple[Scan, ProfileResult]], at: datetime
) -> list[uuid.UUID]:
    """The ``cta_targets`` signal of each captured scan; promoted targets become candidates."""
    brand = session.get(Brand, case.brand_id)
    domain = session.get(Domain, case.domain_id)
    if brand is None or domain is None:
        return []
    officials = sorted(
        set(
            session.scalars(
                select(OfficialDomain.domain).where(OfficialDomain.customer_id == case.customer_id)
            )
        )
    )
    matcher = Matcher([profile_for(session, brand)])
    # The pre-check fetched the favicon; the browser does not.
    favicon_match = session.scalar(
        select(Signal.value)
        .join(Scan, Scan.id == Signal.scan_id)
        .where(
            Signal.case_id == case.id,
            Signal.name == "favicon_match",
            Scan.stage == ScanStage.HTTP,
        )
        .order_by(Scan.started_at.desc())
        .limit(1)
    )
    chosen: dict[str, CtaTarget] = {}
    for scan, result in captured:
        links = browser_links(result)
        targets = classify_links(links, domain.name, officials, matcher)
        final_host = host_of(result.final_url)
        own = (domain.name, final_host)
        lure_page = (
            contains_brand(without_hosts(result.title or "", own), brand.keywords)
            or contains_brand(without_hosts(result.text_excerpt, own), brand.keywords)
            or favicon_match is True
        ) and not is_official(final_host, officials)
        promoted = promoted_targets(targets, brand.keywords, lure_page=lure_page)
        counts, detail = cta_summary(targets, promoted)
        refresh = next((link.url for link in result.links if link.tag == "meta-refresh"), None)
        session.add(
            Signal(
                customer_id=case.customer_id,
                case_id=case.id,
                scan_id=scan.id,
                name="cta_targets",
                value=counts,
                detail={
                    "targets": detail,
                    "meta_refresh": _clean(refresh),
                    "js_redirects": _clean(
                        [link.url for link in result.links if link.tag == "navigation"][:10]
                    ),
                    "frames": _clean(
                        [link.url for link in result.links if link.tag in ("frame", "iframe")][:10]
                    ),
                },
            )
        )
        for target in promoted:
            chosen.setdefault(target.host, target)
    promoted_all = list(chosen.values())[:MAX_CTA_CANDIDATES]
    if not promoted_all:
        return []
    return record_cta_candidates(session, brand, case.number, promoted_all, at)


def _write_case(
    session: Session,
    case: Case,
    record: CaptureRecord,
    meta: CaptureMeta,
    stored: Sequence[tuple[Profile, ArtifactKind, StoredObject]],
    at: datetime,
) -> tuple[PersistStatus, list[uuid.UUID]]:
    results = {result.profile: result for result in meta.profiles}
    scans: list[tuple[Scan, ProfileResult | None]] = []
    for profile, scan_id in record.scan_ids.items():
        result = results.get(profile)
        scan = _scan(case, scan_id, profile, result, meta, at)
        session.add(scan)
        scans.append((scan, result))
    session.flush()
    for profile, kind, obj in stored:
        record_artifact(
            session, case, obj, kind=kind, scan_id=record.scan_ids[profile], profile=profile
        )
    outcomes = {scan.profile: scan.outcome for scan, _ in scans if scan.profile is not None}
    usable = {Outcome.OK.value, Outcome.CHALLENGE.value}
    if meta.final_attempt or not usable & set(outcomes.values()):
        errors = [result.error for _, result in scans if result is not None and result.error]
        error = meta.error or (errors[0] if errors else "no profile was captured")
        _give_up_case(session, case, job="browser_capture", error=error, at=at)
        return PersistStatus.FAILED, []
    session.add(
        CaseEvent(
            customer_id=case.customer_id,
            case_id=case.id,
            at=at,
            type=EventType.CAPTURED,
            actor=SYSTEM,
            data={
                "job_id": str(record.job_id),
                "outcomes": {profile.value: outcome for profile, outcome in outcomes.items()},
                "proxy_country": meta.proxy_country,
            },
        )
    )
    captured = [
        (scan, result)
        for scan, result in scans
        if result is not None and result.outcome is Outcome.OK
    ]
    new_ids = _cta_targets(session, case, captured, at)
    if Outcome.CHALLENGE.value in outcomes.values():
        # Never solved or waited out: a person looks at it (product.md §11).
        transition(session, case, CaseState.IN_REVIEW, actor=SYSTEM, reason=CHALLENGE, at=at)
        return PersistStatus.CHALLENGE, new_ids
    return PersistStatus.STORED, new_ids


def _persist_case(
    database: Database,
    redis: Redis,
    store: ObjectStore,
    bucket: str,
    record: CaptureRecord,
    *,
    now: Callable[[], datetime],
    root: Path,
) -> PersistResult:
    job_id, case_id = record.job_id, record.case_id
    with database.worker_session() as session:
        reason = _skip_reason(session, record, session.get(Case, case_id))
    if reason is not None:
        log.info("persist.skipped", job_id=str(job_id), case_id=str(case_id), reason=reason)
        _finish(redis, record, root)
        return PersistResult(job_id, PersistStatus.SKIPPED, case_id)
    try:
        with JobFolder(job_dir(job_id, root)) as folder:
            meta = read_meta(folder, record)
            stored = _store_case_files(folder, store, bucket, record, meta)
    except InvalidCaptureError as exc:
        log.warning("persist.invalid", job_id=str(job_id), case_id=str(case_id), error=str(exc))
        with database.worker_session() as session:
            case, _ = _locked_case(session, record)
            if case is not None:
                error = f"invalid capture: {exc}"
                _give_up_case(session, case, job="persist_capture", error=error, at=now())
        _finish(redis, record, root)
        return PersistResult(job_id, PersistStatus.INVALID, case_id)

    new_ids: list[uuid.UUID] = []
    with database.worker_session() as session:
        case, reason = _locked_case(session, record)
        if case is None:
            # It changed while the files were uploaded; the objects stay, unreferenced.
            status = PersistStatus.SKIPPED
        else:
            status, new_ids = _write_case(session, case, record, meta, stored, now())
    _finish(redis, record, root)
    log.info(
        "persist.done",
        job_id=str(job_id),
        case_id=str(case_id),
        status=status.value,
        reason=reason,
        artifacts=len(stored),
        candidates=len(new_ids),
    )
    return PersistResult(job_id, status, case_id, new_ids)


# --- follow-up confirmations ------------------------------------------------------------------


def _recheck_skip_reason(session: Session, record: CaptureRecord, case: Case | None) -> str | None:
    from kalkan.followup.recheck import FOLLOWED

    if case is None:
        return "no such case"
    if case.customer_id != record.customer_id:
        return "the record does not match the case"
    if case.state not in FOLLOWED:
        return f"the case is {case.state.value}"
    persisted = session.scalar(select(exists().where(Scan.id.in_(list(record.scan_ids.values())))))
    return "already persisted" if persisted else None


def _persist_recheck(
    database: Database,
    redis: Redis,
    store: ObjectStore,
    bucket: str,
    record: CaptureRecord,
    *,
    now: Callable[[], datetime],
    root: Path,
) -> PersistResult:
    from kalkan.followup.recheck import apply_browser
    from kalkan.followup.rules import BrowserPage

    job_id, case_id = record.job_id, record.case_id
    with database.worker_session() as session:
        reason = _recheck_skip_reason(session, record, session.get(Case, case_id))
    meta: CaptureMeta | None = None
    stored: list[tuple[Profile, ArtifactKind, StoredObject]] = []
    if reason is None:
        try:
            with JobFolder(job_dir(job_id, root)) as folder:
                meta = read_meta(folder, record)
                stored = _store_case_files(folder, store, bucket, record, meta)
        except InvalidCaptureError as exc:
            log.warning("persist.recheck_invalid", job_id=str(job_id), error=str(exc))
            meta = None
    if reason is None:
        with database.worker_session() as session:
            case = session.get(Case, case_id, with_for_update=True)
            reason = _recheck_skip_reason(session, record, case)
            if case is not None and reason is None:
                pages: list[BrowserPage] = []
                if meta is not None:
                    results = {result.profile: result for result in meta.profiles}
                    for profile, scan_id in record.scan_ids.items():
                        result = results.get(profile)
                        session.add(_scan(case, scan_id, profile, result, meta, now()))
                    session.flush()
                    for profile, kind, obj in stored:
                        record_artifact(
                            session,
                            case,
                            obj,
                            kind=kind,
                            scan_id=record.scan_ids[profile],
                            profile=profile,
                        )
                    if not meta.final_attempt:
                        pages = [
                            BrowserPage(
                                result.outcome.value,
                                result.http_status,
                                result.text_excerpt,
                                result.title or "",
                            )
                            for result in meta.profiles
                        ]
                apply_browser(session, case, pages, job_id=job_id, now=now())
    _finish(redis, record, root)
    log.info(
        "persist.recheck_done",
        job_id=str(job_id),
        case_id=str(case_id),
        reason=reason,
        artifacts=len(stored),
    )
    return PersistResult(job_id, PersistStatus.RECHECK, case_id)


# --- reference jobs ---------------------------------------------------------------------------


def _reference_brand(session: Session, record: CaptureRecord, *, lock: bool) -> Brand | None:
    # Like the favicons: the brand cannot be deleted while its assets are recorded.
    locking = {"key_share": True} if lock else None
    brand = session.get(Brand, record.brand_id, with_for_update=locking)
    if brand is None or brand.customer_id != record.customer_id:
        return None
    customer = session.get(Customer, brand.customer_id)
    return None if customer is None or customer.is_demo else brand


def _persist_reference(
    database: Database,
    store: ObjectStore,
    bucket: str,
    record: CaptureRecord,
    *,
    root: Path,
) -> PersistResult:
    """Keep the ``ok`` viewport screenshots as reference assets; failures are only logged."""
    job_id = record.job_id
    result = PersistResult(job_id, PersistStatus.REFERENCE)
    with database.worker_session() as session:
        known = _reference_brand(session, record, lock=False) is not None
    if not known:
        log.info("persist.reference_skipped", job_id=str(job_id), brand_id=str(record.brand_id))
        remove_job_dir(job_id, root)
        return result
    kind = ArtifactKind.SCREENSHOT
    try:
        with JobFolder(job_dir(job_id, root)) as folder:
            meta = read_meta(folder, record)
            shots = [r for r in meta.profiles if r.outcome is Outcome.OK and kind in r.files]
            stored = []
            for shot in shots:
                data = folder.read(file_name(kind, shot.profile), FILES[kind].max_bytes, PNG_MAGIC)
                path = paths.brand_reference(record.brand_id, record.issued_at, shot.profile)
                stored.append((shot, put_once(store, bucket, path, data, FILES[kind].content_type)))
    except InvalidCaptureError as exc:
        log.warning("persist.reference_invalid", job_id=str(job_id), error=str(exc))
        remove_job_dir(job_id, root)
        return result
    added = False
    with database.worker_session() as session:
        brand = _reference_brand(session, record, lock=True)
        for shot, obj in stored if brand is not None else []:
            asset_id = uuid.uuid5(record.brand_id, f"reference:{job_id}:{shot.profile.value}")
            if session.get(BrandAsset, asset_id) is not None:
                continue
            session.add(
                BrandAsset(
                    id=asset_id,
                    customer_id=record.customer_id,
                    brand_id=record.brand_id,
                    kind=ASSET_KIND[shot.profile],
                    storage_key=obj.key,
                    sha256=obj.sha256,
                    source_url=_clean(shot.final_url),
                    text_excerpt=_clean(shot.text_excerpt) or None,
                    captured_at=_aware(shot.finished_at),
                )
            )
            added = True
    remove_job_dir(job_id, root)
    log.info(
        "persist.references_stored",
        job_id=str(job_id),
        brand_id=str(record.brand_id),
        stored=[shot.profile.value for shot, _ in stored],
        outcomes={r.profile.value: r.outcome.value for r in meta.profiles},
    )
    return (
        PersistResult(job_id, PersistStatus.REFERENCE, hashed_brand_id=record.brand_id)
        if added
        else result
    )


# --- entry points -----------------------------------------------------------------------------


def persist_capture(
    database: Database,
    redis: Redis,
    store: ObjectStore,
    bucket: str,
    job_id: uuid.UUID,
    *,
    now: Callable[[], datetime],
    root: Path = ARTIFACTS_ROOT,
) -> PersistResult:
    """Store one finished job folder. Raises only for errors worth retrying (storage, DB)."""
    record = load_record(redis, job_id)
    if record is None or (record.purpose is not Purpose.REFERENCE and record.case_id is None):
        log.warning("persist.unknown_job", job_id=str(job_id))
        remove_job_dir(job_id, root)
        return PersistResult(job_id, PersistStatus.UNKNOWN)
    if record.purpose is Purpose.REFERENCE:
        return _persist_reference(database, store, bucket, record, root=root)
    if record.purpose is Purpose.RECHECK:
        return _persist_recheck(database, redis, store, bucket, record, now=now, root=root)
    return _persist_case(database, redis, store, bucket, record, now=now, root=root)


def give_up(
    database: Database,
    redis: Redis,
    job_id: uuid.UUID,
    error: str,
    *,
    at: datetime,
    root: Path = ARTIFACTS_ROOT,
) -> None:
    """persist_capture's last attempt failed: ``job_failed`` and review for a case job."""
    record = load_record(redis, job_id)
    if record is not None and record.purpose is Purpose.RECHECK:
        # The HTTP check stands; the next check may issue a new confirmation.
        log.error("persist.recheck_given_up", job_id=str(job_id), error=error[:500])
        _finish(redis, record, root)
        return
    if record is not None and record.purpose is Purpose.CASE and record.case_id is not None:
        with database.worker_session() as session:
            case, _ = _locked_case(session, record)
            if case is not None:
                _give_up_case(session, case, job="persist_capture", error=error, at=at)
        _finish(redis, record, root)
        return
    event = "persist.reference_given_up" if record is not None else "persist.unknown_job"
    log.error(event, job_id=str(job_id), error=error[:500])
    remove_job_dir(job_id, root)
