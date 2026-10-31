"""Stage 1: the HTTP pre-check (architecture.md §10).

One cheap request decides whether a ``detected`` case needs the browser stage:

1. No DNS, no connection, a parked or an empty page: ``watching``, re-scanned in 7 days.
2. A bot challenge: the browser stage (which will meet it again and send it to review).
3. The brand's favicon, the brand in the title or text, a password field, or a redirect to
   another brand-like domain: the browser stage.
4. None of these, but a match score of at least 80 and a domain at most 30 days old: the
   browser stage, in case the site hides its content from this kind of request.
5. Anything else: ``watching``.

A brand-like domain that redirects to an official site is not dismissed: clones send
scanners to the real site. It is watched and checked again the next day (K57).

Buttons, links and redirects are recorded (``cta_targets``). A brand-like target that is not
official becomes a new candidate; so does any other button, form or redirect target of a page
that presents itself as the brand, because a fake "login address" page leads to its clone or
to a third-party site (K57, K71). A case is pre-checked once per stay in ``detected``.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from kalkan.db.models import (
    Brand,
    BrandAsset,
    BrandMatch,
    Case,
    CaseEvent,
    Customer,
    Domain,
    DomainSighting,
    OfficialDomain,
    Scan,
    Signal,
)
from kalkan.db.session import Database
from kalkan.discovery.catalog import profile_for
from kalkan.discovery.matcher import Matcher, MatchResult
from kalkan.discovery.normalize import fold, plain, view_host
from kalkan.discovery.record import MANUAL_SCORE, Sighting, record_discovery
from kalkan.domain.case_state import transition
from kalkan.domain.enums import (
    BrandAssetKind,
    CaseState,
    EventType,
    MatchReason,
    Profile,
    ScanStage,
    SightingSource,
)
from kalkan.domain.hostnames import InvalidHostnameError
from kalkan.enrichment.whitelist import covers
from kalkan.logging import get_logger
from kalkan.net.safe_http import (
    ConnectionFailedError,
    FetchError,
    NoSuchHostError,
    Response,
    SafeHttpClient,
    SsrfBlockedError,
)
from kalkan.scanning import patterns
from kalkan.scanning.favicon import MAX_FAVICON_BYTES, favicon_hash, image_type
from kalkan.scanning.html import LinkInfo, PageInfo, decode, parse_page
from kalkan.scanning.profiles import DEFAULT_MARKET, MARKETS, user_agent
from kalkan.signals.visual import Hashes, image_hashes

# The browser stage's desktop Chrome and market languages, so both stages look the same to a
# site (a kit that cloaks on the Chrome version cannot tell them apart).
USER_AGENT = user_agent(Profile.DESKTOP)
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ACCEPT_LANGUAGE = {country: market.accept_language for country, market in MARKETS.items()}
DEFAULT_LANGUAGE = DEFAULT_MARKET.accept_language
WATCH_AGAIN = timedelta(days=7)
REDIRECT_AGAIN = timedelta(days=1)
EMPTY_TEXT_CHARS = 200
SUSPICIOUS_SCORE = 80
YOUNG_DAYS = 30
MAX_CTA_CANDIDATES = 10
# Every request of one pre-check shares this budget; the actor's time limit is 45 s.
PRECHECK_SECONDS = 30.0
SYSTEM = "system"
_WHITESPACE = re.compile(r"\s+")
# A link is a call to action when its tag says so or its text does (many languages).
_CTA_TAGS = frozenset({"button", "form", "redirect", "frame"})
_CTA_WORDS = re.compile(
    r"giri[s\u015f]|login|log in|sign ?in|signin|register|kay[\u0131i]t|üye ol|"
    r"güncel|guncel|adres|enter|zaloguj|zarejestruj|войти|вход|регистр|anmelden|einloggen|"
    r"verify|do[\u011fg]rula|weryfik|подтверд|claim|refund|cashback|iade|zwrot|возврат|"
    r"deposit|top up|transfer|yat[\u0131i]r|wp[\u0142l]a[\u0107c]|внести|пополн|einzahl",
    re.IGNORECASE,
)

log = get_logger(__name__)


class Route(StrEnum):
    WATCH = "watch"
    CAPTURE = "capture"


class Outcome(StrEnum):
    """``scans.outcome`` of an HTTP pre-check."""

    OK = "ok"
    NO_DNS = "no_dns"
    NO_CONNECTION = "no_connection"
    BLOCKED = "blocked"  # the host resolves to an address the scanner must not reach
    ERROR = "error"


@dataclass
class Facts:
    """What the pre-check learnt; :func:`route` decides from this alone."""

    outcome: Outcome
    http_status: int | None = None
    final_url: str | None = None
    redirect_chain: list[str] = field(default_factory=list)
    parked: bool = False
    empty: bool = False
    challenge: bool = False
    favicon_match: bool = False
    # The candidate favicon's perceptual hashes; the signals stage compares them (§12).
    favicon_hashes: Hashes | None = None
    brand_in_title: bool = False
    brand_in_text: bool = False
    password_field: bool = False
    brand_redirect: str | None = None  # a brand-like, non-official host it redirects to
    redirects_to_official: bool = False
    match_score: int = 0
    domain_age_days: int | None = None


@dataclass(frozen=True)
class Decision:
    route: Route
    reason: str
    next_check: timedelta | None = None


def route(facts: Facts) -> Decision:
    if facts.outcome is Outcome.NO_DNS:
        return Decision(Route.WATCH, "no DNS", WATCH_AGAIN)
    if facts.outcome is not Outcome.OK:
        return Decision(Route.WATCH, f"no connection ({facts.outcome.value})", WATCH_AGAIN)
    if facts.redirects_to_official:
        return Decision(Route.WATCH, "redirects to an official site", REDIRECT_AGAIN)
    # Parking text never hides a login form or the brand's own icon (K71).
    if facts.parked and not (facts.password_field or facts.favicon_match):
        return Decision(Route.WATCH, "parked", WATCH_AGAIN)
    if facts.challenge:
        return Decision(Route.CAPTURE, "bot challenge")
    # A doorway page has almost no text; where it sends visitors decides (K71).
    if facts.empty and facts.brand_redirect is None:
        return Decision(Route.WATCH, "empty page", WATCH_AGAIN)
    signs = [
        name
        for name, present in (
            ("favicon match", facts.favicon_match),
            ("brand in title", facts.brand_in_title),
            ("brand in text", facts.brand_in_text),
            ("password field", facts.password_field),
            ("redirect to a brand-like domain", facts.brand_redirect is not None),
        )
        if present
    ]
    if signs:
        return Decision(Route.CAPTURE, ", ".join(signs))
    young = facts.domain_age_days is not None and facts.domain_age_days <= YOUNG_DAYS
    if facts.match_score >= SUSPICIOUS_SCORE and young:
        return Decision(Route.CAPTURE, "strong match on a young domain (possible cloaking)")
    return Decision(Route.WATCH, "nothing suspicious", WATCH_AGAIN)


# --- reading the page -------------------------------------------------------------------------


def contains_brand(text: str, keywords: Iterable[str]) -> bool:
    """A keyword in ``text`` after the matcher's folding (leetspeak, look-alikes, spaces)."""
    if not text:
        return False
    # Spaces go too: "Mintera Bank" is the brand on a page as much as "mintera" in a host name.
    squeezed = _WHITESPACE.sub("", text)
    plain_text = plain(squeezed)
    folded_text = fold(squeezed)
    for keyword in keywords:
        if len(plain(keyword)) < 4:
            continue
        if plain(keyword) in plain_text:
            return True
        if any(word in variant for word in fold(keyword) for variant in folded_text):
            return True
    return False


