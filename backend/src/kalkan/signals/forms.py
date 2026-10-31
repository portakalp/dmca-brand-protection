"""Form signals from the browser's form inventory (architecture.md §10, §12).

The inventory comes from the untrusted browser, so the hints it carries (``kind``,
``intent``) are not reused: every input and button is classified again here from its raw
attributes with the multilingual patterns of ``scanning/form_patterns.yaml``.

- ``form_password``, ``form_iban``, ``form_phone``: an input of that kind.
- ``form_card``: a card number or security code input (an expiry date alone is not enough).
- ``deposit_cta``: a button or button-like link that offers a deposit, in or outside a form.
- ``external_form_action``: a form submits to another registered domain than the page's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from kalkan.scanning.capture_contract import ButtonInfo, FormInfo, InputInfo
from kalkan.scanning.form_patterns import button_intent, input_kind
from kalkan.scanning.precheck import host_of, registered

CARD_KINDS = frozenset({"card_number", "card_cvv"})
MAX_DETAIL = 10


def classify_input(item: InputInfo) -> str:
    return input_kind(
        input_type=item.type,
        autocomplete=item.autocomplete,
        texts=(item.name, item.id, item.label),
        placeholder=item.placeholder,
    )


def classify_button(item: ButtonInfo) -> str:
    return button_intent(item.text)


def parse_forms(raw: Sequence[Any]) -> list[FormInfo]:
    """``scans.forms`` back into the hand-off model; malformed entries are skipped."""
    forms: list[FormInfo] = []
    for item in raw:
        try:
            forms.append(FormInfo.model_validate(item))
        except ValidationError:
            continue
    return forms


def parse_buttons(raw: Sequence[Any]) -> list[ButtonInfo]:
    buttons: list[ButtonInfo] = []
    for item in raw:
        try:
            buttons.append(ButtonInfo.model_validate(item))
        except ValidationError:
            continue
    return buttons


@dataclass
class FormSignals:
    form_password: bool = False
    form_card: bool = False
    form_iban: bool = False
    form_phone: bool = False
    deposit_cta: bool = False
    external_form_action: bool = False
    # Details: the kinds found, the deposit texts and the external action hosts.
    kinds: dict[str, int] = field(default_factory=dict)
    visible_kinds: dict[str, int] = field(default_factory=dict)
    deposit_texts: list[str] = field(default_factory=list)
    external_hosts: list[str] = field(default_factory=list)

    def values(self) -> dict[str, bool]:
        return {
            "form_password": self.form_password,
            "form_card": self.form_card,
            "form_iban": self.form_iban,
            "form_phone": self.form_phone,
            "deposit_cta": self.deposit_cta,
            "external_form_action": self.external_form_action,
        }

    def details(self) -> dict[str, dict[str, Any]]:
        inputs = {"kinds": self.kinds, "visible_kinds": self.visible_kinds}
        return {
            "form_password": inputs,
            "form_card": inputs,
            "form_iban": inputs,
            "form_phone": inputs,
            "deposit_cta": {"texts": self.deposit_texts},
            "external_form_action": {"hosts": self.external_hosts},
        }


def form_signals(
    forms: Sequence[FormInfo], buttons: Sequence[ButtonInfo], page_url: str | None
) -> FormSignals:
    """The form signals of one scan; ``page_url`` is the page's final URL."""
    result = FormSignals()
    for form in forms:
        for item in form.inputs:
            kind = classify_input(item)
            if not kind:
                continue
            result.kinds[kind] = result.kinds.get(kind, 0) + 1
            if item.visible:
                result.visible_kinds[kind] = result.visible_kinds.get(kind, 0) + 1
    found = set(result.kinds)
    result.form_password = "password" in found
    result.form_card = bool(found & CARD_KINDS)
    result.form_iban = "iban" in found
    result.form_phone = "phone" in found

    for button in [*(item for form in forms for item in form.buttons), *buttons]:
        if classify_button(button) == "deposit":
            result.deposit_cta = True
            text = button.text.strip()
            if text and text not in result.deposit_texts and len(result.deposit_texts) < MAX_DETAIL:
                result.deposit_texts.append(text)

    page_host = host_of(page_url)
    own = registered(page_host) if page_host else ""
    for form in forms:
        target = host_of(form.action)
        if not target or not own or registered(target) == own:
            continue
        result.external_form_action = True
        if target not in result.external_hosts and len(result.external_hosts) < MAX_DETAIL:
            result.external_hosts.append(target)
    return result
