"""The browser stage's hand-off (architecture.md §10, §11, §24; K6).

worker-default is trusted (it reads the database). It issues a capture job in two parts:
:class:`CaptureJob`, the message the browser receives, and :class:`CaptureRecord`, a record
kept in Redis under a key the browser's Redis user cannot reach. The browser has no database
and no cloud credentials. It writes the files named here and ``meta.json``
(:class:`CaptureMeta`) into ``ARTIFACTS_ROOT/<job_id>/`` and queues
``persist_capture(job_id)``. worker-default trusts only its own record, never identity fields
from the browser, and reads only the files named here.

Every string has a length cap, so a hostile page cannot grow ``meta.json`` without bound; the
browser truncates before writing and worker-default validates before reading.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from kalkan.domain.enums import ArtifactKind, Profile

ARTIFACTS_ROOT = Path("/artifacts-tmp")
META_FILE = "meta.json"
META_MAX_BYTES = 2 * 1024 * 1024
# The browser writes into <job_id>.partial/ and renames it when meta.json is complete.
PARTIAL_SUFFIX = ".partial"

CAPTURE_QUEUE = "capture"
PERSIST_QUEUE = "persist"
SIGNALS_QUEUE = "signals"
PERSIST_ACTOR = "persist_capture"
COMPUTE_SIGNALS_ACTOR = "compute_signals"

# Redis keys. The browser's ACL user can reach none of the capture:* keys.
# One live job per case: set by the issuer, removed by persist_capture.
PENDING_KEY = "capture:pending:{case_id}"
PENDING_TTL_SECONDS = 50 * 3600
JOB_KEY = "capture:job:{job_id}"
# As long as the pending marker: the browser drops jobs 2 hours before that, so a job it still
# starts always finds its record when persist_capture runs.
JOB_TTL_SECONDS = PENDING_TTL_SECONDS
# Approximate proxy bytes per UTC day (YYYYMMDD); the browser reads and increments it.
PROXY_BYTES_KEY = "kalkan:proxy:bytes:{day}"
PROXY_BYTES_TTL_SECONDS = 40 * 24 * 3600

MAX_URL = 2048
MAX_URLS = 5
MAX_REDIRECTS = 20
MAX_TITLE = 500
MAX_TEXT = 20_000
MAX_LABEL = 200
MAX_ERROR = 2000
MAX_FORMS = 50
MAX_INPUTS_PER_FORM = 200
MAX_BUTTONS = 100
MAX_LINKS = 200

Url = Annotated[str, Field(max_length=MAX_URL)]
Label = Annotated[str, Field(max_length=MAX_LABEL)]
Short = Annotated[str, Field(max_length=40)]


class Purpose(StrEnum):
    CASE = "case"  # a detected case's site
    REFERENCE = "reference"  # an official site's reference screenshots (brand_assets)
    # A notified or taken-down case confirmed from the target market (followup.recheck, §16).
    RECHECK = "recheck"


class Outcome(StrEnum):
    """``scans.outcome`` of one browser profile."""

    OK = "ok"
    CHALLENGE = "challenge"  # a bot challenge; never solved, the case goes to review
    ERROR = "error"  # navigation or page failure


class FileSpec(NamedTuple):
    extension: str
    content_type: str
    max_bytes: int


FILES: dict[ArtifactKind, FileSpec] = {
    ArtifactKind.SCREENSHOT: FileSpec("png", "image/png", 10 * 1024 * 1024),
    ArtifactKind.SCREENSHOT_FULL: FileSpec("png", "image/png", 40 * 1024 * 1024),
    ArtifactKind.HTML: FileSpec("html", "text/html; charset=utf-8", 15 * 1024 * 1024),
    ArtifactKind.HAR: FileSpec("har", "application/json", 50 * 1024 * 1024),
}


def file_name(kind: ArtifactKind, profile: Profile) -> str:
    """``screenshot-desktop.png``: the only names worker-default reads from a job folder."""
    return f"{kind.value}-{profile.value}.{FILES[kind].extension}"


def job_dir(job_id: uuid.UUID, root: Path = ARTIFACTS_ROOT) -> Path:
    return root / str(job_id)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaptureJob(_Strict):
    """The message payload of ``browser_capture`` (``model_dump(mode="json")``)."""

    job_id: uuid.UUID
    purpose: Purpose
    urls: list[Url] = Field(min_length=1, max_length=MAX_URLS)  # entry URLs, tried in order
    # The target market. The browser uses PROXY_MAP[country], or PROXY_DEFAULT_COUNTRY's entry
    # when there is none, and takes locale and timezone from the country it actually used.
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    profiles: list[Profile] = Field(min_length=1, max_length=2)
    issued_at: datetime


class CaptureRecord(_Strict):
    """What only worker-default knows about a job (Redis ``JOB_KEY``)."""

    job_id: uuid.UUID
    purpose: Purpose
    customer_id: uuid.UUID
    brand_id: uuid.UUID
    case_id: uuid.UUID | None  # None for reference jobs
    # Fixed when the job is issued, so storage paths stay the same across retries.
    scan_ids: dict[Profile, uuid.UUID]
    urls: list[Url] = Field(min_length=1, max_length=MAX_URLS)
    issued_at: datetime


class InputInfo(_Strict):
    type: Short
    name: Label = ""
    id: Label = ""
    placeholder: Label = ""
    autocomplete: Short = ""
    inputmode: Short = ""
    maxlength: int | None = None
    label: Label = ""  # the visible label or aria-label
    visible: bool = True
    # A hint from scanning/form_patterns.yaml: password, card_number, card_cvv, card_expiry,
    # iban, phone, email, username, otp, amount; empty when nothing matched.
    kind: Short = ""


class ButtonInfo(_Strict):
    text: Label = ""
    type: Short = ""
    target: Url | None = None  # href, formaction or a URL found in onclick/data-href
    visible: bool = True
    # A hint from form_patterns.yaml: deposit, login, register; empty when nothing matched.
    intent: Short = ""


class FormInfo(_Strict):
    action: Url | None = None  # absolute
    method: Short = "get"
    frame_url: Url | None = None  # set for forms inside a child frame
    inputs: list[InputInfo] = Field(default_factory=list, max_length=MAX_INPUTS_PER_FORM)
    buttons: list[ButtonInfo] = Field(default_factory=list, max_length=MAX_BUTTONS)


class LinkInfo(_Strict):
    url: Url
    text: Label = ""
    # a, area, button, form, frame, iframe, onclick, data-href, meta-refresh, redirect,
    # navigation (a client-side navigation seen during the capture)
    tag: Short


class ProfileResult(_Strict):
    profile: Profile
    outcome: Outcome
    started_at: datetime
    finished_at: datetime
    http_status: int | None = None
    final_url: Url | None = None
    redirect_chain: list[Url] = Field(default_factory=list, max_length=MAX_REDIRECTS)
    title: Annotated[str, Field(max_length=MAX_TITLE)] | None = None
    text_excerpt: Annotated[str, Field(max_length=MAX_TEXT)] = ""
    forms: list[FormInfo] = Field(default_factory=list, max_length=MAX_FORMS)
    buttons: list[ButtonInfo] = Field(default_factory=list, max_length=MAX_BUTTONS)  # not in forms
    links: list[LinkInfo] = Field(default_factory=list, max_length=MAX_LINKS)
    challenge: Label | None = None  # the signature that matched
    error: Annotated[str, Field(max_length=MAX_ERROR)] | None = None
    files: list[ArtifactKind] = Field(default_factory=list, max_length=len(FILES))
    proxy_bytes: int = Field(default=0, ge=0)


class CaptureMeta(_Strict):
    """``meta.json``, written last. Identity fields are informational only."""

    job_id: uuid.UUID
    # True when the browser gives up (its last attempt failed): worker-default then records the
    # failure. False for a successful capture.
    final_attempt: bool = False
    proxy_country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    browser_version: Label = ""
    profiles: list[ProfileResult] = Field(default_factory=list, max_length=2)
    error: Annotated[str, Field(max_length=MAX_ERROR)] | None = None