def host_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        return (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""


def registered(host: str) -> str:
    """The registered domain of ``host`` (``www.`` and other subdomains dropped)."""
    try:
        return view_host(host).registered_domain
    except InvalidHostnameError:
        return host


def without_hosts(text: str, hosts: Iterable[str]) -> str:
    """``text`` without the site's own host names: error and placeholder pages print them."""
    names: set[str] = set()
    for host in hosts:
        if not host:
            continue
        names |= {host, f"www.{host}"}
        with contextlib.suppress(InvalidHostnameError):
            names.add(view_host(host).unicode_host)
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    return text


def clean_url(url: str | None) -> str | None:
    """An http(s) URL without user information or fragment; a bare host gets ``http://``."""
    if not url:
        return None
    value = url.strip()
    if "://" not in value:
        value = f"http://{value}"
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def is_official(host: str, officials: Iterable[str]) -> bool:
    return bool(host) and any(covers(official, host) for official in officials)


@dataclass(frozen=True)
class CtaTarget:
    url: str
    host: str
    text: str
    kind: str  # official, candidate (brand-like), same_site, other
    tag: str = "a"  # a, button, form, frame, iframe, redirect


def classify_links(
    links: Iterable[LinkInfo], case_host: str, officials: Sequence[str], matcher: Matcher
) -> list[CtaTarget]:
    """Each target host once, in page order; ``case_host``'s own site is ``same_site``."""
    own = registered(case_host)
    targets: dict[str, CtaTarget] = {}
    for link in links:
        host = host_of(link.url)
        if not host:
            continue
        if host in targets:
            known = targets[host]
            if not known.text and link.text:  # keep the most telling label
                targets[host] = CtaTarget(known.url, host, link.text, known.kind, link.tag)
            continue
        if is_official(host, officials):
            kind = "official"
        else:
            found = matcher.match_host(host)
            if found is None:
                kind = "other"
            elif found[0].registered_domain == own:
                kind = "same_site"
            elif found[1]:
                kind = "candidate"
            else:
                kind = "other"
        targets[host] = CtaTarget(link.url, host, link.text, kind, link.tag)
    return list(targets.values())


def is_call_to_action(target: CtaTarget, keywords: Iterable[str]) -> bool:
    return (
        target.tag in _CTA_TAGS
        or bool(_CTA_WORDS.search(target.text))
        or contains_brand(target.text, keywords)
    )


# --- fetching ---------------------------------------------------------------------------------


def request_headers(markets: Sequence[str]) -> dict[str, str]:
    language = ACCEPT_LANGUAGE.get(markets[0].upper(), DEFAULT_LANGUAGE) if markets else None
    return {
        "User-Agent": USER_AGENT,
        "Accept": ACCEPT,
        "Accept-Language": language or DEFAULT_LANGUAGE,
    }


def entry_urls(host: str, manual_url: str | None) -> list[str]:
    """The case's own URL (when it is on this site), then https and http; then ``www.``.

    Discovery drops ``www.``, so a site that only answers there is tried last (K71).
    """
    urls: list[str] = []
    manual = clean_url(manual_url)
    if manual and host_of(manual) in (host, f"www.{host}"):
        urls.append(manual)
    hosts = [host]
    if registered(host) == host:
        hosts.append(f"www.{host}")
    for name in hosts:
        for url in (f"https://{name}/", f"http://{name}/"):
            if url not in urls:
                urls.append(url)
    return urls


@dataclass
class Fetched:
    response: Response | None
    outcome: Outcome
    errors: list[str] = field(default_factory=list)


# The worst outcome wins when no URL answered.
_OUTCOME_ORDER = (Outcome.BLOCKED, Outcome.ERROR, Outcome.NO_CONNECTION, Outcome.NO_DNS)


def fetch_first(
    client: SafeHttpClient,
    urls: Sequence[str],
    headers: dict[str, str],
    deadline: float | None = None,
) -> Fetched:
    """The first URL that answers at all; a host without DNS or with a blocked address is
    not tried again."""
    errors: list[str] = []
    seen: set[Outcome] = set()
    dead: set[str] = set()
    for url in urls:
        host = host_of(url)
        if host in dead:
            continue
        try:
            return Fetched(client.get(url, headers, deadline=deadline), Outcome.OK, errors)
        except NoSuchHostError as exc:
            seen.add(Outcome.NO_DNS)
            dead.add(host)
            errors.append(str(exc))
        except SsrfBlockedError as exc:
            seen.add(Outcome.BLOCKED)
            dead.add(host)
            errors.append(str(exc))
        except ConnectionFailedError as exc:
            seen.add(Outcome.NO_CONNECTION)
            errors.append(str(exc))
        except FetchError as exc:
            seen.add(Outcome.ERROR)
            errors.append(str(exc))
    outcome = next((item for item in _OUTCOME_ORDER if item in seen), Outcome.NO_CONNECTION)
    return Fetched(None, outcome, errors)


def fetch_favicon(
    client: SafeHttpClient,
    page: PageInfo,
    final_url: str,
    headers: dict[str, str],
    deadline: float | None = None,
) -> bytes | None:
    """The page's favicon file, or None."""
    parts = urlsplit(final_url)
    candidates = [*page.icons[:2], urlunsplit((parts.scheme, parts.netloc, "/favicon.ico", "", ""))]
    for url in candidates:
        try:
            response = client.get(url, headers, deadline=deadline)
        except FetchError:
            continue
        # An HTML page served as the icon (an SPA fallback, a soft 404) is no favicon.
        small = len(response.body) <= MAX_FAVICON_BYTES
        if response.status == 200 and small and image_type(response.body) is not None:
            return response.body
    return None


# --- the job ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Context:
    case_id: uuid.UUID
    number: str
    customer_id: uuid.UUID
    brand: Brand
    host: str
    nameservers: list[str]
    officials: list[str]
    favicons: set[int]
    match_score: int
    domain_age_days: int | None
    manual_url: str | None


@dataclass(frozen=True)
class PrecheckResult:
    case_id: uuid.UUID
    skipped: bool
    decision: Decision | None = None
    scan_id: uuid.UUID | None = None
    new_match_ids: list[uuid.UUID] = field(default_factory=list)


def already_prechecked(session: Session, case: Case) -> bool:
    """An HTTP scan exists since the case last entered ``detected`` (K71, K74)."""
    entered = session.scalar(
        select(func.max(CaseEvent.at)).where(
            CaseEvent.case_id == case.id, CaseEvent.type == EventType.DETECTED
        )
    )
    return bool(
        session.scalar(
            select(
                exists().where(
                    Scan.case_id == case.id,
                    Scan.stage == ScanStage.HTTP,
                    Scan.started_at >= (entered or case.created_at),
                )
            )
        )
    )


def _context(session: Session, case_id: uuid.UUID, now: datetime) -> Context | None:
    case = session.get(Case, case_id)
    if case is None or case.state is not CaseState.DETECTED:
        return None
    if already_prechecked(session, case):
        return None
    customer = session.get(Customer, case.customer_id)
    domain = session.get(Domain, case.domain_id)
    brand = session.get(Brand, case.brand_id)
    if customer is None or customer.is_demo or domain is None or brand is None:
        return None
    officials = sorted(
        set(
            session.scalars(
                select(OfficialDomain.domain).where(OfficialDomain.customer_id == case.customer_id)
            )
        )
    )
    favicons = {
        value
        for value in session.scalars(
            select(BrandAsset.favicon_mmh3).where(
                BrandAsset.brand_id == brand.id, BrandAsset.kind == BrandAssetKind.FAVICON
            )
        )
        if value is not None
    }
    score = session.scalar(
        select(BrandMatch.score).where(
            BrandMatch.brand_id == brand.id, BrandMatch.domain_id == domain.id
        )
    )
    manual = session.scalar(
        select(DomainSighting.detail)
        .where(
            DomainSighting.domain_id == domain.id,
            DomainSighting.source == SightingSource.MANUAL,
        )
        .order_by(DomainSighting.seen_at.desc())
        .limit(1)
    )
    age = (now - domain.registered_at).days if domain.registered_at is not None else None
    session.expunge(brand)
    return Context(
        case_id=case.id,
        number=case.number,
        customer_id=case.customer_id,
        brand=brand,
        host=domain.name,
        nameservers=list(domain.nameservers),
        officials=officials,
        favicons=favicons,
        match_score=score or 0,
        domain_age_days=age,
        manual_url=(manual or {}).get("url"),
    )


def _signal(
    session: Session, scan: Scan, name: str, value: Any, detail: dict[str, Any] | None = None
) -> None:
    session.add(
        Signal(
            customer_id=scan.customer_id,
            case_id=scan.case_id,
            scan_id=scan.id,
            name=name,
            value=value,
            detail=detail or {},
        )
    )


def run_precheck(
    database: Database,
    client: SafeHttpClient,
    case_id: uuid.UUID,
    *,
    now: Callable[[], datetime],
) -> PrecheckResult:
    started = now()
    with database.worker_session() as session:
        context = _context(session, case_id, started)
        matcher = Matcher([profile_for(session, context.brand)]) if context else None
    if context is None or matcher is None:
        return PrecheckResult(case_id, skipped=True)

    deadline = client.deadline(PRECHECK_SECONDS)
    headers = request_headers(context.brand.target_markets)
    fetched = fetch_first(client, entry_urls(context.host, context.manual_url), headers, deadline)
    facts = Facts(
        outcome=fetched.outcome,
        match_score=context.match_score,
        domain_age_days=context.domain_age_days,
        parked=patterns.parked_by_nameserver(context.nameservers),
    )
    page = PageInfo()
    links: list[CtaTarget] = []
    response = fetched.response
    if response is not None:
        raw = decode(response.body, response.headers.get("content-type"))
        page = parse_page(raw, response.url)
        final_host = host_of(response.url)
        keywords = context.brand.keywords
        own = registered(context.host)
        facts.http_status = response.status
        facts.final_url = response.url
        facts.redirect_chain = response.redirect_chain
        facts.redirects_to_official = is_official(final_host, context.officials)
        facts.parked = facts.parked or patterns.parked_by_text(page.text, page.title or "")
        facts.password_field = page.password_fields > 0
        facts.challenge = patterns.challenge(raw, response.headers) or patterns.captcha_wall(
            raw, page.text, page.password_fields
        )
        facts.empty = len(page.text) < EMPTY_TEXT_CHARS and not any(f.inputs for f in page.forms)
        # The site's own name on an error or placeholder page is not the brand on the page.
        own_names = (context.host, final_host)
        facts.brand_in_title = contains_brand(without_hosts(page.title or "", own_names), keywords)
        facts.brand_in_text = contains_brand(without_hosts(page.text, own_names), keywords)
        hints = [page.meta_refresh, *page.js_redirects, *page.frames]
        if facts.empty:
            hints += page.iframes  # a page that is only an iframe shows another site
        hinted = [url for url in hints if url]
        if facts.empty and any(is_official(host_of(url), context.officials) for url in hinted):
            facts.redirects_to_official = True
        chain_hosts = [host_of(url) for url in [*response.redirect_chain, response.url]]
        for host in [*chain_hosts, *(host_of(url) for url in hinted)]:
            if not host or registered(host) == own or is_official(host, context.officials):
                continue
            found = matcher.match_host(host)
            if found is not None and found[1]:
                facts.brand_redirect = host
                break
        if context.favicons and not facts.redirects_to_official:
            icon = fetch_favicon(client, page, response.url, headers, deadline)
            if icon is not None:
                facts.favicon_match = favicon_hash(icon) in context.favicons
                facts.favicon_hashes = image_hashes(icon)
        hops = [*response.redirect_chain[1:], response.url] if response.redirect_chain else []
        redirects = [LinkInfo(url, "", "redirect") for url in hops]
        redirects += [LinkInfo(url, "", "redirect") for url in hinted]
        links = classify_links([*redirects, *page.links], context.host, context.officials, matcher)

    decision = route(facts)
    promoted = _promoted(facts, links, context.brand.keywords)
    finished = now()
    with database.worker_session() as session:
        case = session.get(Case, case_id, with_for_update=True)
        if case is None or case.state is not CaseState.DETECTED:
            return PrecheckResult(case_id, skipped=True)
        if already_prechecked(session, case):  # another run got here first
            return PrecheckResult(case_id, skipped=True)
        scan = Scan(
            customer_id=case.customer_id,
            case_id=case.id,
            stage=ScanStage.HTTP,
            started_at=started,
            finished_at=finished,
            outcome=facts.outcome.value,
            http_status=facts.http_status,
            final_url=facts.final_url,
            redirect_chain=facts.redirect_chain,
            title=page.title,
            text_excerpt=page.text or None,
            forms=page.forms_json(),
            error="; ".join(fetched.errors)[:2000] or None,
        )
        session.add(scan)
        session.flush()
        _write_signals(session, scan, facts, page, links, promoted)
        session.add(
            CaseEvent(
                customer_id=case.customer_id,
                case_id=case.id,
                at=finished,
                type=EventType.PRECHECKED,
                actor=SYSTEM,
                data={
                    "outcome": facts.outcome.value,
                    "route": decision.route.value,
                    "reason": decision.reason,
                    "http_status": facts.http_status,
                    "final_url": facts.final_url,
                },
            )
        )
        if decision.route is Route.WATCH and decision.next_check is not None:
            transition(
                session,
                case,
                CaseState.WATCHING,
                actor=SYSTEM,
                reason=decision.reason,
                at=finished,
                last_action="Monitoring",
            )
            case.next_check_at = finished + decision.next_check
        new_ids = record_cta_candidates(session, context.brand, context.number, promoted, finished)
        scan_id = scan.id
    log.info(
        "precheck.done",
        case_id=str(case_id),
        outcome=facts.outcome.value,
        route=decision.route.value,
        reason=decision.reason,
    )
    return PrecheckResult(case_id, False, decision, scan_id, new_ids)


def record_failure(database: Database, case_id: uuid.UUID, error: str, *, at: datetime) -> None:
    """The last attempt failed: ``job_failed`` stops the reconciler; ops sees it (§7)."""
    with database.worker_session() as session:
        case = session.get(Case, case_id, with_for_update=True)
        if case is None or case.state is not CaseState.DETECTED:
            return
        session.add(
            CaseEvent(
                customer_id=case.customer_id,
                case_id=case.id,
                at=at,
                type=EventType.JOB_FAILED,
                actor=SYSTEM,
                data={"job": "http_precheck", "error": error[:500]},
            )
        )


def promoted_targets(
    links: Sequence[CtaTarget], keywords: Sequence[str], *, lure_page: bool
) -> list[CtaTarget]:
    """The targets that become candidates (K57, K71).

    Brand-like targets always do. On a page that presents itself as the brand (its name or
    its favicon) and is not the official site (``lure_page``), so do the other targets of its
    buttons, forms, redirects and call-to-action links: that is where a fake login page sends
    its visitors. The browser stage uses this too (``scanning/persist.py``).
    """
    chosen = [
        link
        for link in links
        if link.kind == "candidate"
        or (link.kind == "other" and lure_page and is_call_to_action(link, keywords))
    ]
    return chosen[:MAX_CTA_CANDIDATES]


def _promoted(facts: Facts, links: list[CtaTarget], keywords: Sequence[str]) -> list[CtaTarget]:
    lure_page = (
        facts.brand_in_title or facts.brand_in_text or facts.favicon_match
    ) and not facts.redirects_to_official
    return promoted_targets(links, keywords, lure_page=lure_page)


def cta_summary(
    links: Sequence[CtaTarget], promoted: Sequence[CtaTarget]
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """The ``cta_targets`` signal: counts per kind, and the targets that are not the site's own."""
    counts: dict[str, int] = {}
    for link in links:
        counts[link.kind] = counts.get(link.kind, 0) + 1
    chosen = {link.host for link in promoted}
    targets = [
        {
            "host": link.host,
            "kind": link.kind,
            "tag": link.tag,
            "text": link.text,
            "candidate": link.host in chosen,
        }
        for link in links
        if link.kind != "same_site"
    ][:50]
    return counts, targets


def _write_signals(
    session: Session,
    scan: Scan,
    facts: Facts,
    page: PageInfo,
    links: list[CtaTarget],
    promoted: list[CtaTarget],
) -> None:
    _signal(session, scan, "http_status", facts.http_status, {"outcome": facts.outcome.value})
    if facts.outcome is not Outcome.OK:
        return
    for name, value in (
        ("parked", facts.parked),
        ("empty", facts.empty),
        ("challenge", facts.challenge),
        ("brand_in_title", facts.brand_in_title),
        ("brand_in_text", facts.brand_in_text),
        ("password_field_html", facts.password_field),
        ("redirects_to_official", facts.redirects_to_official),
    ):
        _signal(session, scan, name, value)
    hashes = facts.favicon_hashes
    _signal(
        session,
        scan,
        "favicon_match",
        facts.favicon_match,
        {"phash": hashes.phash, "dhash": hashes.dhash} if hashes is not None else {},
    )
    _signal(
        session,
        scan,
        "brand_redirect",
        facts.brand_redirect is not None,
        {"host": facts.brand_redirect} if facts.brand_redirect else {},
    )
    counts, targets = cta_summary(links, promoted)
    _signal(
        session,
        scan,
        "cta_targets",
        counts,
        {
            "targets": targets,
            "meta_refresh": page.meta_refresh,
            "js_redirects": page.js_redirects[:10],
            "frames": page.frames[:10],
        },
    )


def record_cta_candidates(
    session: Session,
    brand: Brand,
    case_number: str,
    promoted: Sequence[CtaTarget],
    at: datetime,
) -> list[uuid.UUID]:
    """Promoted targets become manual candidates of the case's brand, as an operator's would
    (the 03 path: score 100, the matcher's reasons next to ``manual``). Queue ``admit_match``
    for the returned ids after the transaction commits."""
    matcher = Matcher([profile_for(session, brand)])
    new_ids: list[uuid.UUID] = []
    for link in promoted:
        found = matcher.match_host(link.host)
        if found is None:
            continue
        view, matches = found
        reasons = (MatchReason.MANUAL, *(matches[0].reasons if matches else ()))
        recorded = record_discovery(
            session,
            view,
            [MatchResult(brand.id, brand.customer_id, MANUAL_SCORE, reasons)],
            Sighting(
                SightingSource.MANUAL,
                at,
                {
                    "via": "cta",
                    "from_case": case_number,
                    "url": clean_url(link.url),
                    "text": link.text,
                },
            ),
        )
        new_ids += recorded.new_match_ids
    return new_ids
