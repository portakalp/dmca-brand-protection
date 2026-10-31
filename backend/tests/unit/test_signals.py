"""Signal functions without services: visual, form and text signals, the snapshot
schema and the ai_triage hand-off."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import dramatiq
import pytest
from PIL import Image
from pydantic import ValidationError

from kalkan.ai import queue
from kalkan.config import REPO_ROOT
from kalkan.domain.enums import Profile
from kalkan.scanning.capture_contract import ButtonInfo, FormInfo, InputInfo
from kalkan.signals import text as textsig
from kalkan.signals.forms import form_signals, parse_buttons, parse_forms
from kalkan.signals.snapshot import CaseSnapshot
from kalkan.signals.visual import (
    MAX_PIXELS,
    Hashes,
    Reference,
    distance,
    image_hashes,
    nearest,
    similarity,
    visual_similarity,
)

SCREENSHOTS = REPO_ROOT / "fixtures" / "screenshots"
SITES = REPO_ROOT / "fixtures" / "sites"
SNAPSHOT = Path(__file__).parents[1] / "fixtures" / "snapshot_v1.json"


def _shot(name: str, profile: Profile) -> Hashes:
    found = image_hashes(
        (SCREENSHOTS / f"{name}-{profile.value}.png").read_bytes(), screenshot=True
    )
    assert found is not None
    return found


def _references() -> dict[Profile, list[Reference]]:
    return {
        profile: [Reference(f"ref-{profile.value}", *_shot("official", profile))]
        for profile in Profile
    }


# --- visual -----------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", list(Profile))
def test_the_clone_looks_like_the_official_site(profile: Profile) -> None:
    result = visual_similarity({profile: _shot("clone", profile)}, _references())
    assert result.similarity is not None
    assert result.similarity >= 0.85
    found = result.distances[profile]
    assert found is not None
    assert found.asset_id == f"ref-{profile.value}"


@pytest.mark.parametrize("name", ["unrelated", "parked"])
@pytest.mark.parametrize("profile", list(Profile))
def test_unrelated_sites_do_not(name: str, profile: Profile) -> None:
    result = visual_similarity({profile: _shot(name, profile)}, _references())
    assert result.similarity is not None
    assert result.similarity < 0.60


def test_the_best_profile_counts_and_missing_references_give_none() -> None:
    shots = {Profile.DESKTOP: _shot("unrelated", Profile.DESKTOP)}
    shots[Profile.MOBILE] = _shot("clone", Profile.MOBILE)
    result = visual_similarity(shots, _references())
    assert result.similarity is not None
    assert result.similarity >= 0.85
    empty = visual_similarity(shots, {})
    assert empty.similarity is None
    assert empty.distances == {Profile.DESKTOP: None, Profile.MOBILE: None}


def test_the_fixture_favicons_are_the_same_image() -> None:
    official = image_hashes((SITES / "official" / "favicon.ico").read_bytes())
    clone = image_hashes((SITES / "clone" / "favicon.ico").read_bytes())
    assert official is not None
    assert clone is not None
    assert distance(official.phash, clone.phash) == 0
    found = nearest(clone.phash, clone.dhash, [Reference("icon", official.phash, official.dhash)])
    assert found is not None
    assert (found.distance, found.dhash_distance) == (0, 0)


def _png(width: int, height: int, mode: str = "RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, (width, height)).save(buffer, "PNG")
    return buffer.getvalue()


def test_unreadable_or_oversized_images_have_no_hash() -> None:
    assert image_hashes(b"<html>not an icon</html>") is None
    assert image_hashes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64) is None
    # A decompression bomb: tiny file, huge canvas.
    side = int(MAX_PIXELS**0.5) + 10
    assert image_hashes(_png(side, side, "1")) is None
    assert image_hashes(_png(32, 32, "RGBA")) is not None  # transparency is flattened
    assert image_hashes(_png(1280, 3000), screenshot=True) is not None


def test_hash_distances() -> None:
    assert distance("0" * 16, "f" * 16) == 64
    assert distance("0" * 16, "nothex") is None
    assert similarity(0) == 1.0
    assert similarity(9) == 0.8594
    references = [Reference("b", "0" * 16), Reference("a", "0" * 16), Reference("c", "bad")]
    found = nearest("0" * 15 + "1", None, references)
    assert found is not None
    assert (found.asset_id, found.distance, found.dhash_distance) == ("a", 1, None)


# --- forms ------------------------------------------------------------------------------------


def test_forms_are_classified_again_and_the_browsers_hints_ignored() -> None:
    # A compromised browser could label anything; only the raw attributes count.
    forms = [
        FormInfo(
            action="https://mintera-login.test/login",
            inputs=[
                InputInfo(type="text", name="q", kind="password"),
                InputInfo(type="text", name="iban", kind=""),
            ],
            buttons=[ButtonInfo(text="Search", intent="deposit")],
        )
    ]
    found = form_signals(forms, [], "https://mintera-login.test/")
    assert found.values() == {
        "form_password": False,
        "form_card": False,
        "form_iban": True,
        "form_phone": False,
        "deposit_cta": False,
        "external_form_action": False,
    }


@pytest.mark.parametrize(
    ("action", "page", "external"),
    [
        ("https://mintera-login.test/login", "https://mintera-login.test/", False),
        ("https://pay.mintera-login.test/charge", "https://www.mintera-login.test/", False),
        ("https://collector.test/grab.php", "https://mintera-login.test/", True),
        (None, "https://mintera-login.test/", False),  # submitted from JavaScript
        ("https://collector.test/grab.php", None, False),  # no page URL to compare with
    ],
)
def test_external_form_actions(action: str | None, page: str | None, external: bool) -> None:
    found = form_signals([FormInfo(action=action)], [], page)
    assert found.external_form_action is external
    assert found.external_hosts == (["collector.test"] if external else [])


def test_hidden_fields_count_and_deposit_buttons_outside_forms() -> None:
    forms = [FormInfo(inputs=[InputInfo(type="password", name="pw", visible=False)])]
    buttons = [ButtonInfo(text="Wpłać teraz"), ButtonInfo(text="Wpłać teraz")]
    found = form_signals(forms, buttons, "https://mintera-login.test/")
    assert found.form_password is True
    assert found.kinds == {"password": 1}
    assert found.visible_kinds == {}
    assert found.deposit_cta is True
    assert found.deposit_texts == ["Wpłać teraz"]


def test_card_signal_needs_a_number_or_a_security_code() -> None:
    expiry = [FormInfo(inputs=[InputInfo(type="text", placeholder="MM/YY")])]
    assert form_signals(expiry, [], None).form_card is False
    cvv = [FormInfo(inputs=[InputInfo(type="text", name="cvc")])]
    assert form_signals(cvv, [], None).form_card is True


def test_stored_inventories_are_parsed_leniently() -> None:
    raw: list[Any] = [{"action": None, "inputs": [{"type": "password"}]}, {"inputs": "bad"}, 3]
    assert [form.inputs[0].type for form in parse_forms(raw)] == ["password"]
    assert [b.text for b in parse_buttons([{"text": "Deposit"}, {"text": 5}])] == ["Deposit"]


# --- text -------------------------------------------------------------------------------------


def test_text_similarity_uses_the_same_profile_first() -> None:
    references = {
        Profile.DESKTOP: [
            ("d1", "Mintera Bank login deposit transfer savings cards"),
            ("d0", "old"),
        ],
        Profile.MOBILE: [("m1", "completely different words here")],
    }
    best, per_profile = textsig.text_similarity(
        {Profile.DESKTOP: "Mintera Bank login deposit transfer savings cards", Profile.MOBILE: "x"},
        references,
    )
    assert best == 100.0
    assert per_profile[Profile.DESKTOP] == textsig.TextMatch(100.0, "d1")
    assert per_profile[Profile.MOBILE] is None  # no words to compare
    fallback, found = textsig.text_similarity(
        {Profile.MOBILE: "Mintera Bank login"}, {Profile.DESKTOP: [("d1", "Mintera Bank login")]}
    )
    assert fallback == 100.0
    assert found[Profile.MOBILE] == textsig.TextMatch(100.0, "d1")
    assert textsig.text_similarity({Profile.DESKTOP: "words"}, {}) == (
        None,
        {Profile.DESKTOP: None},
    )


def test_brand_on_page_ignores_the_sites_own_name() -> None:
    assert textsig.brand_on_page(
        "mintera-login.test is parked",
        "Buy mintera-login.test",
        ["mintera"],
        ["mintera-login.test"],
    ) == (False, False)
    assert textsig.brand_on_page("Mintera Bank | Login", "", ["mintera"], ["x.test"]) == (
        True,
        False,
    )


def test_redirects_to_official() -> None:
    assert textsig.redirects_to_official("https://www.mintera.test/pl", ["mintera.test"])
    assert not textsig.redirects_to_official("https://mintera-login.test/", ["mintera.test"])
    assert not textsig.redirects_to_official(None, ["mintera.test"])


def test_cloaking() -> None:
    clone = textsig.PageView("Mintera Bank login password deposit refund", True)
    blog = textsig.PageView("Weekend recipes tomato soup lemon tart salads", False)
    assert textsig.cloaking({Profile.DESKTOP: blog, Profile.MOBILE: clone})[0] is True
    assert textsig.cloaking({Profile.DESKTOP: clone, Profile.MOBILE: clone})[0] is False
    # Different pages that both show the brand are not cloaking.
    other = textsig.PageView("Mintera Bank promo codes and reviews", True)
    assert textsig.cloaking({Profile.DESKTOP: other, Profile.MOBILE: clone})[0] is False
    assert textsig.cloaking({Profile.DESKTOP: clone}) == (None, {})


def test_domain_age_and_webrisk() -> None:
    detected = datetime(2026, 11, 10, tzinfo=UTC)
    assert textsig.domain_age_days(detected - timedelta(days=3, hours=5), detected) == 3
    assert textsig.domain_age_days(detected + timedelta(days=1), detected) == 0
    assert textsig.domain_age_days(None, detected) is None
    assert textsig.webrisk_listed({"threat_types": ["MALWARE"]}) is True
    assert textsig.webrisk_listed({"threat_types": []}) is False
    assert textsig.webrisk_listed(None) is None


# --- snapshot ---------------------------------------------------------------------------------


def _example() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return data


def test_the_example_snapshot_matches_the_schema() -> None:
    data = _example()
    snapshot = CaseSnapshot.model_validate(data)
    assert snapshot.version == 1
    assert json.loads(snapshot.model_dump_json()) == data
    assert snapshot.flag("form_password") is True
    assert snapshot.flag("redirects_to_official") is False
    assert snapshot.flag("no_such_signal") is None
    assert snapshot.value("domain_age_days") == 5
    assert snapshot.value("no_such_signal") is None


def test_the_snapshot_schema_is_strict() -> None:
    data = _example()
    with pytest.raises(ValidationError):
        CaseSnapshot.model_validate(data | {"version": 2})
    with pytest.raises(ValidationError):
        CaseSnapshot.model_validate(data | {"unexpected": True})


# --- the ai_triage hand-off -------------------------------------------------------------------


class _Broker:
    def __init__(self) -> None:
        self.messages: list[dramatiq.Message[Any]] = []

    def enqueue(self, message: dramatiq.Message[Any]) -> None:
        self.messages.append(message)


def test_ai_triage_is_sent_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _Broker()
    monkeypatch.setattr(queue.dramatiq, "get_broker", lambda: broker)
    queue.send_ai_triage("case-1")
    [message] = broker.messages
    assert (message.queue_name, message.actor_name, message.kwargs) == (
        "ai",
        "ai_triage",
        {"case_id": "case-1"},
    )
