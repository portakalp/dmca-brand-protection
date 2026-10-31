"""``enrich_domain``: DNS, RDAP, Cloudflare and Web Risk for one domain (architecture.md §9).

- The lookups, and the DNS footprint of the customers' official domains, run before the
  write transaction; that transaction only writes. The domain row is locked with
  ``FOR NO KEY UPDATE`` so two jobs for one domain write one after the other.
- A domain enriched in the last 24 hours is not looked up again: the last answers are kept
  in Redis for a day, and open cases that have no evidence or signal yet get them from
  there (K67).
- Partial success counts: every source that answered is written. ``enriched_at`` is set only
  when no source failed; otherwise the job is retried, and the last attempt records
  ``job_failed`` on the domain's ``detected`` cases, which still go on to the pre-check.
- A TLD without RDAP is not a failure: the open cases get a ``rdap_unavailable`` note.
- Only public IPs are looked up; a domain pointing at 127.0.0.1 is not an error.
- ``channel_contacts`` overrides the abuse addresses found in RDAP (key: ASN or IANA id).
- ``official_infrastructure`` (§12, K57, K64) is true, false, or ``None`` when it cannot be
  known (Cloudflare's ranges or an official domain's DNS unavailable); the decision rules
  treat ``None`` like true.
"""

from __future__ import annotations

import ipaddress
import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from kalkan.db.base import new_id
from kalkan.db.models import (
    Artifact,
    Case,
    CaseEvent,
    ChannelContact,
    Customer,
    Domain,
    OfficialDomain,
    Signal,
)
from kalkan.db.session import Database
from kalkan.discovery.psl import split_host
from kalkan.domain.enums import ArtifactKind, CaseState, ContactKind, EventType
from kalkan.enrichment import dns as dnslookup
from kalkan.enrichment.admission import CLOSED_STATES, SYSTEM, needs_enrichment
from kalkan.enrichment.cloudflare import cloudflare_nameserver, in_ranges, is_cloudflare
from kalkan.enrichment.rdap import (
    RdapThrottledError,
    RdapUnavailableError,
    parse_asn,
    parse_domain,
    parse_ip,
)
from kalkan.logging import get_logger
from kalkan.storage import paths
from kalkan.storage.artifacts import store_artifact
from kalkan.storage.gcs import ObjectExistsError, ObjectStore

OFFICIAL_INFRASTRUCTURE = "official_infrastructure"
RDAP_UNAVAILABLE = "rdap_unavailable"
FINDINGS_PREFIX = "enrich:findings:"
FINDINGS_SECONDS = 24 * 3600

log = get_logger(__name__)


class Rdap(Protocol):
    def domain(self, name: str) -> dict[str, Any] | None: ...

    def ip(self, address: str) -> dict[str, Any] | None: ...

    def asn(self, number: int) -> dict[str, Any] | None: ...


class WebRisk(Protocol):
    def lookup(self, host: str) -> dict[str, Any]: ...


class Cache(Protocol):
    def get(self, name: str) -> Any: ...

    def set(self, name: str, value: str, *, ex: int) -> Any: ...


@dataclass
class Sources:
    resolver: dnslookup.Resolver
    rdap: Rdap
    cloudflare_ranges: Callable[[], list[str]]
    webrisk: WebRisk | None = None
    store: ObjectStore | None = None
    evidence_bucket: str | None = None
    cache: Cache | None = None


@dataclass
class Findings:
    dns: dnslookup.DnsResult
    rdap_domain: dict[str, Any] | None = None
    rdap_ip: dict[str, Any] | None = None
    rdap_asn: dict[str, Any] | None = None
    rdap_unavailable: bool = False
    origin: dnslookup.Origin | None = None
    cloudflare: bool | None = None
    webrisk: dict[str, Any] | None = None
    errors: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        data = asdict(self)
        data["dns"] = self.dns.as_json()
        return json.dumps(data, default=str)

    @classmethod
    def from_json(cls, text: str | bytes) -> Findings:
        data = json.loads(text)
        dns = dnslookup.DnsResult(**data.pop("dns"))
        origin = data.pop("origin")
        return cls(dns=dns, origin=dnslookup.Origin(**origin) if origin else None, **data)


