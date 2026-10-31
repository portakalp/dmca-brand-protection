"""The three-layer classification prompt (architecture.md §13).

1. System prompt: ``prompts/system.md``, the same for every call.
2. Brand block: ``prompts/brand.md.j2`` and the brand's official images, each after a text
   label. The block ends with a 1-hour ``cache_control`` breakpoint, so the system prompt and
   the brand block are read from the cache by the brand's next cases.
3. Candidate block: the screenshots first ("Image 1: candidate desktop", "Image 2: candidate
   mobile"), then ``prompts/candidate.md.j2``: domain, match, code signals and the form
   inventory as our code classified it, the RDAP and hosting data in ``<untrusted_domain_data>``
   and the page's title, URLs, buttons, field labels and text (at most :data:`MAX_TEXT`
   characters in all) in ``<untrusted_page_content>``.

Everything that came from the candidate site or from registration records passes through
:func:`untrusted`, which removes angle brackets so the content cannot close its block.
``PROMPT_VERSION`` is a hash of the prompt files and :data:`LAYOUT`; it is stored with every
verdict and AI call.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import JsonValue

from kalkan.ai.images import PreparedImage
from kalkan.domain.enums import Profile, ScanStage
from kalkan.scanning.capture_contract import Outcome
from kalkan.scanning.precheck import host_of
from kalkan.signals.forms import classify_button, classify_input
from kalkan.signals.snapshot import BrandInfo, CaseSnapshot, ScanInfo

PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPT_FILES = ("system.md", "brand.md.j2", "candidate.md.j2")
# Bump when this module changes what goes into a block or in which order.
LAYOUT = "1"
MAX_TEXT = 4000  # page text in the candidate block, all pages together
MIN_TEXT = 200  # a second page's text is left out when less room than this is left
PAGE_SEPARATOR = "\n---\n"
MAX_TITLE = 200
MAX_URL = 300
MAX_ITEMS = 15  # buttons, field labels, redirects per page
MAX_WHITELIST = 50
CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}
CANDIDATE_IMAGES = (
    (Profile.DESKTOP, "Image 1: candidate desktop"),
    (Profile.MOBILE, "Image 2: candidate mobile"),
)
HOST = re.compile(r"^[a-z0-9.-]{1,253}$")
# Angle brackets become their quotation-mark look-alikes: no tag can be opened or closed.
ANGLES = str.maketrans({"<": "\u2039", ">": "\u203a"})
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_env = Environment(
    loader=FileSystemLoader(PROMPTS_DIR),
    autoescape=False,  # noqa: S701 - plain text for a model, not HTML; untrusted() escapes
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=False,
)


def _prompt_version() -> str:
    digest = hashlib.sha256(f"layout:{LAYOUT}\n".encode())
    for name in PROMPT_FILES:
        digest.update(f"{name}\n".encode())
        digest.update((PROMPTS_DIR / name).read_bytes())
    return digest.hexdigest()[:12]


PROMPT_VERSION = _prompt_version()
SYSTEM_PROMPT = (PROMPTS_DIR / "system.md").read_text(encoding="utf-8").strip()


def untrusted(value: object, limit: int | None = None) -> str:
    """Untrusted text made safe for its labelled block: no angle brackets, no control
    characters, at most ``limit`` characters."""
    text = CONTROL.sub(" ", str(value)).translate(ANGLES)
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return text


@dataclass(frozen=True)
class BrandImage:
    """An official image: a Files API id in production, inline bytes in the evaluation."""

    label: str
    file_id: str | None = None
    image: PreparedImage | None = None

    def block(self) -> dict[str, Any]:
        if self.file_id is not None:
            return {"type": "image", "source": {"type": "file", "file_id": self.file_id}}
        if self.image is None:
            raise ValueError("a brand image needs a file id or its bytes")
        return image_block(self.image)


@dataclass(frozen=True)
class Prompt:
    system: str
    content: list[dict[str, Any]]
    version: str = PROMPT_VERSION
    images: int = 0  # images in the content

    def text(self) -> str:
        """The text blocks joined, images shown by a placeholder (tests and reports)."""
        parts = [self.system]
        for block in self.content:
            parts.append(block["text"] if block["type"] == "text" else "[image]")
        return "\n\n".join(parts)


@dataclass
class _Page:
    label: str
    title: str
    final_url: str
    redirects: list[str] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)


def image_block(image: PreparedImage) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image.media_type,
            "data": base64.b64encode(image.data).decode("ascii"),
        },
    }


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


# --- brand block ------------------------------------------------------------------------------


def brand_blocks(
    brand: BrandInfo, whitelist: Sequence[str], images: Sequence[BrandImage]
) -> list[dict[str, Any]]:
    text = _env.get_template("brand.md.j2").render(
        brand=brand,
        whitelist=list(whitelist)[:MAX_WHITELIST],
        images=[image.label for image in images],
    )
    blocks = [_text(text)]
    for image in images:
        blocks.append(_text(image.label))
        blocks.append(image.block())
    blocks[-1] = {**blocks[-1], "cache_control": CACHE_CONTROL}
    return blocks


# --- candidate block --------------------------------------------------------------------------


def _value(value: JsonValue) -> str | None:
    if value is None:
        return "not computed"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return None  # lists and objects (e.g. CTA targets) hold untrusted URLs


def _signal_lines(signals: Mapping[str, Any]) -> list[tuple[str, str]]:
    lines = []
    for name in sorted(signals):
        shown = _value(signals[name].value)
        if shown is not None:
            lines.append((name, shown))
    return lines


def _browser_scans(snapshot: CaseSnapshot) -> list[ScanInfo]:
    order = {Profile.DESKTOP: 0, Profile.MOBILE: 1}
    scans = [scan for scan in snapshot.scans if scan.stage is ScanStage.BROWSER and scan.profile]
    return sorted(scans, key=lambda scan: order[scan.profile] if scan.profile else 9)


def _scan_label(scan: ScanInfo) -> str:
    if scan.stage is ScanStage.HTTP:
        return "HTTP pre-check"
    country = f", from {scan.proxy_country}" if scan.proxy_country else ""
    outcome = "" if scan.outcome == Outcome.OK.value else f", outcome {scan.outcome or 'unknown'}"
    return f"{scan.profile.value if scan.profile else 'browser'} capture{country}{outcome}"


def _form_lines(scan: ScanInfo) -> list[str]:
    page_host = host_of(scan.final_url)
    lines = []
    for number, form in enumerate(scan.forms, start=1):
        kinds: dict[str, int] = {}
        for item in form.inputs:
            kind = classify_input(item) or f"other {item.type or 'text'}"
            if not item.visible:
                kind += " (hidden)"
            kinds[kind] = kinds.get(kind, 0) + 1
        inputs = ", ".join(
            f"{kind} x{count}" if count > 1 else kind for kind, count in kinds.items()
        )
        action_host = host_of(form.action)
        if not form.action:
            target = "no action"
        elif action_host == page_host:
            target = "posts to the same host"
        elif HOST.match(action_host):
            target = f"posts to another host ({action_host})"
        else:
            target = "posts to another host"
        frame = "; inside a frame" if form.frame_url else ""
        lines.append(
            f"form {number}: {form.method.upper()}, {target}{frame}; inputs: {inputs or 'none'}"
        )
    intents: dict[str, int] = {}
    for button in scan.buttons:
        intent = classify_button(button)
        if intent:
            intents[intent] = intents.get(intent, 0) + 1
    if intents:
        lines.append(
            "buttons by intent: " + ", ".join(f"{name} x{count}" for name, count in intents.items())
        )
    return lines


def _page(scan: ScanInfo) -> _Page:
    page = _Page(
        label=_scan_label(scan),
        title=untrusted(scan.title or "(none)", MAX_TITLE),
        final_url=untrusted(scan.final_url or "(none)", MAX_URL),
        redirects=[untrusted(url, MAX_URL) for url in scan.redirect_chain[:MAX_ITEMS]],
    )
    for button in scan.buttons[:MAX_ITEMS]:
        target = f" -> {untrusted(button.target, MAX_URL)}" if button.target else ""
        page.buttons.append(f'"{untrusted(button.text, 80)}"{target}')
    for form in scan.forms:
        for item in form.inputs:
            label = item.label or item.placeholder or item.name
            if label and len(page.fields) < MAX_ITEMS:
                page.fields.append(f'"{untrusted(label, 80)}"')
    return page


def _page_text(scans: Iterable[ScanInfo]) -> str:
    """The pages' texts, each once, together at most :data:`MAX_TEXT` characters."""
    parts: list[str] = []
    seen: set[str] = set()
    left = MAX_TEXT
    for scan in scans:
        text = scan.text_excerpt.strip()
        if parts:
            left -= len(PAGE_SEPARATOR)
        if not text or text in seen or left < MIN_TEXT:
            continue
        seen.add(text)
        cut = untrusted(text, left)
        parts.append(cut)
        left -= len(cut)
    return PAGE_SEPARATOR.join(parts)


