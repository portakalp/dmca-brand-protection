"""dnstwist permutations of official domains (architecture.md §8)."""

from __future__ import annotations

from collections import Counter

from kalkan.discovery.permutations import FUZZERS, dictionary_for, generate, tlds_for


def test_generation_is_deterministic_and_sorted() -> None:
    first = generate("mintera.com", ["PL", "TR"])
    second = generate("mintera.com", ["TR", "PL"])
    assert first == second
    assert [p.domain for p in first] == sorted(p.domain for p in first)
    assert len({p.domain for p in first}) == len(first)
    assert "mintera.com" not in {p.domain for p in first}


def test_every_configured_fuzzer_contributes() -> None:
    fuzzers = Counter(p.fuzzer for p in generate("mintera.com", ["PL"]))
    assert set(fuzzers) == set(FUZZERS)


def test_expected_permutations_are_present() -> None:
    domains = {p.domain: p.fuzzer for p in generate("mintera.com", ["TR"])}
    assert domains["minteraa.com"] == "addition"  # first fuzzer in FUZZERS order wins
    assert domains["minttera.com"] == "repetition"
    assert domains["mintra.com"] == "omission"
    assert domains["mint-era.com"] == "hyphenation"
    assert domains["mintera-verify.com"] == "dictionary"
    assert domains["mintera-hesap.com"] == "dictionary"  # a TR market word
    assert domains["mintera.com.tr"] == "tld-swap"  # a TR market TLD
    assert domains["mintera.support"] == "tld-swap"
    assert domains["xn--mintra-eva.com"] == "homoglyph"  # mintéra
    assert domains["rnintera.com"] == "homoglyph"
    assert all(domain.isascii() for domain in domains)


def test_market_words_and_tlds() -> None:
    assert "bankowosc" in dictionary_for(["pl"])
    assert "bankowosc" not in dictionary_for(["TR"])
    assert dictionary_for([]) == dictionary_for(["XX"])
    assert tlds_for(["PL"])[-2:] == ["pl", "com.pl"]
    assert len(tlds_for(["PL", "PL"])) == len(set(tlds_for(["PL", "PL"])))


def test_multi_label_suffixes() -> None:
    domains = {p.domain for p in generate("mintera.com.pl", ["PL"])}
    assert "minteraa.com.pl" in domains
    assert "mintera.pl" in domains


def test_the_public_suffix_list_splits_the_official_domain() -> None:
    # dnstwist's own list does not know waw.pl and would permute "waw" instead of "mintera".
    domains = {p.domain for p in generate("mintera.waw.pl", ["PL"])}
    assert "mintra.waw.pl" in domains
    assert not any(
        domain.startswith("mintera.") and domain.endswith(".waw.pl") for domain in domains
    )


def test_a_subdomain_of_an_official_host_is_dropped() -> None:
    domains = {p.domain for p in generate("login.mintera.com")}
    assert "mintra.com" in domains
    assert "mintera.com" not in domains
    assert not any(domain.startswith("login.") for domain in domains)
