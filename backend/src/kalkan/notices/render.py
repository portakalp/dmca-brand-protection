"""Rendering a notice (architecture.md §15; K137).

A notice has four parts: the fixed legal paragraph (``legal/{template}.{lang}.txt``), the
explanation paragraph (the AI's, or the fixed one of ``templates/_explanation.{lang}.j2``), the
fact block (``templates/_facts.{lang}.j2``: URL, domain, IPs, first seen, case number,
evidence link, SHA-256 of the screenshots) and the signature. ``templates/{template}.{lang}.j2``
puts them together. A notice in Turkish, Polish or Russian carries its English version below.

Notices are plain text. Nothing the candidate site or the registration records wrote reaches the
fact block except the cleaned URL; RDAP names never do. Phishing notices to a registrar or the
registry of a generic TLD cite the ICANN DNS-abuse obligations of 2024 (the registry one not for
.com and .net, whose agreements differ). A legal file whose first line is
``LEGAL_REVIEW_REQUIRED`` makes the notice a dry-run-only notice (gate ``legal_review``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path
from urllib.parse import quote, urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from kalkan.domain.enums import Channel, Language, NoticeTemplate

HERE = Path(__file__).parent
LEGAL_DIR = HERE / "legal"
FLAG = "LEGAL_REVIEW_REQUIRED"
MAX_URL = 300
NO_REGISTRY_CITATION = frozenset({"com", "net"})
CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_env = Environment(
    loader=FileSystemLoader(HERE / "templates"),
    autoescape=False,  # noqa: S701 - plain-text e-mail, not HTML
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)

# Subjects and short words of the notices, per language (``templates/strings.json``).
_STRINGS = json.loads((HERE / "templates" / "strings.json").read_text(encoding="utf-8"))
SUBJECTS: dict[str, dict[str, str]] = _STRINGS["subjects"]
SCREENSHOT: dict[str, str] = _STRINGS["screenshot"]
PROFILES: dict[str, dict[str, str]] = _STRINGS["profiles"]
COPIED: dict[str, dict[str, str]] = _STRINGS["copied"]
ENGLISH_BELOW = "\n---------- English version ----------\n\n"


@dataclass(frozen=True)
class NoticeFacts:
    """What a notice says about the case; every field comes from our own records."""

    case_number: str
    brand: str
    official_domain: str | None
    domain: str  # ASCII (punycode)
    domain_unicode: str | None
    tld: str | None
    url: str  # cleaned with :func:`clean_url`
    final_host: str
    ips: tuple[str, ...]
    first_seen: datetime
    category: str
    copied_elements: tuple[str, ...] = ()
    collects: bool = False
    visual_similarity: float | None = None
    domain_age_days: int | None = None
    screenshots: tuple[tuple[str, str], ...] = ()  # (profile, sha256)


@dataclass(frozen=True)
class Rendered:
    subject: str
    body: str
    legal_review_required: bool
    files: tuple[str, ...] = field(default=())  # legal files used


def clean_url(url: str | None, domain: str) -> str:
    """The reported URL: the capture's final URL on the case's own host, without control
    characters or spaces and at most :data:`MAX_URL` characters; otherwise the domain."""
    fallback = f"http://{domain}/"
    if not url:
        return fallback
    text = CONTROL.sub("", url).strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return fallback
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme not in {"http", "https"} or not (host == domain or host.endswith("." + domain)):
        return fallback
    text = quote(text, safe=":/?#[]@!$&'()*+,;=%-._~")
    return text if len(text) <= MAX_URL else text[: MAX_URL - 1] + "…"


@cache
def legal_text(name: str, language: Language) -> tuple[str, bool]:
    """``(text, flagged)``; the flag line itself is not part of the text."""
    raw = (LEGAL_DIR / f"{name}.{language.value}.txt").read_text(encoding="utf-8")
    first, _, rest = raw.partition("\n")
    if first.strip() == FLAG:
        return rest.strip(), True
    return raw.strip(), False


def is_generic_tld(tld: str | None) -> bool:
    value = (tld or "").lower()
    return len(value) > 2 and not value.startswith("xn--")


def icann_citation(template: NoticeTemplate, channel: Channel, tld: str | None) -> str | None:
    if template is not NoticeTemplate.PHISHING or not is_generic_tld(tld):
        return None
    if channel is Channel.REGISTRAR:
        return "icann_registrar"
    if channel is Channel.REGISTRY and (tld or "").lower() not in NO_REGISTRY_CITATION:
        return "icann_registry"
    return None


def fallback_explanation(facts: NoticeFacts, language: Language) -> str:
    words = COPIED[language.value]
    similarity = (
        round(facts.visual_similarity * 100) if facts.visual_similarity is not None else None
    )
    return (
        _env.get_template(f"_explanation.{language.value}.j2")
        .render(
            category=facts.category,
            domain=facts.domain,
            brand=facts.brand,
            official=facts.official_domain or "",
            copied=", ".join(words.get(item, item) for item in facts.copied_elements),
            collects=facts.collects,
            similarity=similarity if similarity and similarity >= 50 else None,
            age=facts.domain_age_days,
        )
        .strip()
    )


def _one(
    template: NoticeTemplate,
    channel: Channel,
    language: Language,
    facts: NoticeFacts,
    *,
    explanation: str,
    evidence_url: str,
    reply_to: str,
    sent_on: datetime | None,
) -> tuple[str, list[tuple[str, bool]]]:
    legal, flagged = legal_text(template.value, language)
    used = [(f"{template.value}.{language.value}", flagged)]
    citation = icann_citation(template, channel, facts.tld)
    icann = ""
    if citation is not None:
        icann, cited_flag = legal_text(citation, language)
        used.append((f"{citation}.{language.value}", cited_flag))
    profiles = PROFILES[language.value]
    hashes = [
        (SCREENSHOT[language.value].format(profile=profiles.get(profile, profile)), digest)
        for profile, digest in facts.screenshots
    ]
    context = {
        "brand": facts.brand,
        "domain": facts.domain,
        "domain_unicode": facts.domain_unicode if facts.domain_unicode != facts.domain else None,
        "tld": facts.tld or "",
        "url": facts.url,
        "ips": list(facts.ips),
        "first_seen": facts.first_seen.strftime("%Y-%m-%d %H:%M UTC"),
        "case_number": facts.case_number,
        "evidence_url": evidence_url,
        "hashes": hashes,
        "channel": channel.value,
        "reply_to": reply_to,
        "sent_on": sent_on.strftime("%Y-%m-%d") if sent_on else "",
    }
    context["explanation"] = explanation
    context["legal"] = _env.from_string(legal).render(brand=facts.brand)
    context["icann"] = icann
    body = _env.get_template(f"{template.value}.{language.value}.j2").render(**context)
    return re.sub(r"\n{3,}", "\n\n", body).strip() + "\n", used


def render_notice(
    template: NoticeTemplate,
    channel: Channel,
    language: Language,
    facts: NoticeFacts,
    *,
    explanation_en: str,
    explanation_local: str | None,
    evidence_url: str,
    reply_to: str,
    sent_on: datetime | None = None,
) -> Rendered:
    english, used = _one(
        template,
        channel,
        Language.EN,
        facts,
        explanation=explanation_en,
        evidence_url=evidence_url,
        reply_to=reply_to,
        sent_on=sent_on,
    )
    subject = f"[{facts.case_number}] {SUBJECTS[template.value]['en']}: {facts.domain}"
    body = english
    if language is not Language.EN:
        local, more = _one(
            template,
            channel,
            language,
            facts,
            explanation=explanation_local or explanation_en,
            evidence_url=evidence_url,
            reply_to=reply_to,
            sent_on=sent_on,
        )
        used += more
        body = local + ENGLISH_BELOW + english
        subject = (
            f"[{facts.case_number}] {SUBJECTS[template.value][language.value]}: {facts.domain} "
            f"({SUBJECTS[template.value]['en']})"
        )
    return Rendered(
        subject=subject,
        body=body,
        legal_review_required=any(flagged for _, flagged in used),
        files=tuple(name for name, _ in used),
    )


def screenshot_hashes(rows: Sequence[tuple[str | None, str]]) -> tuple[tuple[str, str], ...]:
    """``(profile, sha256)`` once per profile, desktop first."""
    seen: dict[str, str] = {}
    for profile, digest in rows:
        if profile and profile not in seen:
            seen[profile] = digest
    return tuple(sorted(seen.items(), key=lambda item: item[0] != "desktop"))


def legal_review_required(
    template: NoticeTemplate, channel: Channel, language: Language, tld: str | None
) -> bool:
    """Whether a legal file the notice uses still waits for the lawyer (gate 8)."""
    names = [template.value]
    citation = icann_citation(template, channel, tld)
    if citation is not None:
        names.append(citation)
    languages = {Language.EN, language}
    return any(legal_text(name, lang)[1] for name in names for lang in languages)
