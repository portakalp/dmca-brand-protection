"""Hints for the form inventory from ``form_patterns.yaml`` (architecture.md §10, §12).

:func:`input_kind` names what an input asks for (password, card number, IBAN, ...) and
:func:`button_intent` what a button or link offers (deposit, login, register). The browser
stage stores both hints in ``meta.json``; the signals stage reads the same file.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

PATTERNS_FILE = Path(__file__).with_name("form_patterns.yaml")
_SEPARATORS = re.compile(r"[\s_\-./:\[\]()]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# Letters without a decomposition: dotless i, Polish l, d with stroke, o with stroke.
_LETTERS = str.maketrans({"\u0131": "i", "\u0142": "l", "\u0111": "d", "\u00f8": "o"})


def normalize(text: str) -> str:
    """Lower case without accents and separators: ``cardNumber``, ``card-number`` and
    ``Card Number`` all become ``card number``; ``DOĞRULA`` becomes ``dogrula``."""
    spaced = _CAMEL.sub(" ", text)
    decomposed = unicodedata.normalize("NFKD", spaced.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _SEPARATORS.sub(" ", plain.translate(_LETTERS)).strip()


def _words(words: Iterable[str]) -> re.Pattern[str] | None:
    alternatives = sorted({normalize(str(word)) for word in words} - {""}, key=len, reverse=True)
    if not alternatives:
        return None
    # A match starts at a word start and may end inside a word (inflections, name suffixes).
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, alternatives)) + ")")


@dataclass(frozen=True)
class FieldRule:
    kind: str
    types: frozenset[str]
    autocomplete: frozenset[str]
    words: re.Pattern[str] | None
    placeholders: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class FormPatterns:
    fields: tuple[FieldRule, ...]
    buttons: tuple[tuple[str, re.Pattern[str]], ...]


@cache
def form_patterns() -> FormPatterns:
    data = yaml.safe_load(PATTERNS_FILE.read_text(encoding="utf-8"))
    fields = tuple(
        FieldRule(
            kind=str(kind),
            types=frozenset(str(value).lower() for value in rule.get("types", ())),
            autocomplete=frozenset(str(value).lower() for value in rule.get("autocomplete", ())),
            words=_words(rule.get("words", ())),
            placeholders=tuple(re.compile(str(value)) for value in rule.get("placeholders", ())),
        )
        for kind, rule in data["fields"].items()
    )
    buttons: list[tuple[str, re.Pattern[str]]] = []
    for intent, words in data["buttons"].items():
        pattern = _words(words)
        if pattern is not None:
            buttons.append((str(intent), pattern))
    return FormPatterns(fields=fields, buttons=tuple(buttons))


def input_kind(
    *,
    input_type: str = "",
    autocomplete: str = "",
    texts: Iterable[str] = (),
    placeholder: str = "",
) -> str:
    """The kind of an input, or ``""``. ``texts`` are its name, id, label and similar."""
    input_type = input_type.strip().lower()
    tokens = set(autocomplete.lower().split())
    joined = " | ".join(normalize(text) for text in (*texts, placeholder) if text)
    shown = normalize(placeholder)
    for rule in form_patterns().fields:
        if input_type in rule.types or tokens & rule.autocomplete:
            return rule.kind
        if rule.words is not None and rule.words.search(joined):
            return rule.kind
        if shown and any(pattern.search(shown) for pattern in rule.placeholders):
            return rule.kind
    return ""


def button_intent(*texts: str) -> str:
    """The intent of a button or link from its visible text and labels, or ``""``."""
    joined = " | ".join(normalize(text) for text in texts if text)
    if not joined:
        return ""
    for intent, pattern in form_patterns().buttons:
        if pattern.search(joined):
            return intent
    return ""
