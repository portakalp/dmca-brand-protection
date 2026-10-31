"""Discovery against the test database: recording, catalogue, dnstwist, CZDS, CLI (§8)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from dramatiq.brokers.redis import RedisBroker
from prometheus_client import REGISTRY
from pydantic import SecretStr
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from kalkan import admin, cli
from kalkan.config import Settings
from kalkan.db.models import (
    AuditLog,
    Brand,
    BrandMatch,
    BrandPermutation,
    Customer,
    Domain,
    DomainSighting,
    OfficialDomain,
)
from kalkan.db.session import Database
from kalkan.discovery import czds
from kalkan.discovery.catalog import MatcherCache, MatcherUnavailableError, load_profiles
from kalkan.discovery.dnscheck import check_permutations, resolve_all
from kalkan.discovery.matcher import MatchResult
from kalkan.discovery.normalize import view_host
from kalkan.discovery.permutations import refresh_brand_permutations
from kalkan.discovery.record import Sighting, record_discovery
from kalkan.domain.enums import CustomerStatus, MatchReason, SightingSource
from tests import factories

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
ZONE = Path(__file__).resolve().parents[3] / "fixtures" / "czds" / "xyz.zone.gz"
LINK_BASE = "https://czds-api.icann.org/czds/downloads"


class FakeLedger:
    """An in-memory stand-in for the Redis keys that claim a zone download per day."""

    def __init__(self) -> None:
        self.keys: dict[str, int] = {}

    def set(self, name: str, value: str, *, nx: bool, ex: int) -> bool:
        if nx and name in self.keys:
            return False
        self.keys[name] = ex
        return True


class Queued:
    """Collects the match ids a discovery source queues for admission."""

    def __init__(self) -> None:
        self.ids: list[uuid.UUID] = []

    def __call__(self, ids: Sequence[uuid.UUID]) -> None:
        self.ids += ids


def _brand(
    database: Database,
    slug: str = "mint",
    *,
    officials: Sequence[str] = (),
    **customer_values: object,
) -> Brand:
    with database.worker_session() as session:
        owner = factories.customer(session, slug, **customer_values)
        brand = factories.brand(session, owner)
        brand.target_markets = ["TR"]
        for domain in officials:
            admin.add_official_domain(session, brand, domain, actor="test")
        return brand


def _count(database: Database, model: type) -> int:
    with database.worker_session() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_record_discovery_is_idempotent(database: Database) -> None:
    brand = _brand(database)
    view = view_host("mintera-login.com")
    match = MatchResult(brand.id, brand.customer_id, 90, (MatchReason.KEYWORD,))
    sighting = Sighting(SightingSource.CT, NOW, {"issuer": "Let's Encrypt"})
    with database.worker_session() as session:
        first = record_discovery(session, view, [match], sighting)
        second = record_discovery(session, view, [match], sighting)
        third = record_discovery(session, view, [match], sighting, sight_known=False)
    assert first.new_domain
    assert len(first.new_match_ids) == 1
    assert not second.new_domain
    assert second.new_match_ids == []
    assert third.domain_id == first.domain_id
    assert _count(database, DomainSighting) == 2  # the third was a known name and match
    with database.worker_session() as session:
        domain = session.scalars(select(Domain)).one()
        row = session.scalars(select(BrandMatch)).one()
    assert (domain.name, domain.registered_domain, domain.tld) == (
        "mintera-login.com",
        "mintera-login.com",
        "com",
    )
    with database.worker_session() as session:
        record_discovery(session, view_host("login.mintera.com.pl"), [match], sighting)
        record_discovery(session, view_host("mintera.pages.dev"), [match], sighting)
        stored = dict(session.execute(select(Domain.name, Domain.tld)).all())
    assert stored["login.mintera.com.pl"] == "pl"  # the TLD, not the public suffix com.pl
    assert stored["mintera.pages.dev"] == "dev"
    assert (row.score, row.reasons, row.admitted) == (90, ["keyword"], False)


def test_catalogue_skips_demo_suspended_and_inactive_brands(database: Database) -> None:
    watched = _brand(database, "watched")
    _brand(database, "demo", is_demo=True)
    _brand(database, "suspended", status=CustomerStatus.SUSPENDED)
    inactive = _brand(database, "inactive")
    with database.worker_session() as session:
        session.execute(update(Brand).where(Brand.id == inactive.id).values(active=False))
        profiles = load_profiles(session)
    assert [profile.brand_id for profile in profiles] == [watched.id]


def test_matcher_cache_rebuilds_only_after_the_ttl_and_a_change(database: Database) -> None:
    clock = [0.0]
    cache = MatcherCache(database, ttl=60, clock=lambda: clock[0])
    assert len(cache.get()) == 0
    _brand(database)
    clock[0] = 30
    assert len(cache.get()) == 0  # still within the TTL
    clock[0] = 61
    first = cache.get()
    assert len(first) == 1
    clock[0] = 200
    assert cache.get() is first  # nothing changed, no rebuild


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE brands SET keywords = '{paylumo}'",
        "UPDATE brands SET active = false",
        "UPDATE customers SET status = 'suspended'",
        "INSERT INTO brand_permutations (id, customer_id, brand_id, domain, fuzzer) "
        "SELECT gen_random_uuid(), customer_id, id, 'mintwra.com', 'replacement' FROM brands",
    ],
)
def test_matcher_cache_notices_changes_made_outside_the_orm(
    database: Database, change: str
) -> None:
    _brand(database)
    clock = [0.0]
    cache = MatcherCache(database, ttl=60, clock=lambda: clock[0])
    first = cache.get()
    with database.worker_session() as session:
        session.execute(text(change))
    clock[0] = 61
    assert cache.get() is not first


class BrokenDatabase:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.broken = False

    def worker_session(self) -> Any:
        if self.broken:
            raise OperationalError("SELECT 1", {}, Exception("database is down"))
        return self.database.worker_session()


def test_matcher_cache_serves_the_last_matcher_while_the_database_is_down(
    database: Database,
) -> None:
    _brand(database)
    broken = BrokenDatabase(database)
    clock = [0.0]
    cache = MatcherCache(broken, ttl=60, clock=lambda: clock[0])  # type: ignore[arg-type]
    first = cache.get()
    broken.broken = True
    clock[0] = 61
    assert cache.get() is first

    empty = MatcherCache(broken, ttl=60, clock=lambda: clock[0])  # type: ignore[arg-type]
    with pytest.raises(MatcherUnavailableError):
        empty.get()
    broken.broken = False
    with pytest.raises(MatcherUnavailableError):  # no new attempt within the retry delay
        empty.get()
    clock[0] += 6
    assert len(empty.get()) == 1


def test_official_domain_add_generates_permutations(database: Database) -> None:
    brand = _brand(database, officials=["mintera.com"])
    with database.worker_session() as session:
        domains = set(
            session.scalars(
                select(BrandPermutation.domain).where(BrandPermutation.brand_id == brand.id)
            )
        )
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "official_domain.add")
        ).one()
    assert {"minteraa.com", "mintera-verify.com", "mintera.com.tr"} <= domains
    assert "mintera.com" not in domains
    assert audit.data["permutations"] == len(domains)

    demo = _brand(database, "demo", officials=["mintera-demo.com"], is_demo=True)
    with database.worker_session() as session:
        assert not session.scalar(select(func.count()).where(BrandPermutation.brand_id == demo.id))


def test_refresh_removes_permutations_of_a_removed_official_domain(database: Database) -> None:
    brand = _brand(database, officials=["mintera.com", "mintera.pl"])
    with database.worker_session() as session:
        before = session.scalar(select(func.count()).where(BrandPermutation.brand_id == brand.id))
        session.execute(delete(BrandPermutation).where(BrandPermutation.domain == "minteraa.com"))
        session.execute(delete(OfficialDomain).where(OfficialDomain.domain == "mintera.pl"))
        current = session.get(Brand, brand.id)
        assert current is not None
        result = refresh_brand_permutations(session, current)
    assert result.added == 1  # minteraa.com comes back
    assert result.removed > 0
    assert result.total < (before or 0)


class FakeResolver:
    def __init__(self, registered: set[str], unknown: frozenset[str] = frozenset({"mintera.info"})):
        self.registered_names = registered
        self.unknown = unknown

    async def registered(self, domain: str) -> bool | None:
        if domain in self.unknown:
            return None
        return domain in self.registered_names


async def _fast(domains: Sequence[str], resolver: FakeResolver) -> dict[str, bool | None]:
    return await resolve_all(domains, resolver, rate=1_000_000)


def test_dns_check_turns_registered_permutations_into_candidates(database: Database) -> None:
    brand = _brand(database, officials=["mintera.com"])
    _brand(database, "demo", officials=["demobank.com"], is_demo=True)
    queued = Queued()
    resolver = FakeResolver({"minteraa.com", "mintera-verify.com"})
    result = check_permutations(
        database, resolver, MatcherCache(database), queued, now=lambda: NOW, resolve=_fast
    )
    assert result.registered == 2
    assert result.unknown == 1
    assert len(queued.ids) == 2
    with database.worker_session() as session:
        rows = dict(
            session.execute(
                select(BrandPermutation.domain, BrandPermutation.registered).where(
                    BrandPermutation.brand_id == brand.id
                )
            ).all()
        )
        sightings = session.scalars(select(DomainSighting.source)).all()
    assert rows["minteraa.com"] is True
    assert rows["mintra.com"] is False
    assert rows["mintera.info"] is None  # unknown stays unchecked
    assert sightings == [SightingSource.DNSTWIST] * 2

    again = Queued()
    check_permutations(
        database, resolver, MatcherCache(database), again, now=lambda: NOW, resolve=_fast
    )
    assert again.ids == []  # already registered: no new candidates, no new sightings
    assert _count(database, DomainSighting) == 2


def test_dns_check_skips_suspended_and_demo_brands(database: Database) -> None:
    suspended = _brand(database, "paused", officials=["mintera.com"])
    demo = _brand(database, "demo", is_demo=True)
    with database.worker_session() as session:
        session.execute(
            update(Customer)
            .where(Customer.id == suspended.customer_id)
            .values(status=CustomerStatus.SUSPENDED)
        )
        session.add(
            BrandPermutation(
                customer_id=demo.customer_id, brand_id=demo.id, domain="minteraa.com", fuzzer="x"
            )
        )
    resolver = FakeResolver({"minteraa.com"})
    result = check_permutations(
        database, resolver, MatcherCache(database), Queued(), now=lambda: NOW, resolve=_fast
    )
    assert result.checked == 0
    with database.worker_session() as session:
        checked = session.scalar(
            select(func.count()).where(BrandPermutation.checked_at.is_not(None))
        )
    assert checked == 0


def test_dns_check_keeps_the_progress_of_finished_chunks(database: Database) -> None:
    brand = _brand(database, officials=["mintera.com"])
    resolver = FakeResolver(set(), unknown=frozenset())
    chunks: list[list[str]] = []

    async def fail_on_the_second_chunk(
        domains: Sequence[str], resolver: FakeResolver
    ) -> dict[str, bool | None]:
        chunks.append(list(domains))
        if len(chunks) == 2:
            raise TimeoutError("time limit")
        return await _fast(domains, resolver)

    def run(resolve: Any) -> None:
        check_permutations(
            database, resolver, MatcherCache(database), Queued(), now=lambda: NOW, resolve=resolve,
            chunk=100,
        )  # fmt: skip

    with pytest.raises(TimeoutError):
        run(fail_on_the_second_chunk)
    with database.worker_session() as session:
        checked = set(
            session.scalars(
                select(BrandPermutation.domain).where(
                    BrandPermutation.brand_id == brand.id, BrandPermutation.checked_at.is_not(None)
                )
            )
        )
    assert checked == set(chunks[0])
    assert len(checked) == 100

    # The next run starts with the rows that were not checked yet.
    chunks.clear()

    async def record(domains: Sequence[str], resolver: FakeResolver) -> dict[str, bool | None]:
        chunks.append(list(domains))
        return await _fast(domains, resolver)

    run(record)
    assert not checked & set(chunks[0])
    assert set().union(*chunks) >= checked


def test_zone_scan_records_new_matches_only(database: Database) -> None:
    _brand(database)
    data = ZONE.read_bytes()
    chunks = [data[start : start + 100] for start in range(0, len(data), 100)]
    queued = Queued()
    result = czds.scan_zone(
        "xyz", chunks, MatcherCache(database).get(), database, queued, now=lambda: NOW
    )
    assert (result.names, result.matched, len(result.new_match_ids)) == (120, 5, 5)
    assert queued.ids == result.new_match_ids
    with database.worker_session() as session:
        names = set(session.scalars(select(Domain.name)))
    assert names == {
        "mintera-login.xyz",
        "m1ntera.xyz",
        "login-rnintera.xyz",
        "xn--minter-8nf.xyz",
        "mintera-verify.xyz",
    }
    again = czds.scan_zone("xyz", chunks, MatcherCache(database).get(), database, Queued())
    assert again.new_match_ids == []
    assert _count(database, DomainSighting) == 5


def test_czds_scan_end_to_end(database: Database, test_settings: Settings) -> None:
    _brand(database)

    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == czds.AUTH_URL:
            return httpx2.Response(200, json={"accessToken": "t"})
        if url == czds.LINKS_URL:
            return httpx2.Response(
                200,
                json=[
                    f"https://czds-api.icann.org/czds/downloads/{tld}.zone"
                    for tld in ("xyz", "com", "top")
                ],
            )
        if url.endswith("/xyz.zone"):
            return httpx2.Response(200, content=ZONE.read_bytes())
        return httpx2.Response(503)

    settings = test_settings.model_copy(
        update={"czds_username": SecretStr("u"), "czds_password": SecretStr("p"), "czds_tlds": []}
    )
    ledger = FakeLedger()
    queued = Queued()
    names_before = REGISTRY.get_sample_value("czds_zone_names", {"tld": "xyz"})
    results = czds.run_czds_scan(
        settings,
        database,
        queued,
        ledger,
        transport=httpx2.MockTransport(handler),
        today=lambda: NOW.date(),
    )
    assert [(zone.tld, zone.error is None) for zone in results] == [("top", False), ("xyz", True)]
    assert len(queued.ids) == 5
    assert REGISTRY.get_sample_value("czds_zone_names", {"tld": "xyz"}) == 120
    assert names_before in (None, 120)
    assert set(ledger.keys) == {"czds:downloaded:top:2026-10-20", "czds:downloaded:xyz:2026-10-20"}

    # The same day again (e.g. a redelivered message): no zone is downloaded twice.
    again = czds.run_czds_scan(
        settings,
        database,
        Queued(),
        ledger,
        transport=httpx2.MockTransport(handler),
        today=lambda: NOW.date(),
    )
    assert again == []


def test_czds_zone_failure_does_not_stop_the_other_zones(
    database: Database, test_settings: Settings
) -> None:
    _brand(database)

    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == czds.AUTH_URL:
            return httpx2.Response(200, json={"accessToken": "t"})
        if url == czds.LINKS_URL:
            return httpx2.Response(200, json=[f"{LINK_BASE}/{tld}.zone" for tld in ("info", "xyz")])
        return httpx2.Response(200, content=ZONE.read_bytes())

    calls = 0

    def queue_admit(ids: Sequence[uuid.UUID]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("redis is down")

    settings = test_settings.model_copy(
        update={"czds_username": SecretStr("u"), "czds_password": SecretStr("p"), "czds_tlds": []}
    )
    results = czds.run_czds_scan(
        settings, database, queue_admit, FakeLedger(), transport=httpx2.MockTransport(handler)
    )
    assert [zone.tld for zone in results] == ["info", "xyz"]
    assert results[0].error is not None
    assert "ConnectionError" in results[0].error
    assert results[1].error is None


runner = CliRunner()


def test_cli_candidate_add_records_and_queues(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    test_settings: Settings,
    redis_broker: RedisBroker,
) -> None:
    monkeypatch.setattr(cli, "get_database", lambda: database)
    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)
    brand = _brand(database)

    url = "https://mint-promo.example.net/login?ref=1"
    result = runner.invoke(cli.app, ["candidate", "add", url, "--brand", str(brand.id)])
    assert result.exit_code == 0, result.output
    assert "Added: mint-promo.example.net" in result.output
    again = runner.invoke(cli.app, ["candidate", "add", url, "--brand", str(brand.id)])
    assert "Already known" in again.output

    with database.worker_session() as session:
        match = session.scalars(select(BrandMatch)).one()
        sightings = session.scalars(select(DomainSighting)).all()
        actions = session.scalars(select(AuditLog.action)).all()
    assert (match.score, match.reasons) == (100, ["manual"])
    assert [s.detail["url"] for s in sightings] == [url, url]
    assert actions.count("candidate.add") == 2
    assert redis_broker.client.llen(f"{redis_broker.namespace}:discovery") == 2


def test_candidate_add_upgrades_an_existing_match(database: Database) -> None:
    brand = _brand(database)
    view = view_host("mintera-login.com")
    with database.worker_session() as session:
        record_discovery(
            session,
            view,
            [MatchResult(brand.id, brand.customer_id, 90, (MatchReason.KEYWORD,))],
            Sighting(SightingSource.CT, NOW),
        )
        current = session.get(Brand, brand.id)
        assert current is not None
        result = admin.add_candidate(session, current, "mintera-login.com", actor="test")
        match = session.scalars(select(BrandMatch)).one()
    assert not result.created
    assert result.reasons == (MatchReason.KEYWORD, MatchReason.MANUAL)
    assert (match.score, match.reasons) == (100, ["keyword", "manual"])


def test_cli_candidate_add_rejects_bad_input(
    monkeypatch: pytest.MonkeyPatch, database: Database, test_settings: Settings
) -> None:
    monkeypatch.setattr(cli, "get_database", lambda: database)
    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)
    brand = _brand(database)
    demo = _brand(database, "demo", is_demo=True)
    for url, brand_id, message in [
        ("ftp://mintera.com", brand.id, "not an http(s) URL"),
        ("https://203.0.113.9/login", brand.id, "IP addresses"),
        ("mintera-login.com", demo.id, "demo workspace is frozen"),
        ("mintera-login.com", uuid.uuid4(), "no brand"),
        ("https://mintera-login.com\\@evil.example/", brand.id, "backslash"),
        ("https://user@mintera-login.com/", brand.id, "user information"),
        ("https://mintera login.com/", brand.id, "space"),
    ]:
        result = runner.invoke(cli.app, ["candidate", "add", url, "--brand", str(brand_id)])
        assert result.exit_code == 1, url
        assert message in result.output, result.output
    assert _count(database, BrandMatch) == 0
    with database.worker_session() as session:
        assert session.scalar(select(func.count()).select_from(Customer)) == 2


def test_discovery_actors_skip_the_demo_workspace(
    monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    from kalkan.workers import discovery as actors

    monkeypatch.setattr(actors, "get_database", lambda: database)
    monkeypatch.setattr(actors, "DnsResolver", lambda: FakeResolver({"minteraa.com"}))
    monkeypatch.setattr(actors, "check_permutations", _fast_check(actors.check_permutations))
    monkeypatch.setattr(actors, "queue_admit", Queued())
    brand = _brand(database, officials=["mintera.com"])
    demo = _brand(database, "demo", is_demo=True)
    with database.worker_session() as session:
        session.add(
            BrandPermutation(
                customer_id=demo.customer_id, brand_id=demo.id, domain="demobankk.com", fuzzer="x"
            )
        )
        session.execute(delete(BrandPermutation).where(BrandPermutation.brand_id == brand.id))

    actors.generate_permutations.fn()
    actors.check_permutation_dns.fn()
    with database.worker_session() as session:
        rows = session.execute(select(BrandPermutation.brand_id, BrandPermutation.checked_at)).all()
        matches = session.scalars(select(BrandMatch)).all()
    regenerated = [row for row in rows if row.brand_id == brand.id]
    demo_rows = [row for row in rows if row.brand_id == demo.id]
    assert len(regenerated) > 1000
    unchecked = [row for row in regenerated if row.checked_at is None]
    assert len(unchecked) == 1  # mintera.info: the fake resolver answers "unknown"
    assert [row.checked_at for row in demo_rows] == [None]
    assert [match.brand_id for match in matches] == [brand.id]


def _fast_check(check: Any) -> Any:
    def run(*args: Any, **kwargs: Any) -> Any:
        return check(*args, resolve=_fast, **kwargs)

    return run