@dataclass(frozen=True)
class Enrichment:
    domain_id: uuid.UUID
    complete: bool
    skipped: bool  # no lookups ran: the domain was fresh
    precheck_case_ids: list[uuid.UUID]
    errors: dict[str, str]


class EnrichmentIncompleteError(Exception):
    """Some sources failed; the partial result is saved and the job should be retried."""


def public_ips(ips: Iterable[str]) -> list[str]:
    """Public addresses, IPv4 first, in numeric order."""
    found = {ipaddress.ip_address(ip) for ip in ips}
    public = [ip for ip in found if ip.is_global]
    return [str(ip) for ip in sorted(public, key=lambda ip: (ip.version, int(ip)))]


def collect(host: str, registered_domain: str, sources: Sources) -> Findings:
    """Ask every source; nothing here touches the database."""
    findings = Findings(dns=dnslookup.lookup(host, registered_domain, sources.resolver))
    findings.errors.update({f"dns_{k.lower()}": v for k, v in findings.dns.errors.items()})
    try:
        findings.rdap_domain = sources.rdap.domain(registered_domain)
    except RdapUnavailableError:
        findings.rdap_unavailable = True
    except RdapThrottledError as exc:
        findings.errors["rdap_domain"] = f"throttled: {exc}"
    except Exception as exc:  # noqa: BLE001 - a failing source must not stop the others
        findings.errors["rdap_domain"] = f"{type(exc).__name__}: {exc}"

    addresses = public_ips(findings.dns.ips)
    if addresses:
        try:
            findings.origin = dnslookup.origin_for_ip(addresses[0], sources.resolver)
        except dnslookup.LookupFailedError as exc:
            findings.errors["asn"] = str(exc)
        try:
            findings.rdap_ip = sources.rdap.ip(addresses[0])
        except Exception as exc:  # noqa: BLE001
            findings.errors["rdap_ip"] = f"{type(exc).__name__}: {exc}"
        ip_owner = parse_ip(findings.rdap_ip) if findings.rdap_ip is not None else {}
        if findings.origin is not None and not ip_owner.get("abuse_email"):
            try:
                findings.rdap_asn = sources.rdap.asn(findings.origin.asn)
            except Exception as exc:  # noqa: BLE001
                findings.errors["rdap_asn"] = f"{type(exc).__name__}: {exc}"

    nameservers = findings.dns.nameservers or _rdap_nameservers(findings.rdap_domain)
    if any(cloudflare_nameserver(name) for name in nameservers):
        findings.cloudflare = True
    elif "A" not in findings.dns.errors and "AAAA" not in findings.dns.errors:
        try:
            findings.cloudflare = is_cloudflare(
                findings.dns.ips, nameservers, sources.cloudflare_ranges()
            )
        except Exception as exc:  # noqa: BLE001
            findings.errors["cloudflare"] = f"{type(exc).__name__}: {exc}"

    if sources.webrisk is not None and not findings.dns.nxdomain:
        try:
            findings.webrisk = sources.webrisk.lookup(host)
        except Exception as exc:  # noqa: BLE001
            findings.errors["webrisk"] = f"{type(exc).__name__}: {exc}"
    return findings


def _rdap_nameservers(rdap_domain: dict[str, Any] | None) -> list[str]:
    if rdap_domain is None:
        return []
    nameservers: list[str] = parse_domain(rdap_domain)["nameservers"]
    return nameservers


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _override(session: Session, kind: ContactKind, key: int | None) -> str | None:
    if key is None:
        return None
    email: str | None = session.scalar(
        select(ChannelContact.email).where(
            ChannelContact.kind == kind, ChannelContact.key == str(key)
        )
    )
    return email


