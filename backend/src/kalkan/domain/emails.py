"""E-mail address checks for addresses that may receive notices (architecture.md §9, §15)."""

from __future__ import annotations

import re

MAX_LENGTH = 254
_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def clean_email(value: str | None) -> str | None:
    """``value`` lower-cased when it is a plausible single address, otherwise None.

    RDAP data is untrusted: a value with control characters, white space, several ``@`` or
    an excessive length is dropped rather than stored as a notice recipient.
    """
    if value is None:
        return None
    candidate = value.strip()
    if candidate.lower().startswith("mailto:"):
        candidate = candidate[7:]
    if len(candidate) > MAX_LENGTH or any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        return None
    if not _ADDRESS.fullmatch(candidate):
        return None
    return candidate.lower()
