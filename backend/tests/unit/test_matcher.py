"""Brand matcher and host normalization (architecture.md §8)."""

from __future__ import annotations

import uuid

import pytest

from kalkan.discovery.matcher import BrandProfile, Matcher, MatchResult, combine
from kalkan.discovery.normalize import clean_host, fold, plain, skeleton, view_host
from kalkan.domain.enums import MatchReason
from kalkan.domain.hostnames import InvalidHostnameError

KEYWORD = MatchReason.KEYWORD
HOMOGLYPH = MatchReason.HOMOGLYPH
PERMUTATION = MatchReason.PERMUTATION
FUZZY = MatchReason.FUZZY

MINTERA = BrandProfile(
    brand_id=uuid.UUID(int=1),
    customer_id=uuid.UUID(int=101),
    name="Mintera Bank",
    keywords=("mintera",),
    permutations=frozenset({"mintwra.com", "mint.era.com"}),
)
PAYLUMO = BrandProfile(
    brand_id=uuid.UUID(int=2),
    customer_id=uuid.UUID(int=102),
    name="PayLumo",
    keywords=("paylumo",),
    variants=("Pay-Lumo",),
)
KREDO = BrandProfile(
    brand_id=uuid.UUID(int=3),
    customer_id=uuid.UUID(int=103),
    name="1Kredo",
    keywords=("1kredo",),
)


@pytest.fixture(scope="module")
def matcher() -> Matcher:
    return Matcher([MINTERA, PAYLUMO, KREDO])


def _match(matcher: Matcher, host: str) -> dict[uuid.UUID, MatchResult]:
    found = matcher.match_host(host)
    assert found is not None, host
    return {result.brand_id: result for result in found[1]}


# The prompt's acceptance examples come first; the rest cover the rules and their edges.
@pytest.mark.parametrize(
    ("host", "score", "reasons"),
    [
        ("mintera-login.com", 90, (KEYWORD,)),
        ("rnintera.com", 100, (HOMOGLYPH,)),
        ("minteraa.net", 90, (KEYWORD,)),
        ("mint-era-app.com", 90, (KEYWORD,)),
        ("m1ntera.top", 100, (HOMOGLYPH,)),
        ("login.mintera.com.evil.xyz", 80, (KEYWORD,)),
        ("xn--minter-8nf.com", 100, (HOMOGLYPH,)),  # Cyrillic a
        ("mintera.xyz", 100, (KEYWORD,)),
        ("Mintera.PL.", 100, (KEYWORD,)),
        ("www.mintera-verify.top", 90, (KEYWORD,)),
        ("*.mintera-rewards.support", 90, (KEYWORD,)),
        ("mintera123.com", 90, (KEYWORD,)),
        ("secure-mintera-account.online", 90, (KEYWORD,)),
        ("mint-era.example.com", 80, (KEYWORD,)),  # only in a subdomain
        ("mint3ra.site", 100, (HOMOGLYPH,)),
        ("mint3ra-secure.live", 90, (HOMOGLYPH,)),
        ("m1nt3r4.info", 100, (HOMOGLYPH,)),
        ("mintera.com.pl", 100, (KEYWORD,)),
        ("mintera.pages.dev", 100, (KEYWORD,)),
        # The keyword runs into the public suffix (80); the label alone is also close (83).
        ("minter.app", 88, (KEYWORD, FUZZY)),
        ("xn--mintra-eva.com", 100, (HOMOGLYPH,)),  # mintéra
        ("mintwra.com", 95, (PERMUTATION, FUZZY)),
        ("mint.era.com", 95, (KEYWORD, PERMUTATION)),
        # The plain keyword is only a subdomain; the registered label is a Cyrillic look-alike.
        ("mintera.xn--minter-8nf.com", 100, (KEYWORD, HOMOGLYPH)),
        ("mintra.com", 83, (FUZZY,)),  # omission
        ("minetra.com", 78, (FUZZY,)),  # transposition
        ("mintea.com", 83, (FUZZY,)),
        ("nintera.com", 83, (FUZZY,)),
    ],
)
def test_mintera_matches(matcher: Matcher, host: str, score: int, reasons: tuple) -> None:
    result = _match(matcher, host).get(MINTERA.brand_id)
    assert result is not None, host
    assert (result.score, result.reasons) == (score, reasons)
    assert result.customer_id == MINTERA.customer_id


@pytest.mark.parametrize(
    "host",
    [
        "mint.com",
        "eramint.org",
        "mintpay.com",
        "terabank.com",
        "era.com",
        "example.com",
        "mint-bank.com",
    ],
)
def test_no_mintera_match(matcher: Matcher, host: str) -> None:
    assert MINTERA.brand_id not in _match(matcher, host)


