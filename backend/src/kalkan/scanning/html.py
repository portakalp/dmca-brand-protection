"""What the HTTP pre-check reads from raw HTML (architecture.md §10). Pure; no network.

Python's own ``html.parser``: no dependency, tolerant of broken markup, and it never runs
anything. The page is untrusted; nothing here follows or executes it, every list is capped,
and the work stays linear in the size of the page.
"""

from __future__ import annotations

import codecs
import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

MAX_TEXT = 20_000
MAX_LINKS = 200
MAX_FORMS = 50
MAX_INPUTS = 200
MAX_DEPTH = 1_000
_SKIPPED_TEXT = frozenset({"script", "style", "noscript", "template", "head", "title", "svg"})
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
# What may stand in <head>; anything else (or <body>) closes it, as in a browser.
_HEAD_CONTENT = frozenset(
    {"base", "link", "meta", "noscript", "script", "style", "template", "title", "head"}
)
_SPACE = re.compile(r"\s+")
_JS_REDIRECT = re.compile(
    r"""(?:window\.|document\.|top\.|self\.)?location(?:\.href)?\s*=\s*["']([^"']{1,500})["']"""
    r"""|location\.(?:replace|assign)\(\s*["']([^"']{1,500})["']""",
    re.IGNORECASE,
)
_WINDOW_OPEN = re.compile(r"""window\.open\(\s*["']([^"']{1,500})["']""", re.IGNORECASE)
# <meta http-equiv=refresh content="5; url=https://x">, the "url=" being optional (WHATWG).
_REFRESH = re.compile(
    r"""^\s*[\d.]*\s*[;,]?\s*(?:url\s*=\s*)?(['"]?)(.+?)\1?\s*$""", re.IGNORECASE | re.DOTALL
)
_URL_NOISE = re.compile(r"[\t\n\r]")
# Python codecs that are no text encoding a browser knows; a page may name any of them.
_NOT_PAGE_ENCODINGS = frozenset(
    {"idna", "punycode", "undefined", "raw_unicode_escape", "unicode_escape", "utf_7"}
)
_BOMS = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


@dataclass
class FormInfo:
    action: str | None
    method: str
    inputs: list[dict[str, str]] = field(default_factory=list)


@dataclass
class LinkInfo:
    url: str  # absolute
    text: str
    tag: str  # a, button, form


@dataclass
class PageInfo:
    title: str | None = None
    text: str = ""
    meta_refresh: str | None = None
    js_redirects: list[str] = field(default_factory=list)
    frames: list[str] = field(default_factory=list)  # <frame src>: the page wraps another
    iframes: list[str] = field(default_factory=list)
    icons: list[str] = field(default_factory=list)
    forms: list[FormInfo] = field(default_factory=list)
    links: list[LinkInfo] = field(default_factory=list)
    password_fields: int = 0  # counted in full, even past the stored inventory's cap

    def forms_json(self) -> list[dict[str, object]]:
        return [
            {"action": form.action, "method": form.method, "inputs": form.inputs}
            for form in self.forms
        ]


def _absolute(base: str, href: str | None) -> str | None:
    """``href`` as an absolute http(s) URL, read the way a browser would; None otherwise."""
    if not href:
        return None
    href = _URL_NOISE.sub("", href.strip(" \t\n\r\x0c\x00"))[:2000]
    if not href or href.lower().startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    try:
        scheme = urlsplit(href).scheme.lower()
        if scheme in ("", "http", "https"):
            href = href.replace("\\", "/")  # browsers read "\\" as "/" in web URLs
        url = urljoin(base, href)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
    except ValueError:  # e.g. "http://[evil/"
        return None
    return url


