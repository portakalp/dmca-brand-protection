"""DNS records of a domain and the ASN of an IP (architecture.md §9).

dnspython with a 3 second lifetime per query. A, AAAA, CNAME and MX are asked for the host;
NS for its registered domain, because a subdomain usually has no NS records of its own. The
ASN comes from Team Cymru's IP-to-ASN DNS service (``origin.asn.cymru.com``), a known
provider queried over DNS (K63).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any, Protocol

import dns.exception
import dns.resolver

TIMEOUT_SECONDS = 3.0
HOST_RECORDS = ("A", "AAAA", "CNAME", "MX")


class Resolver(Protocol):
    def resolve(self, name: str, rdtype: str) -> list[str]:
        """Record texts; raises :class:`NoSuchDomainError`, or :class:`LookupFailedError`."""
        ...


class NoSuchDomainError(Exception):
    pass


class LookupFailedError(Exception):
    pass


class DnsPythonResolver:
    def __init__(self, timeout: float = TIMEOUT_SECONDS) -> None:
        self._resolver = dns.resolver.Resolver()
        self._resolver.lifetime = timeout

    def resolve(self, name: str, rdtype: str) -> list[str]:
        try:
            answer = self._resolver.resolve(name, rdtype)
        except dns.resolver.NXDOMAIN as exc:
            raise NoSuchDomainError(name) from exc
        except dns.resolver.NoAnswer:
            return []
        except (dns.resolver.NoNameservers, dns.exception.Timeout) as exc:
            raise LookupFailedError(f"{rdtype} {name}: {type(exc).__name__}") from exc
        return sorted(record.to_text() for record in answer)


@dataclass
class DnsResult:
    records: dict[str, list[str]] = field(default_factory=dict)
    nxdomain: bool = False
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ips(self) -> list[str]:
        return sorted({*self.records.get("A", []), *self.records.get("AAAA", [])})

    @property
    def nameservers(self) -> list[str]:
        return sorted({name.rstrip(".").lower() for name in self.records.get("NS", [])})

    def as_json(self) -> dict[str, Any]:
        return {"records": self.records, "nxdomain": self.nxdomain, "errors": self.errors}


def lookup(host: str, registered_domain: str, resolver: Resolver) -> DnsResult:
    result = DnsResult()
    queries = [(host, rdtype) for rdtype in HOST_RECORDS] + [(registered_domain, "NS")]
    for name, rdtype in queries:
        try:
            result.records[rdtype] = resolver.resolve(name, rdtype)
        except NoSuchDomainError:
            if name == registered_domain:
                result.nxdomain = True
            result.records[rdtype] = []
        except LookupFailedError as exc:
            result.errors[rdtype] = str(exc)
    return result


@dataclass(frozen=True)
class Origin:
    asn: int
    country: str | None  # ISO 3166-1 alpha-2 as Team Cymru reports it


def origin_for_ip(address: str, resolver: Resolver) -> Origin | None:
    """Origin ASN and country of ``address`` from Team Cymru; None when it is not routed.

    Raises :class:`LookupFailedError` when the lookup itself failed, so the job retries.
    """
    ip = ipaddress.ip_address(address)
    if ip.version == 4:
        name = ".".join(reversed(address.split("."))) + ".origin.asn.cymru.com"
    else:
        nibbles = ip.exploded.replace(":", "")
        name = ".".join(reversed(nibbles)) + ".origin6.asn.cymru.com"
    try:
        records = resolver.resolve(name, "TXT")
    except NoSuchDomainError:
        return None  # the address is not routed; a failed lookup raises instead
    for record in records:
        # "24940 | 88.198.0.0/16 | DE | ripencc | 2005-12-27"; several origins: first wins.
        fields = [part.strip() for part in record.strip('"').split("|")]
        asn = fields[0].split()[0] if fields and fields[0] else ""
        if asn.isdigit():
            country = fields[2].upper() if len(fields) > 2 and len(fields[2]) == 2 else None
            return Origin(int(asn), country)
    return None
