"""``ai_classify_reply(message_id)``: Haiku sorts a reply (architecture.md §13, §16).

The model sees the case domain, the channel of our notice and the e-mail (sender, subject, at
most 6,000 characters of the body, Postmark's stripped reply when there is one) inside an
``<untrusted_email>`` block. Its answer is a :class:`ReplyVerdict`; the action is code
(``followup.replies.apply_reply``), and a low confidence or an unavailable AI leaves the reply to
the operator.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, ConfigDict, Field

from kalkan.ai.client import AiClient, AiError
from kalkan.ai.prompting import Prompt, untrusted
from kalkan.config import Settings
from kalkan.db.models import Case, Domain, Message, Notice
from kalkan.db.session import Database
from kalkan.domain.enums import AiPurpose, ReplyClass
from kalkan.followup.replies import ReplyAction, apply_reply
from kalkan.logging import get_logger

PROMPT_DIR = Path(__file__).parents[1] / "ai" / "prompts" / "classify_reply"
PROMPT_FILES = ("system.md", "user.md.j2")
MAX_BODY = 6000

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


class ReplyVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: Literal["removed", "needs_info", "not_our_customer", "auto_reply", "other"]
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(max_length=300)


def build_prompt(*, domain: str, channel: str | None, message: Message) -> Prompt:
    text = _env.get_template("user.md.j2").render(
        domain=domain,
        channel=channel,
        sender=untrusted(message.from_addr or "unknown", 300),
        subject=untrusted(message.subject or "", 500),
        body=untrusted(message.body_text or "", MAX_BODY),
    )
    return Prompt(system=SYSTEM, content=[{"type": "text", "text": text}], version=PROMPT_VERSION)


def _summary(value: str) -> str:
    # Shown on the ops panel and in a task: the first line, without markup characters.
    first = " ".join(value.split())[:200]
    return "".join(char for char in first if char not in "<>")


def classify_reply(
    database: Database,
    client: AiClient | None,
    settings: Settings,
    message_id: uuid.UUID,
    *,
    now: datetime,
) -> ReplyAction | None:
    """None when the message is unknown or already classified."""
    with database.worker_session() as session:
        message = session.get(Message, message_id)
        if message is None or message.classification is not None:
            return None
        case = session.get_one(Case, message.case_id)
        domain = session.get_one(Domain, case.domain_id).name
        notice = session.get(Notice, message.notice_id) if message.notice_id else None
        prompt = build_prompt(
            domain=domain,
            channel=notice.channel.value if notice is not None else None,
            message=message,
        )
        case_id = case.id
    category: ReplyClass | None = None
    confidence: float | None = None
    summary = ""
    if client is not None:
        try:
            answer = client.call(
                prompt,
                output=ReplyVerdict,
                model=settings.ai_triage_model,
                purpose=AiPurpose.CLASSIFY_REPLY,
                case_id=case_id,
                effort="low",
            )
        except AiError as exc:
            log.warning("replies.classify_failed", message_id=str(message_id), reason=exc.reason)
        else:
            category = ReplyClass(answer.output.category)
            confidence = answer.output.confidence
            summary = _summary(answer.output.summary)
    with database.worker_session() as session:
        message = session.get(Message, message_id, with_for_update=True)
        if message is None or message.classification is not None:
            return None
        if category is not None:
            message.classification = category
            message.classification_confidence = Decimal(str(round(confidence or 0, 3)))
        else:
            # No AI answer: "other" without a confidence, so a redelivery does not ask again.
            message.classification = ReplyClass.OTHER
        return apply_reply(session, message, category, confidence, summary=summary, now=now)