@pytest.mark.parametrize(
    ("host", "brand", "reasons"),
    [
        ("paylumo-wallet.com", PAYLUMO, (KEYWORD,)),
        ("pay1umo.online", PAYLUMO, (HOMOGLYPH,)),  # 1 read as l
        ("paylum0.com", PAYLUMO, (HOMOGLYPH,)),  # 0 read as o
        ("1kredo-verify.com", KREDO, (KEYWORD,)),
        ("lkredo.top", KREDO, (HOMOGLYPH,)),  # 1 read as l
        ("ikredo.top", KREDO, (HOMOGLYPH,)),  # 1 read as i
    ],
)
def test_other_brands(matcher: Matcher, host: str, brand: BrandProfile, reasons: tuple) -> None:
    results = _match(matcher, host)
    assert results[brand.brand_id].reasons == reasons
    assert MINTERA.brand_id not in results


def test_one_host_can_match_several_brands(matcher: Matcher) -> None:
    results = _match(matcher, "mintera-paylumo.com")
    assert set(results) == {MINTERA.brand_id, PAYLUMO.brand_id}


def test_short_keywords_are_ignored() -> None:
    brand = BrandProfile(uuid.UUID(int=9), uuid.UUID(int=9), "Pay", ("pay",))
    assert Matcher([brand]).match_host("pay-login.com") == (view_host("pay-login.com"), [])


def test_empty_matcher_matches_nothing() -> None:
    assert Matcher([]).match_host("mintera.com") == (view_host("mintera.com"), [])


@pytest.mark.parametrize(
    "host", ["", "203.0.113.7", "co.uk", "foo.ck", "-bad.com", "a" * 64 + ".com"]
)
def test_unusable_hosts_are_skipped(matcher: Matcher, host: str) -> None:
    assert matcher.match_host(host) is None


def test_scores_combine_with_a_bonus_per_extra_reason() -> None:
    assert combine({KEYWORD: 90}) == 90
    assert combine({KEYWORD: 90, PERMUTATION: 90}) == 95
    assert combine({KEYWORD: 100, PERMUTATION: 90, FUZZY: 90}) == 100


def test_fuzzy_is_skipped_when_the_label_holds_the_keyword(matcher: Matcher) -> None:
    assert _match(matcher, "minteras.com")[MINTERA.brand_id].reasons == (KEYWORD,)


# --- normalization ----------------------------------------------------------------------------


def test_fold_maps_leetspeak_confusables_and_marks() -> None:
    assert fold("Mintera") == fold("mint3r4") == fold("rnintera") == ("rnintera",)
    assert fold("m1nt3r4") == ("rnlntera", "rnintera")  # 1 read as l or as i
    assert fold("minter\N{CYRILLIC SMALL LETTER A}") == ("rnintera",)  # Cyrillic a
    assert fold(
        "\N{LATIN SMALL LETTER S WITH CEDILLA}ube-bankas\N{LATIN SMALL LETTER DOTLESS I}"
    ) == ("subebankasi",)  # Turkish s with cedilla, dotless i
    assert fold("łatwy") == ("latwy",)  # Polish l with stroke
    assert fold("1kredo") == ("lkredo", "ikredo")
    assert plain("mint-era.co_uk") == "minteracouk"


def test_skeleton_is_the_uts39_skeleton() -> None:
    assert skeleton("m") == "rn"
    assert skeleton("\N{CYRILLIC SMALL LETTER ES}") == "c"  # Cyrillic es


@pytest.mark.parametrize(
    ("raw", "host"),
    [
        ("*.WWW.Mintera.com.", "mintera.com"),
        ("www.mintera.com", "mintera.com"),
        ("minter\N{CYRILLIC SMALL LETTER A}.com", "xn--minter-8nf.com"),
        ("xn--minter-8nf.com", "xn--minter-8nf.com"),
    ],
)
def test_clean_host(raw: str, host: str) -> None:
    assert clean_host(raw) == host


def test_clean_host_rejects_invalid_punycode() -> None:
    with pytest.raises(InvalidHostnameError):
        clean_host("xn--zz.com")


def test_view_host_splits_with_the_public_suffix_list() -> None:
    view = view_host("login.xn--minter-8nf.com.pl")
    assert view.registered_domain == "xn--minter-8nf.com.pl"
    assert view.suffix == "com.pl"
    assert view.unicode_host == "login.minter\N{CYRILLIC SMALL LETTER A}.com.pl"
    assert view.subdomain.plain == "login"
    assert view.label.folded == ("rnintera",)
    # Private-section suffixes count: a site on a shared platform is its own domain.
    assert view_host("mintera.github.io").registered_domain == "mintera.github.io"
