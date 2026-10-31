"""The explanation paragraph of a notice (architecture.md §13 "Bildirim metni", §24, K138).

``ai_notice_text`` (worker-ai) gives ``AI_TEXT_MODEL`` the structured fields only: brand,
official domain, case domain, the host of the reported page, category, copied elements, the
fixed list of signals, registration date, first seen, IPs and the target language; the
registrar's name and the hosting organisation go in an ``<untrusted_registration_data>`` block.
The page title and text, URL paths and the verdict's evidence and explanation never go in.

The answer must pass :func:`validate`: at most 900 characters, plain text, no URL, no e-mail
address, no obfuscated host ("example[.]com", "(dot)") and no host name other than the case
domain, the reported page's host and the evidence link's host. Otherwise, and whenever the AI is
unavailable or out of budget, the fixed paragraph (``notices/render.py``) is used.

Before any AI call the customer gates 1-3 are checked (no text is written for a notice that
cannot go); then the row becomes ``queued`` and ``send_notice`` is sent.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, ConfigDict, Field

from kalkan.ai.client import AiClient, AiError
from kalkan.ai.prompting import Prompt, untrusted
from kalkan.config import Settings
from kalkan.db.models import Case, Customer, Notice
from kalkan.db.session import Database
from kalkan.domain.enums import (
    AiPurpose,
    CaseState,
    ExplanationSource,
    Language,
    NoticeStatus,
    NoticeTemplate,
)
from kalkan.logging import get_logger
from kalkan.notices.facts import TextInputs, notice_facts
from kalkan.notices.gates import customer_gate
from kalkan.notices.render import fallback_explanation

PROMPT_DIR = Path(__file__).parents[1] / "ai" / "prompts" / "notice_text"
PROMPT_FILES = ("system.md", "user.md.j2")
MAX_CHARS = 900
LANGUAGE_NAMES = {Language.EN: "English", Language.TR: "Turkish", Language.PL: "Polish"}
LANGUAGE_NAMES[Language.RU] = "Russian"
SENDABLE_STATES = frozenset({CaseState.APPROVED, CaseState.NOTIFIED})
# Plain text: no markup, no links, no addresses.
FORBIDDEN = re.compile(r"[<>`*#\[\]{}|\\@]|https?:|www\.|://|xn--", re.IGNORECASE)
OBFUSCATED = re.compile(r"\(\s*\.\s*\)|\[\s*dot\s*\]|\(\s*dot\s*\)|\s+dot\s+[a-z]", re.IGNORECASE)
HOST = re.compile(r"(?<![\w.-])(?:[^\W_](?:[\w-]*[^\W_])?\.)+[^\W\d_]{2,}(?![\w-])", re.UNICODE)

_env = Environment(
    loader=FileSystemLoader(PROMPT_DIR),
    autoescape=False,  # noqa: S701 - plain text for a model; untrusted() escapes
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)

log = get_logger(__name__)


def _version() -> str:
    digest = hashlib.sha256()
    for name in PROMPT_FILES:
        digest.update(name.encode() + b"\n" + (PROMPT_DIR / name).read_bytes())
    return digest.hexdigest()[:12]


PROMPT_VERSION = _version()
SYSTEM = (PROMPT_DIR / "system.md").read_text(encoding="utf-8").strip()


class NoticeText(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    english: str = Field(max_length=2000)
    local: str = Field(max_length=2000)


def allowed_hosts(domain: str, final_host: str, evidence_base_url: str | None) -> set[str]:
    hosts = {domain.lower(), final_host.lower()}
    if evidence_base_url:
        hosts.add((urlsplit(evidence_base_url).hostname or "").lower())
    return {host for host in hosts if host}


def validate(text: str, hosts: set[str]) -> str | None:
    """The paragraph when it may go into a notice, else None."""
    value = " ".join(text.split())
    if not value or len(value) > MAX_CHARS:
        return None
    if FORBIDDEN.search(value) or OBFUSCATED.search(value):
        return None
    for found in HOST.findall(value):
        if found.lower().rstrip(".") not in hosts:
            return None
    return value


def build_prompt(inputs: TextInputs, language: Language) -> Prompt:
    facts = inputs.facts
    signals = sorted(
        (name, ("yes" if value else "no") if isinstance(value, bool) else str(value))
        for name, value in inputs.signals.items()
    )
    text = _env.get_template("user.md.j2").render(
        language=LANGUAGE_NAMES[language],
        brand=facts.brand,
        official_domain=facts.official_domain,
        domain=facts.domain,
        final_host=facts.final_host,
        category=facts.category.replace("_", " "),
        copied=", ".join(item.replace("_", " ") for item in facts.copied_elements),
        signals=signals,
        registered_on=inputs.registered_on,
        first_seen=facts.first_seen.date().isoformat(),
        ips=", ".join(facts.ips),
        registrar=untrusted(inputs.registrar, 200) if inputs.registrar else None,
        hosting_org=untrusted(inputs.hosting_org, 200) if inputs.hosting_org else None,
    )
    return Prompt(system=SYSTEM, content=[{"type": "text", "text": text}], version=PROMPT_VERSION)


@dataclass(frozen=True)
class Explanation:
    english: str
    local: str
    source: ExplanationSource


def explain(
    client: AiClient | None,
    settings: Settings,
    inputs: TextInputs,
    language: Language,
    *,
    case_id: uuid.UUID,
) -> Explanation:
    """The AI's paragraph when it validates, else the fixed one (never an exception)."""
    fixed = Explanation(
        fallback_explanation(inputs.facts, Language.EN),
        fallback_explanation(inputs.facts, language),
        ExplanationSource.FALLBACK,
    )
    if client is None:
        return fixed
    try:
        answer = client.call(
            build_prompt(inputs, language),
            output=NoticeText,
            model=settings.ai_text_model,
            purpose=AiPurpose.NOTICE_TEXT,
            case_id=case_id,
            effort="low",
        )
    except AiError as exc:
        log.warning("notices.text_fallback", case_id=str(case_id), reason=exc.reason)
        return fixed
    hosts = allowed_hosts(inputs.facts.domain, inputs.facts.final_host, settings.evidence_base_url)
    english = validate(answer.output.english, hosts)
    local = validate(answer.output.local, hosts) if language is not Language.EN else english
    if english is None or local is None:
        log.warning("notices.text_rejected", case_id=str(case_id))
        return fixed
    return Explanation(english, local, ExplanationSource.AI)


