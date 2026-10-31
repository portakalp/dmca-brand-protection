"""The channel plan (architecture.md §15; K56). Pure: facts in, targets out.

Phishing basis (a clone, or a lookalike landing page that leads to one):

- level 0, the first wave: the registrar; the hosting company when the site is not behind a
  proxy or the operator gave its origin; Google Safe Browsing as a manual report;
- level 1 (48 hours after the first wave, still online): the registry;
- level 2 (96 hours): an operator task.

Trademark basis (a lookalike page that sends the brand's customers to a third-party service):
level 0 the hosting company when it is known; level 1 (48 hours) an operator task (URS or
UDRP is the customer's decision).

Cloudflare's abuse channel is never planned (K56). Writing the first wave to ``notices`` is
``notices.planning``; the timing of levels 1 and 2 is the follow-up engine's (§16).

Recipients and languages: an e-mail address from the domain row (an operator override was
written there by enrichment, K68), a web form and a language from ``channel_contacts``. The
language follows the recipient's country (PL → pl, TR → tr, RU, BY, KZ → ru, else en); the
registrar's is English, the registry's follows a country-code TLD.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from kalkan.domain.enums import (
    Channel,
    ContactKind,
    Language,
    NoticeBasis,
    NoticeMethod,
    NoticeTemplate,
)

GOOGLE_REPORT_URL = "https://safebrowsing.google.com/safebrowsing/report_phish/"
COUNTRY_LANGUAGE = {"PL": Language.PL, "TR": Language.TR, "RU": Language.RU, "BY": Language.RU}
COUNTRY_LANGUAGE["KZ"] = Language.RU
TLD_LANGUAGE = {
    "pl": Language.PL,
    "tr": Language.TR,
    "ru": Language.RU,
    "su": Language.RU,
    "by": Language.RU,
    "kz": Language.RU,
    "xn--p1ai": Language.RU,  # .рф
}
TEMPLATES = {
    NoticeBasis.PHISHING: NoticeTemplate.PHISHING,
    NoticeBasis.TRADEMARK: NoticeTemplate.TRADEMARK,
}


@dataclass(frozen=True)
class Contact:
    email: str | None = None
    form_url: str | None = None
    language: Language | None = None


@dataclass(frozen=True)
class ChannelFacts:
    basis: NoticeBasis
    tld: str | None
    registrar_iana_id: int | None = None
    registrar_abuse_email: str | None = None
    behind_proxy: bool = False
    asn: int | None = None
    hosting_abuse_email: str | None = None
    hosting_country: str | None = None
    origin_known: bool = False  # the operator gave the origin behind the proxy
    origin_abuse_email: str | None = None
    origin_country: str | None = None
    contacts: Mapping[tuple[ContactKind, str], Contact] = field(default_factory=dict)


@dataclass(frozen=True)
class Target:
    channel: Channel
    method: NoticeMethod
    recipient: str | None  # e-mail address or form URL; None: the operator finds one
    language: Language
    template: NoticeTemplate
    level: int


@dataclass(frozen=True)
class Level:
    level: int
    after_hours: int  # since the last notice of the first wave
    targets: tuple[Target, ...] = ()
    operator_task: bool = False


def country_language(country: str | None) -> Language:
    return COUNTRY_LANGUAGE.get((country or "").upper(), Language.EN)


def tld_language(tld: str | None) -> Language:
    return TLD_LANGUAGE.get((tld or "").lower(), Language.EN)


def _target(
    channel: Channel,
    template: NoticeTemplate,
    level: int,
    *,
    email: str | None,
    contact: Contact | None,
    language: Language,
) -> Target:
    contact = contact or Contact()
    chosen = contact.language or language
    if email:
        return Target(channel, NoticeMethod.EMAIL, email, chosen, template, level)
    # No address: a web form when one is known, otherwise the operator finds the way.
    return Target(channel, NoticeMethod.MANUAL_FORM, contact.form_url, chosen, template, level)


def registrar_target(facts: ChannelFacts, template: NoticeTemplate, level: int = 0) -> Target:
    key = str(facts.registrar_iana_id) if facts.registrar_iana_id is not None else ""
    return _target(
        Channel.REGISTRAR,
        template,
        level,
        email=facts.registrar_abuse_email,
        contact=facts.contacts.get((ContactKind.REGISTRAR, key)),
        language=Language.EN,
    )


def hosting_target(facts: ChannelFacts, template: NoticeTemplate, level: int = 0) -> Target | None:
    """None behind a proxy whose origin nobody gave: the proxy's address shows the proxy."""
    if facts.behind_proxy and not facts.origin_known:
        return None
    if facts.origin_known:
        email, country = facts.origin_abuse_email, facts.origin_country
    else:
        email, country = facts.hosting_abuse_email, facts.hosting_country
    key = str(facts.asn) if facts.asn is not None else ""
    return _target(
        Channel.HOSTING,
        template,
        level,
        email=email,
        contact=None if facts.origin_known else facts.contacts.get((ContactKind.HOST, key)),
        language=country_language(country),
    )


def registry_target(facts: ChannelFacts, template: NoticeTemplate, level: int = 1) -> Target:
    """The registry's contact is the operator's (``channel_contacts``, key: the TLD, K136)."""
    contact = facts.contacts.get((ContactKind.REGISTRY, (facts.tld or "").lower()))
    return _target(
        Channel.REGISTRY,
        template,
        level,
        email=contact.email if contact else None,
        contact=contact,
        language=tld_language(facts.tld),
    )


def google_target(level: int = 0) -> Target:
    return Target(
        Channel.GOOGLE,
        NoticeMethod.MANUAL_FORM,
        GOOGLE_REPORT_URL,
        Language.EN,
        NoticeTemplate.PHISHING,
        level,
    )


@dataclass(frozen=True)
class Step:
    """A level after the first wave: its hours and its channel; no channel is an operator task."""

    level: int
    after_hours: int
    channel: Channel | None


ESCALATIONS: dict[NoticeTemplate, tuple[Step, ...]] = {
    NoticeTemplate.PHISHING: (Step(1, 48, Channel.REGISTRY), Step(2, 96, None)),
    NoticeTemplate.TRADEMARK: (Step(1, 48, None),),
}


def plan_levels(facts: ChannelFacts) -> list[Level]:
    """Every level of the plan; empty for a basis that sends nothing (none, copyright)."""
    template = TEMPLATES.get(facts.basis)
    if template is None:
        return []
    hosting = hosting_target(facts, template)
    if template is NoticeTemplate.PHISHING:
        first = [registrar_target(facts, template)]
        if hosting is not None:
            first.append(hosting)
        first.append(google_target())
        registry, task = ESCALATIONS[template]
        return [
            Level(0, 0, tuple(first)),
            Level(registry.level, registry.after_hours, (registry_target(facts, template),)),
            Level(task.level, task.after_hours, operator_task=True),
        ]
    (task,) = ESCALATIONS[NoticeTemplate.TRADEMARK]
    return [
        Level(0, 0, (hosting,) if hosting is not None else ()),
        Level(task.level, task.after_hours, operator_task=True),
    ]


def first_wave(facts: ChannelFacts) -> list[Target]:
    levels = plan_levels(facts)
    return list(levels[0].targets) if levels else []