def _registration(snapshot: CaseSnapshot) -> list[tuple[str, str]]:
    domain = snapshot.domain

    def show(value: object) -> str:
        return untrusted(value, MAX_URL) if value not in (None, "", []) else "unknown"

    rows = [
        ("Registrar", show(domain.registrar)),
        (
            "Registered",
            show(domain.registered_at.date().isoformat() if domain.registered_at else None),
        ),
        ("Domain age at detection (days)", show(domain.age_days)),
        ("Name servers", show(", ".join(domain.nameservers[:6]))),
        ("IP addresses", show(", ".join(domain.ips[:6]))),
        ("Behind Cloudflare", "yes" if domain.is_cloudflare else "no"),
        ("Hosting", show(" ".join(filter(None, [domain.hosting_org, domain.hosting_country])))),
        ("ASN", show(domain.asn)),
    ]
    if domain.origin_ip or domain.origin_org:
        origin = " ".join(
            filter(None, [domain.origin_ip, domain.origin_org, domain.origin_country])
        )
        rows.append(("Origin behind the proxy", show(origin)))
    if domain.webrisk_threat_types is not None:
        rows.append(
            ("Google Web Risk", show(", ".join(domain.webrisk_threat_types) or "not listed"))
        )
    return rows


def candidate_blocks(
    snapshot: CaseSnapshot, images: Mapping[Profile, PreparedImage | None]
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    shown: list[str] = []
    for profile, label in CANDIDATE_IMAGES:
        image = images.get(profile)
        if image is None:
            shown.append(f"{label}: not available")
            continue
        blocks.append(_text(label))
        blocks.append(image_block(image))
        shown.append(f"{label}: shown above")
    browser = _browser_scans(snapshot)
    http = [scan for scan in snapshot.scans if scan.stage is ScanStage.HTTP]
    page_scans = browser or http
    text = _env.get_template("candidate.md.j2").render(
        number=snapshot.case.number,
        domain=snapshot.domain,
        first_seen=_day(snapshot.case.first_seen_at),
        match=snapshot.match,
        images=shown,
        signals=_signal_lines(snapshot.signals),
        scan_signals=[
            {"label": f"{_scan_label(scan)} signals", "signals": _signal_lines(scan.signals)}
            for scan in browser
            if _signal_lines(scan.signals)
        ],
        forms=[{"label": _scan_label(scan), "lines": _form_lines(scan)} for scan in browser],
        registration=_registration(snapshot),
        pages=[_page(scan) for scan in page_scans],
        text=_page_text(page_scans),
    )
    blocks.append(_text(text))
    return blocks


def _day(value: datetime) -> str:
    return value.date().isoformat()


def build_prompt(
    snapshot: CaseSnapshot,
    *,
    whitelist: Sequence[str],
    brand_images: Sequence[BrandImage],
    candidate_images: Mapping[Profile, PreparedImage | None],
) -> Prompt:
    content = brand_blocks(snapshot.brand, whitelist, brand_images)
    content += candidate_blocks(snapshot, candidate_images)
    count = sum(1 for block in content if block["type"] == "image")
    return Prompt(system=SYSTEM_PROMPT, content=content, images=count)