def apply(session: Session, domain: Domain, findings: Findings, now: datetime) -> None:
    """Write the findings onto ``domain``; keeps earlier values a failed source did not give."""
    addresses_known = "A" not in findings.dns.errors and "AAAA" not in findings.dns.errors
    if addresses_known:
        domain.ips = [ipaddress.ip_address(ip) for ip in findings.dns.ips]
    nameservers = findings.dns.nameservers or _rdap_nameservers(findings.rdap_domain)
    if nameservers or "NS" not in findings.dns.errors:
        domain.nameservers = nameservers
    if findings.rdap_domain is not None:
        parsed = parse_domain(findings.rdap_domain)
        domain.registrar = parsed["registrar"]
        domain.registrar_iana_id = parsed["registrar_iana_id"]
        domain.registrar_abuse_email = parsed["registrar_abuse_email"]
        domain.registered_at = _parse_datetime(parsed["registered_at"])
    domain.registrar_abuse_email = (
        _override(session, ContactKind.REGISTRAR, domain.registrar_iana_id)
        or domain.registrar_abuse_email
    )

    if addresses_known and not public_ips(findings.dns.ips):
        # No public address: whatever hosted the domain before does not any more.
        domain.asn = None
        domain.hosting_org = domain.hosting_country = domain.hosting_abuse_email = None
    elif addresses_known and "asn" not in findings.errors:
        domain.asn = findings.origin.asn if findings.origin is not None else None
    if findings.rdap_ip is not None:
        owner = parse_ip(findings.rdap_ip)
        holder = parse_asn(findings.rdap_asn) if findings.rdap_asn is not None else {}
        domain.hosting_org = owner["org"] or holder.get("org")
        # ARIN puts the country only into postal addresses; Team Cymru reports its code.
        domain.hosting_country = owner["country"] or (
            findings.origin.country if findings.origin is not None else None
        )
        domain.hosting_abuse_email = owner["abuse_email"] or holder.get("abuse_email")
    domain.hosting_abuse_email = (
        _override(session, ContactKind.HOST, domain.asn) or domain.hosting_abuse_email
    )
    if findings.cloudflare is not None:
        domain.is_cloudflare = findings.cloudflare
    if findings.webrisk is not None:
        domain.webrisk = {**findings.webrisk, "checked_at": now.isoformat()}
    if not findings.errors:
        domain.enriched_at = now


# --- official infrastructure ------------------------------------------------------------------


@dataclass(frozen=True)
class Footprint:
    host: str
    ips: frozenset[str]
    nameservers: frozenset[str]
    unknown: bool = False  # its DNS could not be read now and nothing was stored before


# Addresses no two independent sites can be said to share: loopback, private networks,
# CGNAT, link-local and unspecified (K73). A clone and an official site that both resolve
# to one of these (a local fixture network, a 127.0.0.1 sinkhole) share nothing.
_UNSHAREABLE = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def shareable_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_multicast and not any(ip in network for network in _UNSHAREABLE)


def official_footprints(
    session: Session, customer_id: uuid.UUID, sources: Sources, now: datetime
) -> list[Footprint]:
    """IPs and name servers of the customer's official domains.

    A stored copy younger than 24 hours is used as it is; otherwise DNS is asked, and when
    that fails the stored copy of any age is used.
    """
    hosts = sorted(
        set(
            session.scalars(
                select(OfficialDomain.domain).where(OfficialDomain.customer_id == customer_id)
            )
        )
    )
    footprints = []
    for host in hosts:
        row = session.scalar(select(Domain).where(Domain.name == host))
        stored = None
        if row is not None and (row.ips or row.nameservers):
            stored = Footprint(
                host, frozenset(str(ip) for ip in row.ips), frozenset(row.nameservers)
            )
        if row is not None and stored is not None and not needs_enrichment(row, now):
            footprints.append(stored)
            continue
        _, label, suffix = split_host(host)
        result = dnslookup.lookup(host, f"{label}.{suffix}", sources.resolver)
        if result.errors:
            footprints.append(stored or Footprint(host, frozenset(), frozenset(), unknown=True))
        else:
            footprints.append(Footprint(host, frozenset(result.ips), frozenset(result.nameservers)))
    return footprints


