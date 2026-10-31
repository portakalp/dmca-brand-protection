"""Admission, enrichment, outbox and reconciler against the test services."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import redis
from sqlalchemy import func, select
from typer.testing import CliRunner

from kalkan import admin, cli
from kalkan.config import Settings
from kalkan.db.models import (
    Artifact,
    Brand,
    BrandMatch,
    Case,
    CaseEvent,
    ChannelContact,
    Customer,
    Domain,
    JobOutbox,
    ReviewTask,
    Scan,
    Signal,
)
from kalkan.db.outbox import (
    ENRICH_DOMAIN,
    REFRESH_BRAND_FAVICONS,
    REFRESH_BRAND_REFERENCES,
    WHITELIST_CHANGED,
)
from kalkan.db.session import Database
from kalkan.discovery.matcher import MatchResult
from kalkan.discovery.normalize import view_host
from kalkan.discovery.record import Sighting, record_discovery, upsert_domain
from kalkan.domain.case_state import transition
from kalkan.domain.enums import (
    CaseState,
    ContactKind,
    EventType,
    MatchReason,
    ScanStage,
    SightingSource,
    TaskKind,
    TaskStatus,
)
from kalkan.enrichment import service
from kalkan.enrichment.admission import admit, apply_whitelist
from kalkan.enrichment.dns import LookupFailedError
from kalkan.enrichment.rdap import RATE_PREFIX, RdapUnavailableError, RedisTokenBucket
from kalkan.scheduler import outbox, reconciler
from kalkan.storage.gcs import ObjectStore
from tests import factories
from tests.support import redis_url_for_tests

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "rdap"
CLOUDFLARE = ["104.16.0.0/13"]
TEST_EVIDENCE_BUCKET = "kalkan-test-evidence"


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


class Sent:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, str]]] = []

    def __call__(self, actor: str, **kwargs: str) -> None:
        self.messages.append((actor, kwargs))


def _customer_brand(
    database: Database, slug: str = "mint", officials: tuple[str, ...] = (), **values: Any
) -> Brand:
    with database.worker_session() as session:
        owner = factories.customer(session, slug, **values)
        brand = factories.brand(session, owner)
        for host in officials:
            admin.add_official_domain(session, brand, host, actor="test")
        return brand


def _match(database: Database, brand: Brand, host: str, at: datetime = NOW) -> uuid.UUID:
    with database.worker_session() as session:
        recorded = record_discovery(
            session,
            view_host(host),
            [MatchResult(brand.id, brand.customer_id, 90, (MatchReason.KEYWORD,))],
            Sighting(SightingSource.CT, at, {"log": "Test"}),
        )
    return recorded.new_match_ids[0]


def _admit(database: Database, match_id: uuid.UUID) -> Any:
    with database.worker_session() as session:
        return admit(session, match_id, now=NOW, prefix="KLK")


# --- admission ------------------------------------------------------------------------------------


def test_admit_opens_one_case_per_match(database: Database) -> None:
    brand = _customer_brand(database)
    match_id = _match(database, brand, "mintera-login.com")
    first = _admit(database, match_id)
    second = _admit(database, match_id)
    assert first.created
    assert first.follow_up
    assert second.follow_up  # a redelivered message queues the follow-up again
    assert not second.created
    assert second.case_id == first.case_id
    with database.worker_session() as session:
        case = session.scalars(select(Case)).one()
        event = session.scalars(select(CaseEvent)).one()
        match = session.get(BrandMatch, match_id)
    assert case.state is CaseState.DETECTED
    assert case.number.startswith("KLK-")
    assert case.first_seen_at == NOW
    assert event.type is EventType.DETECTED
    assert event.data == {
        "source": "ct",
        "reasons": ["keyword"],
        "score": 90,
        "domain": "mintera-login.com",
    }
    assert match is not None
    assert match.admitted
    assert match.checked_at == NOW


@pytest.mark.parametrize(
    ("host", "whitelisted"),
    [
        ("mintera.com", True),  # official
        ("www.mintera.com", True),
        ("partners.mintera-referrals.com", True),
        ("x.partners.mintera-referrals.com", True),
        ("mintera-referrals.com", False),
        ("mintera.com.evil.xyz", False),
    ],
)
def test_admit_honours_the_whitelist(database: Database, host: str, whitelisted: bool) -> None:
    brand = _customer_brand(database, officials=("mintera.com",))
    with database.worker_session() as session:
        customer = session.get(Customer, brand.customer_id)
        assert customer is not None
        admin.import_whitelist(
            session, customer, "pattern,kind\npartners.mintera-referrals.com,affiliate\n", actor="t"
        )
    result = _admit(database, _match(database, brand, host))
    assert result.whitelisted is whitelisted
    assert result.created is not whitelisted
    with database.worker_session() as session:
        match = session.scalars(select(BrandMatch)).one()
    assert match.checked_at == NOW
    assert match.admitted is not whitelisted


def test_admit_leaves_the_demo_workspace_alone(database: Database) -> None:
    brand = _customer_brand(database, "demo", is_demo=True)
    result = _admit(database, _match(database, brand, "mintera-login.com"))
    assert not result.created
    with database.worker_session() as session:
        assert session.scalar(select(func.count()).select_from(Case)) == 0
        assert session.scalars(select(BrandMatch.checked_at)).one() == NOW


def test_official_domains_cover_every_brand_of_the_customer(database: Database) -> None:
    bank = _customer_brand(database, officials=("minteraonline.com",))
    with database.worker_session() as session:
        business = Brand(
            customer_id=bank.customer_id, name="Mintera Business", keywords=["mintera"]
        )
        session.add(business)
    # The keyword of one brand matches the customer's own site for the other brand.
    result = _admit(database, _match(database, business, "minteraonline.com"))
    assert result.whitelisted
    assert result.case_id is None


# --- enrichment -----------------------------------------------------------------------------------


class FakeResolver:
    def __init__(self, answers: dict[tuple[str, str], Any]) -> None:
        self.answers = answers

    def resolve(self, name: str, rdtype: str) -> list[str]:
        answer = self.answers.get((name, rdtype), [])
        if isinstance(answer, Exception):
            raise answer
        return list(answer)


class FakeRdap:
    def __init__(self, domains: dict[str, Any], ips: dict[str, Any]) -> None:
        self.domains = domains
        self.ips = ips

    def domain(self, name: str) -> dict[str, Any] | None:
        value = self.domains.get(name)
        if isinstance(value, Exception):
            raise value
        return value

    def ip(self, address: str) -> dict[str, Any] | None:
        value = self.ips.get(address)
        if isinstance(value, Exception):
            raise value
        return value

    def asn(self, number: int) -> dict[str, Any] | None:
        return None


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str, *, ex: int) -> None:
        self.values[name] = value


class FakeWebRisk:
    def lookup(self, host: str) -> dict[str, Any]:
        return {"threat_types": ["SOCIAL_ENGINEERING"], "expire_time": None}


def _sources(
    store: ObjectStore | None = None,
    resolver: FakeResolver | None = None,
    cache: MemoryCache | None = None,
    ranges: Any = None,
    **rdap: Any,
) -> service.Sources:
    answers = {
        ("mintera-login.com", "A"): ["88.198.0.1"],
        ("mintera-login.com", "MX"): ["10 mail.mintera-login.com."],
        ("mintera-login.com", "NS"): ["ns1.mintera.com.", "ns2.registrar-servers.com."],
        ("1.0.198.88.origin.asn.cymru.com", "TXT"): ['"24940 | 88.198.0.0/16 | DE"'],
        ("mintera.com", "A"): ["203.0.113.10"],
        ("mintera.com", "NS"): ["ns1.mintera.com.", "ns2.mintera.com."],
    }
    return service.Sources(
        resolver=resolver or FakeResolver(answers),
        rdap=FakeRdap(
            rdap.get("domains", {"mintera-login.com": _fixture("domain-namecheap.com.json")}),
            {"88.198.0.1": _fixture("ip-88.198.0.1.json")},
        ),
        cloudflare_ranges=ranges or (lambda: CLOUDFLARE),
        webrisk=FakeWebRisk(),
        store=store,
        evidence_bucket=TEST_EVIDENCE_BUCKET if store is not None else None,
        cache=cache,
    )


def _open_case(database: Database, host: str = "mintera-login.com", **kwargs: Any) -> Case:
    brand = _customer_brand(database, officials=("mintera.com",), **kwargs)
    result = _admit(database, _match(database, brand, host))
    with database.worker_session() as session:
        case = session.get(Case, result.case_id)
        assert case is not None
        return case


def _enrich(database: Database, case: Case, sources: service.Sources, final: bool = True) -> Any:
    return service.enrich_domain(
        database, case.domain_id, sources, now=lambda: NOW, final_attempt=final
    )


def test_enrichment_fills_the_domain_and_stores_evidence(
    database: Database, object_store: ObjectStore
) -> None:
    case = _open_case(database)
    result = _enrich(database, case, _sources(object_store))
    assert result.complete
    assert result.precheck_case_ids == [case.id]
    with database.worker_session() as session:
        domain = session.get(Domain, case.domain_id)
        artifacts = session.scalars(select(Artifact).where(Artifact.case_id == case.id)).all()
        signal = session.scalars(select(Signal).where(Signal.case_id == case.id)).one()
    assert domain is not None
    assert [str(ip) for ip in domain.ips] == ["88.198.0.1"]
    assert domain.nameservers == ["ns1.mintera.com", "ns2.registrar-servers.com"]
    assert (domain.registrar, domain.registrar_iana_id) == ("NameCheap, Inc.", 1068)
    assert domain.registrar_abuse_email == "abuse@namecheap.com"
    assert domain.registered_at == datetime(2000, 8, 11, 16, 15, 25, tzinfo=UTC)
    assert (domain.asn, domain.hosting_org, domain.hosting_country) == (
        24940,
        "Hetzner Online GmbH",
        "DE",
    )
    assert domain.hosting_abuse_email == "abuse@hetzner.com"
    assert domain.is_cloudflare is False
    assert domain.webrisk == {
        "threat_types": ["SOCIAL_ENGINEERING"],
        "expire_time": None,
        "checked_at": NOW.isoformat(),
    }
    assert domain.enriched_at == NOW

    # Raw RDAP and DNS answers are case evidence; the stored bytes match the recorded hash.
    assert sorted(a.kind.value for a in artifacts) == ["dns", "rdap"]
    for artifact in artifacts:
        data = object_store.read_bytes(artifact.storage_key)
        assert hashlib.sha256(data).hexdigest() == artifact.sha256
        assert object_store.stored_sha256(artifact.storage_key) == artifact.sha256
    rdap = next(a for a in artifacts if a.kind.value == "rdap")
    assert rdap.storage_key == (
        f"gs://{TEST_EVIDENCE_BUCKET}/cases/{case.id}/rdap/20261020T090000Z.json"
    )
    stored = json.loads(object_store.read_bytes(rdap.storage_key))
    assert stored["domain"] == _fixture("domain-namecheap.com.json")

    # The vanity name server ns1.mintera.com is the official site's; the registrar's is not.
    assert signal.name == "official_infrastructure"
    assert signal.value is True
    assert signal.detail["shared_nameservers"] == ["ns1.mintera.com"]
    assert signal.scan_id is None


def test_operator_contacts_override_rdap(database: Database) -> None:
    case = _open_case(database)
    with database.worker_session() as session:
        admin.set_channel_contact(
            session, kind=ContactKind.HOST, key="AS24940", actor="t", email="noc@hetzner.example"
        )
        admin.set_channel_contact(
            session, kind=ContactKind.REGISTRAR, key="1068", actor="t", email="legal@nc.example"
        )
    _enrich(database, case, _sources())
    with database.worker_session() as session:
        domain = session.get(Domain, case.domain_id)
    assert domain is not None
    assert domain.hosting_abuse_email == "noc@hetzner.example"
    assert domain.registrar_abuse_email == "legal@nc.example"


def test_a_tld_without_rdap_gets_a_note_not_a_failure(database: Database) -> None:
    case = _open_case(database, host="mintera-verify.com.tr")
    sources = _sources(domains={"mintera-verify.com.tr": RdapUnavailableError("tr")})
    result = _enrich(database, case, sources, final=False)
    assert result.complete
    _enrich_again = service.enrich_domain(
        database, case.domain_id, sources, now=lambda: NOW, final_attempt=False, force=True
    )
    assert _enrich_again.complete
    with database.worker_session() as session:
        notes = session.scalars(
            select(CaseEvent.data).where(
                CaseEvent.case_id == case.id, CaseEvent.type == EventType.NOTE
            )
        ).all()
    assert notes == [{"note": "rdap_unavailable"}]  # written once


def test_partial_failure_is_retried_then_recorded(database: Database) -> None:
    case = _open_case(database)
    failing = _sources(
        resolver=FakeResolver({("mintera-login.com", "A"): LookupFailedError("A timeout")})
    )
    with pytest.raises(service.EnrichmentIncompleteError):
        _enrich(database, case, failing, final=False)
    with database.worker_session() as session:
        domain = session.get(Domain, case.domain_id)
        assert domain is not None
        assert domain.enriched_at is None
        assert domain.registrar == "NameCheap, Inc."  # what succeeded is kept

    result = _enrich(database, case, failing, final=True)
    assert not result.complete
    assert result.precheck_case_ids == [case.id]  # the pipeline moves on
    with database.worker_session() as session:
        failed = session.scalars(
            select(CaseEvent).where(CaseEvent.type == EventType.JOB_FAILED)
        ).one()
    assert failed.data["job"] == "enrich_domain"
    assert "dns_a" in failed.data["errors"]


class Exploding(FakeRdap):
    def domain(self, name: str) -> dict[str, Any] | None:
        raise AssertionError("must not query again")


def test_a_case_on_a_fresh_domain_still_gets_evidence_and_its_signal(
    database: Database, object_store: ObjectStore
) -> None:
    cache = MemoryCache()
    first = _open_case(database)
    _enrich(database, first, _sources(object_store, cache=cache))

    # Another customer finds the same domain an hour later.
    other = _customer_brand(database, "other", officials=("otherbank.com",))
    second = _admit(database, _match(database, other, "mintera-login.com"))
    sources = _sources(object_store, cache=cache)
    sources.rdap = Exploding({}, {})
    result = service.enrich_domain(
        database, first.domain_id, sources, now=lambda: NOW + timedelta(hours=1), final_attempt=True
    )
    assert result.skipped  # no lookups
    assert sorted(result.precheck_case_ids) == sorted([first.id, second.case_id])
    with database.worker_session() as session:
        kinds = {
            case_id: sorted(kind.value for kind in kinds_)
            for case_id, kinds_ in _artifact_kinds(session).items()
        }
        signals: dict[uuid.UUID, Any] = dict(
            session.execute(select(Signal.case_id, Signal.value)).all()
        )
    assert kinds[second.case_id] == ["dns", "rdap"]
    assert kinds[first.id] == ["dns", "rdap"]  # nothing duplicated
    assert signals[first.id] is True  # ns1.mintera.com is customer one's
    assert signals[second.case_id] is False  # customer two's officials share nothing


def _artifact_kinds(session: Any) -> dict[uuid.UUID, list[Any]]:
    found: dict[uuid.UUID, list[Any]] = {}
    for case_id, kind in session.execute(select(Artifact.case_id, Artifact.kind)):
        found.setdefault(case_id, []).append(kind)
    return found


def test_a_domain_on_a_private_ip_completes_without_hosting(database: Database) -> None:
    case = _open_case(database)
    answers = {
        ("mintera-login.com", "A"): ["127.0.0.1"],
        ("mintera.com", "A"): ["203.0.113.10"],
    }
    with database.worker_session() as session:
        domain = session.get(Domain, case.domain_id)
        assert domain is not None
        domain.asn, domain.hosting_org = 24940, "Old Host"
    result = _enrich(database, case, _sources(resolver=FakeResolver(answers)), final=False)
    assert result.complete
    with database.worker_session() as session:
        domain = session.get(Domain, case.domain_id)
    assert domain is not None
    assert (domain.asn, domain.hosting_org, domain.hosting_abuse_email) == (None, None, None)
    assert domain.enriched_at == NOW


def test_a_failed_asn_lookup_is_retried(database: Database) -> None:
    case = _open_case(database)
    answers = {
        ("mintera-login.com", "A"): ["88.198.0.1"],
        ("1.0.198.88.origin.asn.cymru.com", "TXT"): LookupFailedError("TXT timeout"),
    }
    with pytest.raises(service.EnrichmentIncompleteError, match="asn"):
        _enrich(database, case, _sources(resolver=FakeResolver(answers)), final=False)


def test_unknown_infrastructure_is_written_as_unknown(database: Database) -> None:
    case = _open_case(database)
    answers = {
        ("mintera-login.com", "A"): ["198.51.100.7"],
        ("mintera.com", "A"): LookupFailedError("A timeout"),
    }
    with pytest.raises(service.EnrichmentIncompleteError, match="official_footprint"):
        _enrich(database, case, _sources(resolver=FakeResolver(answers)), final=False)
    with database.worker_session() as session:
        signal = session.scalars(select(Signal)).one()
    assert signal.value is None
    assert signal.detail["unknown_official_domains"] == ["mintera.com"]

    def no_ranges() -> list[str]:
        raise ConnectionError("cloudflare api down")

    good = {("mintera-login.com", "A"): ["198.51.100.7"], ("mintera.com", "A"): ["203.0.113.1"]}
    with pytest.raises(service.EnrichmentIncompleteError, match="cloudflare"):
        _enrich(
            database, case, _sources(resolver=FakeResolver(good), ranges=no_ranges), final=False
        )
    with database.worker_session() as session:
        signal = session.scalars(select(Signal)).one()
        domain = session.get(Domain, case.domain_id)
    assert signal.value is None
    assert domain is not None
    assert domain.is_cloudflare is False  # unknown is not written over the stored value


def test_cloudflare_ips_do_not_make_infrastructure_official(database: Database) -> None:
    case = _open_case(database)
    answers = {
        ("mintera-login.com", "A"): ["104.18.1.1"],
        ("mintera.com", "A"): ["104.18.1.1"],
        ("mintera-login.com", "NS"): ["lara.ns.cloudflare.com."],
        ("mintera.com", "NS"): ["lara.ns.cloudflare.com."],
    }
    _enrich(database, case, _sources(resolver=FakeResolver(answers)))
    with database.worker_session() as session:
        domain = session.get(Domain, case.domain_id)
        signal = session.scalars(select(Signal)).one()
    assert domain is not None
    assert domain.is_cloudflare is True
    assert signal.value is False


# --- whitelist changes through the outbox ---------------------------------------------------------


def test_whitelist_change_dismisses_open_cases_through_the_outbox(database: Database) -> None:
    brand = _customer_brand(database)
    detected = _admit(database, _match(database, brand, "mintera-login.com")).case_id
    watching = _admit(database, _match(database, brand, "mintera-offers.com")).case_id
    other = _admit(database, _match(database, brand, "mintera-other.com")).case_id
    with database.worker_session() as session:
        case = session.get(Case, watching)
        assert case is not None
        transition(session, case, CaseState.WATCHING, actor="test", at=NOW)
        customer = session.get(Customer, brand.customer_id)
        assert customer is not None
        report = admin.import_whitelist(
            session,
            customer,
            "pattern,kind\nmintera-login.com,affiliate\nmintera-offers.com,partner\n",
            actor="t",
        )
    assert report.imported == 2

    sent = Sent()
    assert outbox.dispatch(database, sent, lambda: NOW) == 1
    assert sent.messages == [("whitelist_changed", {"customer_id": str(brand.customer_id)})]
    assert outbox.dispatch(database, Sent(), lambda: NOW) == 0  # dispatched once

    with database.worker_session() as session:
        dismissed = apply_whitelist(session, brand.customer_id, now=NOW)
    assert set(dismissed) == {detected, watching}
    with database.worker_session() as session:
        states = dict(session.execute(select(Case.id, Case.state)).all())
        events = session.scalars(
            select(CaseEvent.type).where(CaseEvent.case_id == watching).order_by(CaseEvent.at)
        ).all()
    assert states[detected] is CaseState.DISMISSED
    assert states[watching] is CaseState.DISMISSED
    assert states[other] is CaseState.DETECTED
    assert events[-1] is EventType.WHITELISTED


def test_outbox_skips_demo_jobs_and_keeps_failed_sends(database: Database) -> None:
    demo = _customer_brand(database, "demo", is_demo=True)
    real = _customer_brand(database, "real")
    with database.worker_session() as session:
        session.add(
            JobOutbox(kind=WHITELIST_CHANGED, payload={"customer_id": str(demo.customer_id)})
        )
        session.add(JobOutbox(kind="no_such_job", payload={}))
        session.add(
            JobOutbox(kind=WHITELIST_CHANGED, payload={"customer_id": str(real.customer_id)})
        )

    def broken(actor: str, **kwargs: str) -> None:
        raise ConnectionError("redis is down")

    for _ in range(15):  # a long broker outage uses up no attempts
        assert outbox.dispatch(database, broken, lambda: NOW) == 0
    sent = Sent()
    assert outbox.dispatch(database, sent, lambda: NOW) == 1
    assert sent.messages == [("whitelist_changed", {"customer_id": str(real.customer_id)})]
    with database.worker_session() as session:
        rows = {row.kind: row for row in session.scalars(select(JobOutbox))}
        demo_row = session.scalars(
            select(JobOutbox).where(
                JobOutbox.payload["customer_id"].astext == str(demo.customer_id)
            )
        ).one()
    assert rows["no_such_job"].dispatched_at is None
    assert rows["no_such_job"].attempts == outbox.MAX_ATTEMPTS  # then left for the ops panel
    assert rows[WHITELIST_CHANGED].dispatched_at is not None
    assert demo_row.dispatched_at == NOW  # marked, never sent


def test_a_bad_payload_does_not_block_the_outbox(database: Database) -> None:
    real = _customer_brand(database, "real")
    with database.worker_session() as session:
        session.add(JobOutbox(kind=WHITELIST_CHANGED, payload={"customer_id": "not-a-uuid"}))
        session.add(
            JobOutbox(kind=WHITELIST_CHANGED, payload={"customer_id": str(real.customer_id)})
        )
    sent = Sent()
    assert outbox.dispatch(database, sent, lambda: NOW) == 1
    assert sent.messages == [("whitelist_changed", {"customer_id": str(real.customer_id)})]


def test_official_domain_add_queues_enrichment_and_a_whitelist_check(database: Database) -> None:
    brand = _customer_brand(database, officials=("mintera.com",))
    with database.worker_session() as session:
        jobs = session.execute(select(JobOutbox.kind, JobOutbox.payload)).all()
        domain_id = session.scalar(select(Domain.id).where(Domain.name == "mintera.com"))
    assert sorted(kind for kind, _ in jobs) == [
        ENRICH_DOMAIN,
        REFRESH_BRAND_FAVICONS,
        REFRESH_BRAND_REFERENCES,
        WHITELIST_CHANGED,
    ]
    assert {"brand_id": str(brand.id), "domain": "mintera.com"} in [p for _, p in jobs]
    assert {"domain_id": str(domain_id)} in [payload for _, payload in jobs]
    assert {"customer_id": str(brand.customer_id)} in [payload for _, payload in jobs]


# --- reconciler -----------------------------------------------------------------------------------


def test_reconciler_requeues_stuck_cases_and_unchecked_matches(database: Database) -> None:
    brand = _customer_brand(database)
    old = NOW - timedelta(hours=3)
    stuck = _admit_at(database, brand, "mintera-stuck.com", old)
    reviewed = _admit_at(database, brand, "mintera-review.com", old)
    failed = _admit_at(database, brand, "mintera-failed.com", old)
    _admit_at(database, brand, "mintera-new.com", NOW - timedelta(minutes=30))
    with database.worker_session() as session:
        session.add(
            ReviewTask(
                customer_id=brand.customer_id,
                case_id=reviewed,
                kind=TaskKind.REVIEW,
                reason="test",
                status=TaskStatus.OPEN,
            )
        )
        session.add(
            CaseEvent(
                customer_id=brand.customer_id,
                case_id=failed,
                at=old + timedelta(minutes=5),
                type=EventType.JOB_FAILED,
                actor="system",
                data={},
            )
        )
    lost = _match(database, brand, "mintera-lost.com", at=NOW - timedelta(minutes=20))
    _match(database, brand, "mintera-just-now.com", at=NOW - timedelta(minutes=2))
    demo = _customer_brand(database, "demo", is_demo=True)
    _match(database, demo, "mintera-demo.com", at=NOW - timedelta(hours=5))

    sent = Sent()
    result = reconciler.reconcile(database, sent, NOW)
    assert result == reconciler.Reconciled(prechecks=1, admissions=1)
    assert sorted(sent.messages) == [
        ("admit_match", {"match_id": str(lost)}),
        ("http_precheck", {"case_id": str(stuck)}),
    ]


def _admit_at(database: Database, brand: Brand, host: str, at: datetime) -> uuid.UUID:
    match_id = _match(database, brand, host, at=at)
    with database.worker_session() as session:
        result = admit(session, match_id, now=at, prefix="KLK")
    assert result.case_id is not None
    return result.case_id


# --- actors, CLI and the RDAP rate limit ----------------------------------------------------------


def test_admit_match_actor_queues_enrichment_then_precheck(
    monkeypatch: pytest.MonkeyPatch, database: Database, test_settings: Settings
) -> None:
    from kalkan.workers import discovery, enrichment

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(discovery, "get_database", lambda: database)
    monkeypatch.setattr(discovery, "get_settings", lambda: test_settings)
    monkeypatch.setattr(discovery.enrich_domain, "send", lambda d: sent.append(("enrich", d)))
    monkeypatch.setattr(enrichment, "get_database", lambda: database)
    monkeypatch.setattr(enrichment, "sources", _sources)
    monkeypatch.setattr(enrichment.http_precheck, "send", lambda c: sent.append(("precheck", c)))

    brand = _customer_brand(database)
    match_id = str(_match(database, brand, "mintera-login.com"))
    discovery.admit_match.fn(match_id)
    with database.worker_session() as session:
        case = session.scalars(select(Case)).one()
    assert sent == [("enrich", str(case.domain_id))]

    enrichment.enrich_domain.fn(str(case.domain_id))
    assert sent[-1] == ("precheck", str(case.id))

    # A redelivered admission (its first follow-up send was lost) queues enrichment again.
    discovery.admit_match.fn(match_id)
    assert sent[-1] == ("enrich", str(case.domain_id))

    # A second customer's match on the now fresh domain also goes through enrich_domain.
    other = _customer_brand(database, "other")
    discovery.admit_match.fn(str(_match(database, other, "mintera-login.com")))
    assert sent[-1] == ("enrich", str(case.domain_id))


def test_the_last_enrichment_attempt_gives_up_into_the_precheck(
    monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    from kalkan.workers import enrichment

    sent: list[str] = []
    monkeypatch.setattr(enrichment, "get_database", lambda: database)
    monkeypatch.setattr(enrichment.http_precheck, "send", sent.append)

    def broken_sources() -> service.Sources:
        sources = _sources()
        sources.rdap = Exploding({}, {})  # an unexpected error inside a source...
        sources.cloudflare_ranges = lambda: (_ for _ in ()).throw(MemoryError("boom"))
        return sources

    monkeypatch.setattr(enrichment, "sources", broken_sources)
    monkeypatch.setattr(service, "collect", lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))
    case = _open_case(database)
    # Called directly there is no message, which counts as the last attempt.
    enrichment.enrich_domain.fn(str(case.domain_id))
    assert sent == [str(case.id)]
    with database.worker_session() as session:
        failed = session.scalars(
            select(CaseEvent).where(CaseEvent.type == EventType.JOB_FAILED)
        ).one()
    assert failed.data["errors"] == {"error": "KeyError: 'x'"}


runner = CliRunner()


def test_cli_contact_set(monkeypatch: pytest.MonkeyPatch, database: Database) -> None:
    monkeypatch.setattr(cli, "get_database", lambda: database)
    result = runner.invoke(
        cli.app, ["contact", "set", "host", "AS24940", "--email", "abuse@hetzner.example"]
    )
    assert result.exit_code == 0, result.output
    again = runner.invoke(
        cli.app, ["contact", "set", "host", "24940", "--form-url", "https://abuse.example/"]
    )
    assert again.exit_code == 0, again.output
    with database.worker_session() as session:
        contact = session.scalars(select(ChannelContact)).one()
    assert (contact.kind, contact.key, contact.email) == (ContactKind.HOST, "24940", None)
    assert contact.form_url == "https://abuse.example/"
    for args, message in [
        (["contact", "set", "host", "hetzner", "--email", "a@b.example"], "digits"),
        (["contact", "set", "registrar", "1068"], "e-mail address, a form URL"),
        (["contact", "set", "registrar", "1068", "--email", "nope"], "not an e-mail"),
        (["contact", "set", "registrar", "1068", "--form-url", "http://x.example"], "https://"),
    ]:
        failed = runner.invoke(cli.app, args)
        assert failed.exit_code == 1
        assert message in failed.output


@pytest.fixture
def bucket_redis() -> Iterator[redis.Redis]:
    client = redis.Redis.from_url(redis_url_for_tests())
    for key in client.scan_iter(f"{RATE_PREFIX}*"):
        client.delete(key)
    yield client
    for key in client.scan_iter(f"{RATE_PREFIX}*"):
        client.delete(key)
    client.close()


def test_token_bucket_in_redis(bucket_redis: redis.Redis) -> None:
    clock = [1_000.0]
    bucket = RedisTokenBucket(bucket_redis, clock=lambda: clock[0])
    key = f"{RATE_PREFIX}rdap.example"
    waits = [bucket.acquire(key, rate=2, burst=3) for _ in range(4)]
    assert waits[:3] == [0, 0, 0]
    assert waits[3] == pytest.approx(0.5)
    clock[0] += 0.5
    assert bucket.acquire(key, rate=2, burst=3) == 0
    assert bucket.acquire(f"{RATE_PREFIX}other.example", rate=2, burst=3) == 0  # per server
    assert 0 < bucket_redis.pttl(key) <= 2_500


def test_domains_are_shared_across_customers(database: Database) -> None:
    first = _customer_brand(database, "first")
    second = _customer_brand(database, "second")
    _admit(database, _match(database, first, "mintera-login.com"))
    _admit(database, _match(database, second, "mintera-login.com"))
    with database.worker_session() as session:
        assert session.scalar(select(func.count()).select_from(Domain)) == 1
        assert session.scalar(select(func.count()).select_from(Case)) == 2
        upsert_domain(session, view_host("mintera-login.com"), NOW)
        assert session.scalar(select(func.count()).select_from(Domain)) == 1


def test_whitelist_import_refuses_shared_platform_roots(database: Database) -> None:
    brand = _customer_brand(database)
    with database.worker_session() as session:
        customer = session.get(Customer, brand.customer_id)
        assert customer is not None
        report = admin.import_whitelist(
            session,
            customer,
            "pattern,kind\ngithub.io,other\nmintera.github.io,official\n",
            actor="t",
        )
    assert report.imported == 0
    assert "shared platform root" in report.errors[0].message


def test_whitelist_change_runs_end_to_end_through_the_actor(
    monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    from kalkan.workers import discovery

    monkeypatch.setattr(discovery, "get_database", lambda: database)
    brand = _customer_brand(database)
    reviewing = _admit(database, _match(database, brand, "mintera-review.com")).case_id
    with database.worker_session() as session:
        case = session.get(Case, reviewing)
        assert case is not None
        transition(session, case, CaseState.IN_REVIEW, actor="test", reason="unclear", at=NOW)
        current = session.get(Brand, brand.id)
        assert current is not None
        # The customer's new official domain is the domain of an open case.
        admin.add_official_domain(session, current, "mintera-review.com", actor="test")

    def run_actor(actor: str, **kwargs: str) -> None:
        if actor == "whitelist_changed":
            getattr(discovery, actor).fn(**kwargs)

    outbox.dispatch(database, run_actor, lambda: NOW)
    with database.worker_session() as session:
        case = session.get(Case, reviewing)
        task = session.scalars(select(ReviewTask)).one()
    assert case is not None
    assert case.state is CaseState.DISMISSED
    assert task.status is TaskStatus.DONE
    assert task.note == "case moved to dismissed"


def test_reconciler_skips_scanned_demo_and_blocked_cases(database: Database) -> None:
    brand = _customer_brand(database)
    old = NOW - timedelta(hours=3)
    scanned = _admit_at(database, brand, "mintera-scanned.com", old)
    # Pre-checked long ago and sent to the browser stage: not pre-checked again (K74).
    scanned_earlier = _admit_at(database, brand, "mintera-capture-wait.com", old)
    enrich_failed = _admit_at(database, brand, "mintera-enrich.com", old)
    capture_failed = _admit_at(database, brand, "mintera-capture.com", old)
    with database.worker_session() as session:
        for case_id, at in ((scanned, NOW - timedelta(minutes=30)), (scanned_earlier, old)):
            session.add(
                Scan(
                    customer_id=brand.customer_id,
                    case_id=case_id,
                    stage=ScanStage.HTTP,
                    started_at=at + timedelta(minutes=1),
                )
            )
        for case_id, job in ((enrich_failed, "enrich_domain"), (capture_failed, "browser_capture")):
            session.add(
                CaseEvent(
                    customer_id=brand.customer_id,
                    case_id=case_id,
                    at=old + timedelta(minutes=5),
                    type=EventType.JOB_FAILED,
                    actor="system",
                    data={"job": job},
                )
            )
    demo = _customer_brand(database, "demo", is_demo=True)
    with database.worker_session() as session:
        owner = session.get(Customer, demo.customer_id)
        assert owner is not None
        factories.case(session, owner, session.get(Brand, demo.id))  # type: ignore[arg-type]
    sent = Sent()
    reconciler.reconcile(database, sent, NOW)
    assert sent.messages == [("http_precheck", {"case_id": str(enrich_failed)})]


def test_contact_keys_are_canonical(database: Database) -> None:
    with database.worker_session() as session:
        contact = admin.set_channel_contact(
            session, kind=ContactKind.HOST, key="AS024940", actor="t", email=" Abuse@Host.Example "
        )
    assert (contact.key, contact.email) == ("24940", "abuse@host.example")
    with database.worker_session() as session, pytest.raises(admin.AdminError):
        admin.set_channel_contact(
            session, kind=ContactKind.HOST, key="1", actor="t", email="abuse@host.example\nBcc: x"
        )
