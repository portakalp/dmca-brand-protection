"""Public Suffix List: the copy in this repository, read without network access (§8).

``kalkan psl update`` refreshes the copy from publicsuffix.org; commit the result.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import httpx2
import tldextract
from tldextract.suffix_list import extract_tlds_from_suffix_list
from tldextract.tldextract import _PublicSuffixListTLDExtractor

PSL_FILE = Path(__file__).parent / "data" / "public_suffix_list.dat"
PSL_URL = "https://publicsuffix.org/list/public_suffix_list.dat"
# A sanity floor: the list has well over 9,000 rules.
MIN_RULES = 5_000
# Special-use TLDs (RFC 2606, RFC 6761) behave as suffixes too, so local fixture sites such as
# mintera-login.test split like real domains (K69).
SPECIAL_USE_SUFFIXES = ("test", "example", "invalid", "localhost")


class _RepositoryTLDExtract(tldextract.TLDExtract):
    """Reads the suffix list from the repository copy, never from the network or a cache."""

    def _get_tld_extractor(self, session: Any = None) -> _PublicSuffixListTLDExtractor:
        if self._extractor is None:
            public, private = extract_tlds_from_suffix_list(PSL_FILE.read_text(encoding="utf-8"))
            self._extractor = _PublicSuffixListTLDExtractor(
                public_tlds=public,
                private_tlds=private,
                extra_tlds=list(SPECIAL_USE_SUFFIXES),
                # Shared platforms (github.io, pages.dev, ...) count as suffixes, so a site
                # on one of them is its own registered domain.
                include_psl_private_domains=True,
            )
        return self._extractor


@cache
def _extractor() -> tldextract.TLDExtract:
    # The constructor insists on one data source; the snapshot is never read because
    # _get_tld_extractor is overridden.
    return _RepositoryTLDExtract(
        cache_dir=None,
        suffix_list_urls=(),
        fallback_to_snapshot=True,
        include_psl_private_domains=True,
    )


def split_host(host: str) -> tuple[str, str, str]:
    """``(subdomain, registered label, public suffix)``; the label is empty for a suffix."""
    result = _extractor()(host)
    return result.subdomain, result.domain, result.suffix


def count_rules(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() and not line.startswith("//"))


def update_public_suffix_list(path: Path = PSL_FILE, url: str = PSL_URL) -> int:
    """Download the list to ``path`` and return its rule count."""
    response = httpx2.get(url, timeout=30, follow_redirects=True)
    response.raise_for_status()
    text = response.text
    rules = count_rules(text)
    if rules < MIN_RULES or "===BEGIN ICANN DOMAINS===" not in text:
        raise ValueError(f"the downloaded list does not look like the PSL ({rules} rules)")
    path.write_text(text, encoding="utf-8")
    _extractor.cache_clear()
    return rules
