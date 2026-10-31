"""The channel plan (architecture.md §15; K56)."""

from __future__ import annotations

from typing import Any

import pytest

from kalkan.domain.enums import (
    Channel,
    ContactKind,
    Language,
    NoticeBasis,
    NoticeMethod,
    NoticeTemplate,
)
from kalkan.notices.planner import (
    GOOGLE_REPORT_URL,
    ChannelFacts,
    Contact,
    country_language,
    first_wave,
    plan_levels,
    tld_language,
)

PHISHING = NoticeBasis.PHISHING
TRADEMARK = NoticeBasis.TRADEMARK


def facts(basis: NoticeBasis = PHISHING, **values: Any) -> ChannelFacts:
    base: dict[str, Any] = {
        "tld": "online",
        "registrar_iana_id": 1068,
        "registrar_abuse_email": "abuse@registrar.example",
        "asn": 64500,
        "hosting_abuse_email": "abuse@host.example",
        "hosting_country": "PL",
    }
    base.update(values)
    return ChannelFacts(basis=basis, **base)


def summary(targets: list[Any]) -> list[tuple[str, str, str | None, str]]:
    return [(t.channel.value, t.method.value, t.recipient, t.language.value) for t in targets]


def test_phishing_outside_a_proxy_goes_to_registrar_host_and_google() -> None:
    assert summary(first_wave(facts())) == [
        ("registrar", "email", "abuse@registrar.example", "en"),
        ("hosting", "email", "abuse@host.example", "pl"),
        ("google", "manual_form", GOOGLE_REPORT_URL, "en"),
    ]
    assert {t.template for t in first_wave(facts())} == {NoticeTemplate.PHISHING}


def test_phishing_behind_a_proxy_skips_the_host() -> None:
    assert [t.channel for t in first_wave(facts(behind_proxy=True))] == [
        Channel.REGISTRAR,
        Channel.GOOGLE,
    ]


def test_a_known_origin_behind_the_proxy_gets_the_hosting_notice() -> None:
    wave = first_wave(
        facts(
            behind_proxy=True,
            origin_known=True,
            origin_abuse_email="abuse@origin.example",
            origin_country="TR",
        )
    )
    hosting = [t for t in wave if t.channel is Channel.HOSTING]
    assert summary(hosting) == [("hosting", "email", "abuse@origin.example", "tr")]


def test_trademark_goes_to_the_host_only() -> None:
    assert summary(first_wave(facts(TRADEMARK))) == [
        ("hosting", "email", "abuse@host.example", "pl")
    ]
    assert first_wave(facts(TRADEMARK))[0].template is NoticeTemplate.TRADEMARK
    assert first_wave(facts(TRADEMARK, behind_proxy=True)) == []


@pytest.mark.parametrize("basis", [NoticeBasis.NONE, NoticeBasis.COPYRIGHT])
def test_a_basis_without_notices_plans_nothing(basis: NoticeBasis) -> None:
    assert plan_levels(facts(basis)) == []


def test_escalation_levels_of_the_phishing_basis() -> None:
    levels = plan_levels(
        facts(contacts={(ContactKind.REGISTRY, "online"): Contact(email="abuse@nic.online")})
    )
    assert [(level.level, level.after_hours, level.operator_task) for level in levels] == [
        (0, 0, False),
        (1, 48, False),
        (2, 96, True),
    ]
    assert summary(list(levels[1].targets)) == [("registry", "email", "abuse@nic.online", "en")]
    assert levels[1].targets[0].level == 1


def test_a_registry_without_a_contact_is_the_operators() -> None:
    registry = plan_levels(facts(tld="pl"))[1].targets[0]
    assert (registry.method, registry.recipient, registry.language) == (
        NoticeMethod.MANUAL_FORM,
        None,
        Language.PL,
    )


def test_escalation_of_the_trademark_basis_is_an_operator_task() -> None:
    levels = plan_levels(facts(TRADEMARK))
    assert [(level.level, level.after_hours, level.operator_task) for level in levels] == [
        (0, 0, False),
        (1, 48, True),
    ]


def test_contacts_give_web_forms_and_languages() -> None:
    contacts = {
        (ContactKind.REGISTRAR, "1068"): Contact(form_url="https://registrar.example/abuse"),
        (ContactKind.HOST, "64500"): Contact(language=Language.RU),
    }
    wave = first_wave(facts(registrar_abuse_email=None, contacts=contacts))
    assert summary(wave)[:2] == [
        ("registrar", "manual_form", "https://registrar.example/abuse", "en"),
        ("hosting", "email", "abuse@host.example", "ru"),
    ]


def test_cloudflare_is_never_planned() -> None:
    for basis in (PHISHING, TRADEMARK):
        for proxied in (True, False):
            for level in plan_levels(facts(basis, behind_proxy=proxied)):
                assert Channel.CLOUDFLARE not in {t.channel for t in level.targets}


def test_languages() -> None:
    assert [country_language(c) for c in ("PL", "tr", "BY", "KZ", "DE", None)] == [
        Language.PL,
        Language.TR,
        Language.RU,
        Language.RU,
        Language.EN,
        Language.EN,
    ]
    assert [tld_language(t) for t in ("pl", "TR", "xn--p1ai", "online")] == [
        Language.PL,
        Language.TR,
        Language.RU,
        Language.EN,
    ]