class _Parser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.page = PageInfo()
        self._stack: list[str] = []
        self._open: Counter[str] = Counter()  # tags on the stack, for O(1) membership
        self._skipping = 0  # skipped-text tags on the stack
        self._text: list[str] = []
        self._text_size = 0
        self._title: list[str] | None = None
        self._scripts: list[str] = []
        self._script_size = 0
        self._in_script = False
        self._based = False
        self._form: FormInfo | None = None
        self._inputs = 0
        self._label: tuple[LinkInfo, list[str]] | None = None
        self._loose_inputs = FormInfo(action=None, method="get")

    def _push(self, tag: str) -> None:
        if len(self._stack) >= MAX_DEPTH:
            return
        self._stack.append(tag)
        self._open[tag] += 1
        if tag in _SKIPPED_TEXT:
            self._skipping += 1

    def _pop_through(self, tag: str) -> None:
        """Pop the stack down to and including the innermost ``tag``."""
        while self._stack:
            top = self._stack.pop()
            self._open[top] -= 1
            if top in _SKIPPED_TEXT:
                self._skipping -= 1
            if top == tag:
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): (value or "") for name, value in attrs}
        if self._open["head"] and tag not in _HEAD_CONTENT:
            self._pop_through("head")  # <body> or body content closes an open <head>
        if tag == "base" and attributes.get("href") and not self._based:
            self._based = True  # browsers use the first <base> only
            self.base = _absolute(self.base, attributes["href"]) or self.base
        if tag not in _VOID:
            self._push(tag)
        if tag == "title" and self._title is None:
            self._title = []
        elif tag == "script":
            self._in_script = True
        elif tag == "meta" and attributes.get("http-equiv", "").lower() == "refresh":
            found = _REFRESH.match(attributes.get("content", "")[:2000])
            if found and self.page.meta_refresh is None:
                self.page.meta_refresh = _absolute(self.base, found.group(2))
        elif tag == "link" and "icon" in attributes.get("rel", "").lower().split():
            icon = _absolute(self.base, attributes.get("href"))
            if icon and icon not in self.page.icons:
                self.page.icons.append(icon)
        elif tag in ("frame", "iframe"):
            source = _absolute(self.base, attributes.get("src"))
            sources = self.page.frames if tag == "frame" else self.page.iframes
            if source and source not in sources and len(sources) < 10:
                sources.append(source)
                self._add_link(LinkInfo(source, "", tag))
        elif tag == "form":
            action = _absolute(self.base, attributes.get("action"))
            self._form = FormInfo(
                action=action, method=attributes.get("method", "get").lower()[:10] or "get"
            )
            if len(self.page.forms) < MAX_FORMS:
                self.page.forms.append(self._form)
            if action:
                self._add_link(LinkInfo(action, "", "form"))
        elif tag in ("input", "select", "textarea"):
            kind = attributes.get("type", "text" if tag == "input" else tag).lower()[:30]
            if kind == "password":
                self.page.password_fields += 1
            stored = self._form is None or (self.page.forms and self.page.forms[-1] is self._form)
            if stored and self._inputs < MAX_INPUTS:
                self._inputs += 1
                item = {
                    key: attributes[key][:100]
                    for key in ("name", "placeholder", "autocomplete", "id")
                    if attributes.get(key)
                }
                item["type"] = kind
                (self._form or self._loose_inputs).inputs.append(item)
        elif tag in ("a", "button"):
            self._flush_label()  # an unclosed <a> before this one
            target = _absolute(self.base, attributes.get("href") or attributes.get("formaction"))
            if target is None:
                target = _onclick_target(self.base, attributes.get("onclick", ""))
            if target is not None:
                link = LinkInfo(target, "", tag)
                self._label = (link, [])

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._title is not None and self.page.title is None:
            self.page.title = _SPACE.sub(" ", "".join(self._title)).strip()[:500] or None
        if tag == "script":
            self._in_script = False
        if tag == "form":
            self._form = None
        if tag in ("a", "button"):
            self._flush_label()
        if self._open[tag]:
            self._pop_through(tag)

    def handle_data(self, data: str) -> None:
        if self._in_script:
            if self._script_size < MAX_TEXT * 10:
                self._scripts.append(data)
                self._script_size += len(data)
            return
        if self._title is not None and self.page.title is None and self._open["title"]:
            self._title.append(data[:500])
        if self._label is not None and len(self._label[1]) < 50:
            self._label[1].append(data[:200])
        if not self._skipping and self._text_size < MAX_TEXT * 2:
            self._text.append(data)
            self._text_size += len(data)

    def _flush_label(self) -> None:
        if self._label is not None:
            link, words = self._label
            link.text = _SPACE.sub(" ", " ".join(words)).strip()[:200]
            self._add_link(link)
            self._label = None

    def _add_link(self, link: LinkInfo) -> None:
        if len(self.page.links) < MAX_LINKS:
            self.page.links.append(link)

    def finish(self) -> PageInfo:
        self.close()
        self._flush_label()
        self.page.text = _SPACE.sub(" ", " ".join(self._text)).strip()[:MAX_TEXT]
        for match in _JS_REDIRECT.finditer("\n".join(self._scripts)):
            target = _absolute(self.base, match.group(1) or match.group(2))
            if target and target not in self.page.js_redirects:
                self.page.js_redirects.append(target)
        if self._loose_inputs.inputs and len(self.page.forms) < MAX_FORMS:
            self.page.forms.append(self._loose_inputs)
        return self.page


def _onclick_target(base: str, onclick: str) -> str | None:
    onclick = onclick[:2000]
    found = _JS_REDIRECT.search(onclick) or _WINDOW_OPEN.search(onclick)
    if not found:
        return None
    return _absolute(base, next(group for group in found.groups() if group))


def parse_page(html: str, base_url: str) -> PageInfo:
    parser = _Parser(base_url)
    parser.feed(html)
    return parser.finish()


def _text_encoding(label: str | None, *, from_meta: bool) -> str | None:
    """A codec for a page's charset label, or None when no browser would use it."""
    if not label:
        return None
    try:
        info = codecs.lookup(label.strip()[:40])
    except (LookupError, ValueError):
        return None
    name = info.name.replace("-", "_")
    if name in _NOT_PAGE_ENCODINGS or not getattr(info, "_is_text_encoding", True):
        return None
    if from_meta and name.startswith(("utf_16", "utf_32")):
        return "utf-8"  # an ASCII <meta> cannot be UTF-16; browsers read such pages as UTF-8
    if name.startswith("utf_32"):
        return None  # browsers do not support UTF-32
    return info.name


def decode(body: bytes, content_type: str | None) -> str:
    """The body as text: BOM, then the declared charset, then UTF-8. NUL characters go."""
    encoding = next((name for bom, name in _BOMS if body.startswith(bom)), None)
    if encoding is None and content_type and "charset=" in content_type.lower():
        label = content_type.lower().split("charset=", 1)[1].split(";")[0].strip(" \"'")
        encoding = _text_encoding(label, from_meta=False)
    if encoding is None:
        sniff = re.search(rb"""<meta[^>]+charset=["']?([\w-]+)""", body[:4096], re.IGNORECASE)
        if sniff:
            encoding = _text_encoding(sniff.group(1).decode("ascii"), from_meta=True)
    try:
        text = body.decode(encoding or "utf-8", "replace")
    except (LookupError, UnicodeError, ValueError):
        text = body.decode("utf-8", "replace")
    # PostgreSQL text and JSONB cannot hold NUL.
    return text.replace("\x00", "")
