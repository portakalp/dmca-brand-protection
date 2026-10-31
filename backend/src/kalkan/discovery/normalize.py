"""Host normalization for brand matching (architecture.md §8).

Pure functions. Keywords and hosts go through the same folding, so a confusable or
leetspeak spelling of a keyword folds to the same string as the keyword itself:
lower case, leetspeak (``1`` is tried as both ``l`` and ``i``), the UTS #39 confusable
skeleton (``m`` and ``rn`` fold together, a Cyrillic a becomes a Latin one), combining marks
dropped (``ş`` becomes ``s``) and the separators ``-``, ``.`` and ``_`` removed.
"""

from __future__ import annotations

import contextlib
import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import idna

from kalkan.discovery.psl import split_host
from kalkan.domain.hostnames import InvalidHostnameError, normalize_hostname

DATA_DIR = Path(__file__).parent / "data"
# Unicode's confusables.txt (UTS #39), kept byte-identical to the upstream file.
CONFUSABLES_FILE = DATA_DIR / "confusables.txt"

_LEET = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t"})
_LEET_ONE_AS_I = str.maketrans({"1": "i"})
_SEPARATORS = str.maketrans("", "", "-._")
# Plain lower-case ASCII host names skip the IDNA round trip, which is the slow part.
_ASCII_HOST = re.compile(r"^(?=.{4,253}$)((?!-)[a-z0-9-]{1,63}(?<!-)\.)+[a-z]{2,63}$")


@cache
def _confusables() -> dict[int, str]:
    mapping: dict[int, str] = {}
    for raw in CONFUSABLES_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        source, target, *_ = (part.strip() for part in line.split(";"))
        mapping[int(source, 16)] = "".join(chr(int(code, 16)) for code in target.split())
    return mapping


def skeleton(text: str) -> str:
    """The UTS #39 skeleton: NFD, confusable mapping, NFD again."""
    decomposed = unicodedata.normalize("NFD", text)
    return unicodedata.normalize("NFD", decomposed.translate(_confusables()))


def _drop_marks(text: str) -> str:
    return "".join(char for char in text if not unicodedata.combining(char))


def plain(text: str) -> str:
    """Lower case without separators; no leetspeak and no confusable mapping."""
    return text.casefold().translate(_SEPARATORS)


def fold(text: str) -> tuple[str, ...]:
    """Every folded form of ``text``: one, or two when it contains the digit ``1``."""
    lowered = text.casefold()
    variants = [lowered]
    if "1" in lowered:
        variants.append(lowered.translate(_LEET_ONE_AS_I))
    folded = []
    for variant in variants:
        value = _drop_marks(skeleton(variant.translate(_LEET))).casefold().translate(_SEPARATORS)
        if value not in folded:
            folded.append(value)
    return tuple(folded)


def clean_host(raw: str) -> str:
    """Canonical punycode host without a ``*.`` or ``www.`` prefix.

    Raises :class:`InvalidHostnameError` for anything that is not a host name.
    """
    host = raw.strip().lower().rstrip(".")
    while host.startswith("*."):
        host = host[2:]
    if host.startswith("www."):
        host = host[4:]
    if _ASCII_HOST.match(host) and "xn--" not in host:
        return host
    return normalize_hostname(host)


def to_unicode(host: str) -> str:
    """U-labels for display and folding; labels that are not valid punycode stay as they are."""
    if "xn--" not in host:
        return host
    labels = []
    for label in host.split("."):
        decoded = label
        if label.startswith("xn--"):
            with contextlib.suppress(idna.IDNAError):
                decoded = idna.decode(label)
        labels.append(decoded)
    return ".".join(labels)


@dataclass(frozen=True)
class HostParts:
    """One part of a host (subdomain, registered label or suffix) in matching forms."""

    plain: str
    folded: tuple[str, ...]


@dataclass(frozen=True)
class HostView:
    """A host prepared for the matcher."""

    host: str  # canonical punycode
    unicode_host: str
    registered_domain: str  # eTLD+1 in punycode
    suffix: str  # public suffix in punycode; empty when the host has none
    subdomain: HostParts
    label: HostParts  # the registered label, left of the public suffix
    tail: HostParts  # the public suffix

    @property
    def plain(self) -> str:
        return self.subdomain.plain + self.label.plain + self.tail.plain


def _parts(text: str, variants: int) -> HostParts:
    folded = fold(text)
    # Every part carries the same number of variants so they line up across the host.
    if len(folded) < variants:
        folded = folded + (folded[0],) * (variants - len(folded))
    return HostParts(plain=plain(text), folded=folded)


def view_host(raw: str) -> HostView:
    """Clean ``raw`` and split it for matching; raises :class:`InvalidHostnameError`."""
    host = clean_host(raw)
    unicode_host = to_unicode(host)
    subdomain, label, suffix = split_host(unicode_host)
    if not label:
        raise InvalidHostnameError(f"{raw!r} is a public suffix")
    variants = 2 if "1" in unicode_host else 1
    # Punycode and Unicode labels line up one to one, so the split carries over.
    ascii_labels = host.split(".")
    suffix_length = len(suffix.split(".")) if suffix else 0
    return HostView(
        host=host,
        unicode_host=unicode_host,
        registered_domain=".".join(ascii_labels[-(suffix_length + 1) :]),
        suffix=".".join(ascii_labels[len(ascii_labels) - suffix_length :]),
        subdomain=_parts(subdomain, variants),
        label=_parts(label, variants),
        tail=_parts(suffix, variants),
    )
