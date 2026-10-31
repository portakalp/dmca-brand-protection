"""Brand matcher (architecture.md §8): pure and compiled once for all active brands.

Rules, each giving a reason and a score (K58):

1. ``keyword``: a keyword appears in the host with separators removed. 100 when the
   registered label is the keyword, 90 when the keyword is inside the registered label,
   80 when it only appears in a subdomain or across the public suffix.
2. ``homoglyph``: the same, found after folding (leetspeak, confusables, marks) at a better
   place than any plain hit of the keyword.
3. ``permutation``: the host or its registered domain is one of the brand's dnstwist
   permutations. 90.
4. ``fuzzy``: the folded registered label (at least 5 characters) is at least 85 similar
   to the brand's folded name, a variant or a keyword (rapidfuzz ratio) and holds no
   keyword. The ratio minus 10.

The score is the best rule's score plus 5 for every further reason, at most 100.
Official domains and the whitelist are not checked here; ``admit_match`` does that (§9).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import ahocorasick
from rapidfuzz import fuzz, process

from kalkan.discovery.normalize import HostView, fold, plain, view_host
from kalkan.domain.enums import MatchReason
from kalkan.domain.hostnames import InvalidHostnameError

MIN_KEYWORD_LENGTH = 4
MIN_FUZZY_LABEL_LENGTH = 5
FUZZY_MIN_RATIO = 85

SCORE_EXACT = 100
SCORE_IN_LABEL = 90
SCORE_ELSEWHERE = 80
SCORE_PERMUTATION = 90
FUZZY_PENALTY = 10
EXTRA_REASON_BONUS = 5

_REASON_ORDER = list(MatchReason)


@dataclass(frozen=True)
class BrandProfile:
    brand_id: uuid.UUID
    customer_id: uuid.UUID
    name: str
    keywords: tuple[str, ...]
    variants: tuple[str, ...] = ()
    permutations: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MatchResult:
    brand_id: uuid.UUID
    customer_id: uuid.UUID
    score: int
    reasons: tuple[MatchReason, ...]


def combine(scores: dict[MatchReason, int]) -> int:
    best = max(scores.values())
    return min(100, best + EXTRA_REASON_BONUS * (len(scores) - 1))


def _placement(end_index: int, length: int, start: int, end: int) -> int:
    """Score of a keyword hit at ``[end_index - length + 1, end_index]``."""
    first = end_index - length + 1
    if first == start and end_index == end - 1:
        return SCORE_EXACT
    if first >= start and end_index < end:
        return SCORE_IN_LABEL
    return SCORE_ELSEWHERE


def _automaton(words: dict[str, set[int]]) -> ahocorasick.Automaton | None:
    if not words:
        return None
    automaton = ahocorasick.Automaton()
    for word, owners in words.items():
        automaton.add_word(word, (len(word), frozenset(owners)))
    automaton.make_automaton()
    return automaton


class Matcher:
    """All active brands compiled into two Aho-Corasick automata, a permutation index and a
    fuzzy choice list."""

    def __init__(self, brands: Iterable[BrandProfile]) -> None:
        self.brands = tuple(brands)
        plain_words: dict[str, set[int]] = defaultdict(set)
        folded_words: dict[str, set[int]] = defaultdict(set)
        fuzzy: dict[str, set[int]] = defaultdict(set)
        permutations: dict[str, set[int]] = defaultdict(set)
        for index, brand in enumerate(self.brands):
            for keyword in brand.keywords:
                if len(plain(keyword)) < MIN_KEYWORD_LENGTH:
                    continue
                plain_words[plain(keyword)].add(index)
                for folded in fold(keyword):
                    folded_words[folded].add(index)
            for name in (brand.name, *brand.variants, *brand.keywords):
                for folded in fold(name):
                    if len(folded) >= MIN_KEYWORD_LENGTH:
                        fuzzy[folded].add(index)
            for domain in brand.permutations:
                permutations[domain].add(index)
        self._plain = _automaton(plain_words)
        self._folded = _automaton(folded_words)
        self._fuzzy_choices = list(fuzzy)
        self._fuzzy_owners = [frozenset(fuzzy[choice]) for choice in self._fuzzy_choices]
        self._permutations = {domain: frozenset(owners) for domain, owners in permutations.items()}

    def __len__(self) -> int:
        return len(self.brands)

    def match(self, view: HostView) -> list[MatchResult]:
        hits: dict[int, dict[MatchReason, int]] = defaultdict(dict)
        in_label: set[int] = set()

        def add(owner: int, reason: MatchReason, score: int) -> None:
            hits[owner][reason] = max(score, hits[owner].get(reason, 0))
            if reason in (MatchReason.KEYWORD, MatchReason.HOMOGLYPH) and score > SCORE_ELSEWHERE:
                in_label.add(owner)

        if self._plain is not None:
            start = len(view.subdomain.plain)
            end = start + len(view.label.plain)
            for end_index, (length, owners) in self._plain.iter(view.plain):
                for owner in owners:
                    add(owner, MatchReason.KEYWORD, _placement(end_index, length, start, end))

        if self._folded is not None:
            for variant in range(len(view.label.folded)):
                subdomain = view.subdomain.folded[variant]
                label = view.label.folded[variant]
                text = subdomain + label + view.tail.folded[variant]
                start, end = len(subdomain), len(subdomain) + len(label)
                for end_index, (length, owners) in self._folded.iter(text):
                    score = _placement(end_index, length, start, end)
                    for owner in owners:
                        # A homoglyph counts where it places the brand better than the plain
                        # keyword does, e.g. a plain keyword in a subdomain and a Cyrillic
                        # look-alike as the registered label.
                        if score > hits.get(owner, {}).get(MatchReason.KEYWORD, 0):
                            add(owner, MatchReason.HOMOGLYPH, score)

        for domain in {view.host, view.registered_domain}:
            for owner in self._permutations.get(domain or "", ()):
                add(owner, MatchReason.PERMUTATION, SCORE_PERMUTATION)

        if self._fuzzy_choices:
            for label in set(view.label.folded):
                if len(label) < MIN_FUZZY_LABEL_LENGTH:
                    continue
                for _, ratio, index in process.extract(
                    label,
                    self._fuzzy_choices,
                    scorer=fuzz.ratio,
                    score_cutoff=FUZZY_MIN_RATIO,
                    limit=None,
                ):
                    for owner in self._fuzzy_owners[index]:
                        if owner not in in_label:
                            add(owner, MatchReason.FUZZY, round(ratio) - FUZZY_PENALTY)

        results = []
        for owner, scores in hits.items():
            brand = self.brands[owner]
            reasons = tuple(sorted(scores, key=_REASON_ORDER.index))
            results.append(MatchResult(brand.brand_id, brand.customer_id, combine(scores), reasons))
        return sorted(results, key=lambda result: (-result.score, str(result.brand_id)))

    def match_host(self, raw: str) -> tuple[HostView, list[MatchResult]] | None:
        """Match a raw host name; ``None`` when it is not a usable host name."""
        try:
            view = view_host(raw)
        except InvalidHostnameError:
            return None
        return view, self.match(view)