def shared_infrastructure(
    ips: Iterable[str],
    nameservers: Iterable[str],
    officials: Iterable[Footprint],
    cloudflare_ranges: Iterable[str] | None,
) -> dict[str, Any]:
    """What the domain shares with official domains, ignoring shared CDN infrastructure.

    ``match`` is None when nothing matched but the answer cannot be known: Cloudflare's
    ranges are unavailable, or an official domain's DNS footprint is unknown.
    """
    officials = list(officials)
    official_roots = set()
    for official in officials:
        _, label, suffix = split_host(official.host)
        official_roots.add(f"{label}.{suffix}")
    ranges = tuple(cloudflare_ranges or ())
    own_ips = {str(ip) for ip in ips if shareable_ip(str(ip)) and not in_ranges(str(ip), ranges)}
    vanity = set()
    for name in nameservers:
        if cloudflare_nameserver(name):
            continue
        _, label, suffix = split_host(name)
        if f"{label}.{suffix}" in official_roots:
            vanity.add(name)
    shared_ips: set[str] = set()
    shared_ns: set[str] = set()
    matched: set[str] = set()
    for official in officials:
        ips_here = own_ips & official.ips
        ns_here = vanity & official.nameservers
        if ips_here or ns_here:
            matched.add(official.host)
            shared_ips |= ips_here
            shared_ns |= ns_here
    unknown = sorted(official.host for official in officials if official.unknown)
    match: bool | None = bool(matched)
    if not matched and (cloudflare_ranges is None or unknown):
        match = None
    return {
        "match": match,
        "official_domains": sorted(matched),
        "shared_ips": sorted(shared_ips),
        "shared_nameservers": sorted(shared_ns),
        "unknown_official_domains": unknown,
        "cloudflare_ranges": cloudflare_ranges is not None,
    }


def _write_signal(session: Session, case: Case, detail: dict[str, Any]) -> None:
    session.execute(
        insert(Signal)
        .values(
            id=new_id(),
            customer_id=case.customer_id,
            case_id=case.id,
            scan_id=None,
            name=OFFICIAL_INFRASTRUCTURE,
            value=detail["match"],
            detail=detail,
        )
        .on_conflict_do_update(
            index_elements=[Signal.case_id, Signal.scan_id, Signal.name],
            set_={"value": detail["match"], "detail": detail},
        )
    )


# --- per case writes ---------------------------------------------------------------------------


def _open_cases(session: Session, domain_id: uuid.UUID) -> list[Case]:
    return list(
        session.scalars(
            select(Case)
            .join(Customer, Customer.id == Case.customer_id)
            .where(
                Case.domain_id == domain_id,
                Case.state.not_in(CLOSED_STATES),
                Customer.is_demo.is_(False),
            )
            .order_by(Case.number)
        )
    )


def _evidence_kinds(session: Session, case_id: uuid.UUID) -> set[ArtifactKind]:
    return set(
        session.scalars(
            select(Artifact.kind).where(
                Artifact.case_id == case_id,
                Artifact.scan_id.is_(None),
                Artifact.kind.in_([ArtifactKind.DNS, ArtifactKind.RDAP]),
            )
        )
    )


def _store_evidence(
    session: Session,
    case: Case,
    findings: Findings,
    sources: Sources,
    now: datetime,
    *,
    only_missing: bool,
) -> None:
    if sources.store is None or sources.evidence_bucket is None:
        return
    present = _evidence_kinds(session, case.id) if only_missing else set()
    documents: list[tuple[ArtifactKind, str, Any]] = []
    if ArtifactKind.DNS not in present:
        documents.append((ArtifactKind.DNS, paths.case_dns(case.id, now), findings.dns.as_json()))
    has_rdap = any(x is not None for x in (findings.rdap_domain, findings.rdap_ip))
    if has_rdap and ArtifactKind.RDAP not in present:
        rdap = {"domain": findings.rdap_domain, "ip": findings.rdap_ip, "asn": findings.rdap_asn}
        documents.append((ArtifactKind.RDAP, paths.case_rdap(case.id, now), rdap))
    for kind, path, document in documents:
        try:
            store_artifact(
                session,
                sources.store,
                case,
                bucket=sources.evidence_bucket,
                path=path,
                data=json.dumps(document, indent=1, sort_keys=True, default=str).encode(),
                content_type="application/json",
                kind=kind,
            )
        except ObjectExistsError:
            # Another enrichment of the same domain stored this case's evidence this second.
            log.info("enrich.evidence_exists", case_id=str(case.id), kind=kind.value)


def _note_once(session: Session, case: Case, note: str, now: datetime) -> None:
    exists = session.scalar(
        select(CaseEvent.id).where(
            CaseEvent.case_id == case.id,
            CaseEvent.type == EventType.NOTE,
            CaseEvent.data["note"].astext == note,
        )
    )
    if exists is None:
        session.add(
            CaseEvent(
                customer_id=case.customer_id,
                case_id=case.id,
                at=now,
                type=EventType.NOTE,
                actor=SYSTEM,
                data={"note": note},
            )
        )


