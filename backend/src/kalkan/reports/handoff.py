"""The monthly report's hand-off between worker-default and worker-browser (architecture.md §21).

Both mount the ``artifacts-tmp`` volume. For report ``<id>``:

- ``reports/<id>/token``: the print token, written by ``build_report`` (worker-default) and
  deleted by ``print_report`` (worker-browser) as soon as it is read. The token never goes
  through Redis, so no message, Dramatiq log line or Sentry event can carry it.
- ``reports/<id>/report.pdf``: written by the browser through ``report.pdf.partial``, read by
  ``persist_report`` (worker-default) like a capture folder: no links, a size limit, ``%PDF-``.

Messages carry the report id and the print attempt only. ``print_report`` lives on the
browser's ``capture`` queue; ``persist_report`` and ``report_print_failed`` on ``persist``,
the only other queue the browser's Redis user may write (K82).
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

import dramatiq

from kalkan.scanning.capture_contract import ARTIFACTS_ROOT, CAPTURE_QUEUE, PERSIST_QUEUE
from kalkan.scanning.persist import InvalidCaptureError, JobFolder

ROOT = ARTIFACTS_ROOT / "reports"
TOKEN_FILE = "token"  # noqa: S105 - a file name
PDF_FILE = "report.pdf"
PARTIAL_SUFFIX = ".partial"
TOKEN_MAX_BYTES = 200
PDF_MAX_BYTES = 20 * 1024 * 1024
PDF_MAGIC = b"%PDF-"

PRINT_ACTOR = "print_report"
PRINT_QUEUE = CAPTURE_QUEUE
PERSIST_ACTOR = "persist_report"
FAILED_ACTOR = "report_print_failed"
REPORT_PERSIST_QUEUE = PERSIST_QUEUE

STALE_AFTER = timedelta(hours=24)
_REPORT_DIR = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC


class HandoffError(ValueError):
    """A hand-off file is missing or breaks the contract."""


def report_dir(report_id: uuid.UUID, root: Path = ROOT) -> Path:
    return root / str(report_id)


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, _NEW_FILE, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def write_token(report_id: uuid.UUID, token: str, root: Path = ROOT) -> None:
    """Replace the report's folder with one holding only the new token."""
    folder = report_dir(report_id, root)
    remove(report_id, root)
    folder.mkdir(parents=True, mode=0o700)
    _write_new(folder / TOKEN_FILE, token.encode())


def _read(report_id: uuid.UUID, name: str, max_bytes: int, root: Path) -> bytes:
    try:
        with JobFolder(report_dir(report_id, root)) as folder:
            return folder.read(name, max_bytes)
    except InvalidCaptureError as exc:
        raise HandoffError(str(exc)) from exc


def take_token(report_id: uuid.UUID, root: Path = ROOT) -> str:
    """Read the token and delete its file: it is used once (§17)."""
    try:
        data = _read(report_id, TOKEN_FILE, TOKEN_MAX_BYTES, root)
    finally:
        (report_dir(report_id, root) / TOKEN_FILE).unlink(missing_ok=True)
    token = data.decode("ascii", errors="replace").strip()
    if not token:
        raise HandoffError("the token file is empty")
    return token


def write_pdf(report_id: uuid.UUID, data: bytes, root: Path = ROOT) -> None:
    folder = report_dir(report_id, root)
    partial = folder / (PDF_FILE + PARTIAL_SUFFIX)
    partial.unlink(missing_ok=True)
    _write_new(partial, data)
    partial.rename(folder / PDF_FILE)


def read_pdf(report_id: uuid.UUID, root: Path = ROOT) -> bytes:
    data = _read(report_id, PDF_FILE, PDF_MAX_BYTES, root)
    if not data.startswith(PDF_MAGIC):
        raise HandoffError(f"{PDF_FILE} is not a PDF file")
    return data


def remove(report_id: uuid.UUID, root: Path = ROOT) -> None:
    folder = report_dir(report_id, root)
    if folder.is_symlink():
        folder.unlink()
    elif folder.exists():
        shutil.rmtree(folder)


def remove_stale(root: Path = ROOT, *, now: float, older_than: timedelta = STALE_AFTER) -> int:
    """Delete report folders older than ``older_than`` (a token there has long expired)."""
    try:
        entries = [entry for entry in root.iterdir() if _REPORT_DIR.match(entry.name)]
    except FileNotFoundError:
        return 0
    removed = 0
    for entry in entries:
        try:
            old = now - entry.lstat().st_mtime >= older_than.total_seconds()
        except FileNotFoundError:
            continue
        if old:
            remove(uuid.UUID(entry.name), root)
            removed += 1
    return removed


def _send(queue: str, actor: str, delay: timedelta | None = None, **kwargs: str) -> None:
    message: dramatiq.Message[None] = dramatiq.Message(
        queue_name=queue, actor_name=actor, args=(), kwargs=kwargs, options={}
    )
    milliseconds = int(delay.total_seconds() * 1000) if delay else None
    dramatiq.get_broker().enqueue(message, delay=milliseconds)


def send_print(report_id: uuid.UUID, attempt: int, *, delay: timedelta | None = None) -> None:
    """From worker-default: ``print_report`` is declared by worker-browser only."""
    _send(PRINT_QUEUE, PRINT_ACTOR, delay, report_id=str(report_id), attempt=str(attempt))


def send_persist(report_id: uuid.UUID) -> None:
    _send(REPORT_PERSIST_QUEUE, PERSIST_ACTOR, report_id=str(report_id))


def send_failed(report_id: uuid.UUID, attempt: int) -> None:
    _send(REPORT_PERSIST_QUEUE, FAILED_ACTOR, report_id=str(report_id), attempt=str(attempt))
