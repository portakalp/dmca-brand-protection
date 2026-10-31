"""Suspension and warning pages from ``takedown_patterns.yaml`` (architecture.md §16)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

PATTERNS_FILE = Path(__file__).with_name("takedown_patterns.yaml")


@dataclass(frozen=True)
class Patterns:
    suspended_texts: tuple[str, ...]
    warning_texts: tuple[str, ...]


@cache
def patterns() -> Patterns:
    data = yaml.safe_load(PATTERNS_FILE.read_text(encoding="utf-8"))
    return Patterns(
        suspended_texts=tuple(text.lower() for text in data["suspended_texts"]),
        warning_texts=tuple(text.lower() for text in data["warning_texts"]),
    )


def _found(texts: tuple[str, ...], text: str, title: str) -> bool:
    lowered = f"{title}\n{text}".lower()
    return any(pattern in lowered for pattern in texts)


def suspended(text: str, title: str = "") -> bool:
    return _found(patterns().suspended_texts, text, title)


def warning(text: str, title: str = "") -> bool:
    return _found(patterns().warning_texts, text, title)