def _job_failed(
    session: Session, cases: Iterable[Case], errors: dict[str, str], at: datetime
) -> None:
    for case in cases:
        if case.state is CaseState.DETECTED:
            session.add(
                CaseEvent(
                    customer_id=case.customer_id,
                    case_id=case.id,
                    at=at,
                    type=EventType.JOB_FAILED,
                    actor=SYSTEM,
                    data={"job": "enrich_domain", "errors": errors},
                )
            )


# --- the job -----------------------------------------------------------------------------------


def _cached_findings(sources: Sources, domain_id: uuid.UUID) -> Findings | None:
    if sources.cache is None:
        return None
    raw = sources.cache.get(FINDINGS_PREFIX + str(domain_id))
    if raw is None:
        return None
    try:
        return Findings.from_json(raw)
    except (ValueError, TypeError, KeyError):
        return None


def _ranges(sources: Sources) -> list[str] | None:
    try:
        return sources.cloudflare_ranges()
    except Exception:  # noqa: BLE001 - the signal records that the ranges were unknown
        return None


def enrich_domain(
    database: Database,
    domain_id: uuid.UUID,
    sources: Sources,
    *,
    now: Callable[[], datetime],
    final_attempt: bool,
    force: bool = False,
) -> Enrichment:
    with database.worker_session() as session:
        domain = session.get(Domain, domain_id)
        if domain is None:
            return Enrichment(domain_id, False, True, [], {})
        host = domain.name
        registered = domain.registered_domain or host
        enriched_before = domain.enriched_at
        fresh = not force and not needs_enrichment(domain, now())
        customers = {case.customer_id for case in _open_cases(session, domain_id)}

    findings = _cached_findings(sources, domain_id) if fresh else None
    looked_up = findings is None
    if findings is None:
        findings = collect(host, registered, sources)
        if sources.cache is not None and not findings.errors:
            sources.cache.set(
                FINDINGS_PREFIX + str(domain_id), findings.to_json(), ex=FINDINGS_SECONDS
            )
    at = now()
    ranges = _ranges(sources)
    with database.worker_session() as session:
        footprints = {
            customer: official_footprints(session, customer, sources, at)
            for customer in sorted(customers)
        }
    if any(footprint.unknown for found in footprints.values() for footprint in found):
        findings.errors.setdefault("official_footprint", "an official domain's DNS is unknown")

    with database.worker_session() as session:
        domain = session.get(
            Domain, domain_id, with_for_update={"key_share": True}, populate_existing=True
        )
        if domain is None:  # pragma: no cover - domains are never deleted
            return Enrichment(domain_id, False, True, [], {})
        # Another job may have finished the same domain while this one was looking up.
        overtaken = looked_up and domain.enriched_at != enriched_before and not force
        if looked_up and not overtaken:
            apply(session, domain, findings, at)
        cases = _open_cases(session, domain_id)
        for case in cases:
            _store_evidence(
                session, case, findings, sources, at, only_missing=not looked_up or overtaken
            )
            if findings.rdap_unavailable:
                _note_once(session, case, RDAP_UNAVAILABLE, at)
            if case.customer_id not in footprints:  # a case opened during the lookups
                footprints[case.customer_id] = official_footprints(
                    session, case.customer_id, sources, at
                )
            detail = shared_infrastructure(
                [str(ip) for ip in domain.ips],
                domain.nameservers,
                footprints[case.customer_id],
                ranges,
            )
            _write_signal(session, case, detail)
        detected = [case.id for case in cases if case.state is CaseState.DETECTED]
        if findings.errors and final_attempt:
            _job_failed(session, cases, findings.errors, at)
    if findings.errors:
        log.warning("enrich.incomplete", domain=host, errors=findings.errors, final=final_attempt)
        if not final_attempt:
            raise EnrichmentIncompleteError(", ".join(sorted(findings.errors)))
    return Enrichment(domain_id, not findings.errors, not looked_up, detected, findings.errors)


def give_up(
    database: Database, domain_id: uuid.UUID, errors: dict[str, str], *, now: datetime
) -> list[uuid.UUID]:
    """The last attempt failed outright: record ``job_failed``; the detected cases move on."""
    with database.worker_session() as session:
        cases = _open_cases(session, domain_id)
        _job_failed(session, cases, errors, now)
        return [case.id for case in cases if case.state is CaseState.DETECTED]