@dataclass(frozen=True)
class TextResult:
    notice_id: uuid.UUID
    customer_id: uuid.UUID
    queued: bool


def write_explanation(
    database: Database,
    client: AiClient | None,
    settings: Settings,
    notice_id: uuid.UUID,
    *,
    now: datetime,
) -> TextResult | None:
    """Gates 1-3, the paragraph, then ``queued``; None when the notice needs nothing."""
    with database.worker_session() as session:
        notice = session.get(Notice, notice_id)
        if notice is None or notice.status is not NoticeStatus.DRAFT:
            return None
        case = session.get_one(Case, notice.case_id)
        customer = session.get_one(Customer, notice.customer_id)
        if notice.template is NoticeTemplate.RETRACTION:
            inputs = None
        elif case.state not in SENDABLE_STATES:
            notice.status = NoticeStatus.REJECTED
            notice.failure_reason = f"case is {case.state.value}"
            return None
        else:
            blocked = customer_gate(customer)
            if blocked is not None:
                notice.status = NoticeStatus.BLOCKED
                notice.blocked_reason = blocked
                if case.state is CaseState.APPROVED:
                    case.blocked_reason = blocked
                return TextResult(notice.id, notice.customer_id, queued=False)
            inputs = notice_facts(session, case)
        language = notice.language
        case_id = case.id
    # The AI call runs outside the transaction: it may take a while.
    explanation = explain(client, settings, inputs, language, case_id=case_id) if inputs else None
    with database.worker_session() as session:
        notice = session.get(Notice, notice_id, with_for_update=True)
        if notice is None or notice.status is not NoticeStatus.DRAFT:
            return None
        if explanation is not None:
            notice.explanation_en = explanation.english
            notice.explanation_local = explanation.local
            notice.explanation_source = explanation.source
        notice.status = NoticeStatus.QUEUED
        notice.updated_at = now
        return TextResult(notice.id, notice.customer_id, queued=True)
