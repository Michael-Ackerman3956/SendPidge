"""Turn a raw .eml into the few fields the Reader and the policy need."""

import re
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from urllib.parse import urlsplit

PIDGE_AUTH_HEADER_NAME = "X-Pidge-Auth"
LINKS_MAX_COUNT = 20
LINK_TEXT_MAX_CHARACTERS = 200
LINK_HREF_MAX_CHARACTERS = 2048
WEB_SCHEMES = ("http", "https")
# Approximate: the public suffix list is not shipped; these two-part suffixes cover the family's likely domains.
TWO_PART_PUBLIC_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.kr", "or.kr", "ac.kr", "go.kr", "ne.kr",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "co.nz", "com.br", "com.mx", "co.in", "com.sg", "com.hk", "co.za",
})


@dataclass(frozen=True)
class EmailLink:
    """One anchor from the HTML part, from parsing alone: nothing is fetched or resolved."""

    visible_text: str
    href: str
    registrable_domain: str  # empty for mailto:, tel:, javascript:, fragments and bare paths
    is_hidden: bool  # no visible text at all


@dataclass(frozen=True)
class ParsedEmail:
    sender_name: str
    sender_address: str
    sender_domain: str
    subject: str
    text_body: str
    pidge_auth_header_values: tuple[str, ...] = field(default_factory=tuple)
    links: tuple[EmailLink, ...] = field(default_factory=tuple)
    links_unreadable: bool = False  # the HTML part exists but would not decode, so the links are unknown


class _TextOnlyExtractor(HTMLParser):
    """Collects visible text and drops scripts, styles and tags."""

    SKIPPED_TAGS = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIPPED_TAGS:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIPPED_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skip_depth == 0:
            self.text_parts.append(data)


def strip_html(html_text: str) -> str:
    extractor = _TextOnlyExtractor()
    extractor.feed(html_text)
    return collapse_whitespace(" ".join(extractor.text_parts))


def collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def collapse_whitespace_keeping_paragraphs(text: str) -> str:
    """Wrapped lines join into one; a blank line stays a paragraph break, so the PWA shows the sender's paragraphs."""
    paragraphs = (collapse_whitespace(paragraph) for paragraph in re.split(r"\n[ \t\r]*\n", text))
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


def parse_eml(raw_bytes: bytes) -> ParsedEmail:
    message = message_from_bytes(raw_bytes, policy=policy.default)
    sender_name, sender_address = parseaddr(message.get("From", ""))
    sender_domain = sender_address.rpartition("@")[2].lower()
    links, links_unreadable = extract_links(message)
    return ParsedEmail(
        sender_name=sender_name,
        sender_address=sender_address,
        sender_domain=sender_domain,
        subject=collapse_whitespace(message.get("Subject", "")),
        text_body=extract_text_body(message),
        pidge_auth_header_values=tuple(message.get_all(PIDGE_AUTH_HEADER_NAME, [])),
        links=links,
        links_unreadable=links_unreadable,
    )


def extract_text_body(message: EmailMessage) -> str:
    """Prefer the text/plain part; otherwise strip the HTML part."""
    plain_part = message.get_body(preferencelist=("plain",))
    if plain_part is not None:
        return collapse_whitespace_keeping_paragraphs(plain_part.get_content())
    html_part = message.get_body(preferencelist=("html",))
    if html_part is not None:
        return strip_html(html_part.get_content())
    return ""


class _AnchorExtractor(HTMLParser):
    """Collects every <a href> with the visible text between its tags; stops at LINKS_MAX_COUNT."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[EmailLink] = []
        self.open_href: str | None = None
        self.open_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or len(self.links) >= LINKS_MAX_COUNT:
            return
        href = next((value for name, value in attrs if name == "href" and value), None)
        if href is None:
            return
        self.open_href = href.strip()[:LINK_HREF_MAX_CHARACTERS]
        self.open_text_parts = []

    def handle_data(self, data: str) -> None:
        if self.open_href is not None:
            self.open_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self.open_href is None:
            return
        visible_text = collapse_whitespace(" ".join(self.open_text_parts))[:LINK_TEXT_MAX_CHARACTERS]
        self.links.append(EmailLink(visible_text, self.open_href, registrable_domain_of_href(self.open_href), visible_text == ""))
        self.open_href = None
        self.open_text_parts = []


def registrable_domain_of_href(href: str) -> str:
    """'https://pay.zelle-help.example/x' -> 'zelle-help.example'; anything that is not a web URL -> ''."""
    try:
        parts = urlsplit(href)
    except ValueError:
        return ""
    if parts.scheme.lower() not in WEB_SCHEMES or not parts.hostname:
        return ""
    return registrable_domain_of_host(parts.hostname)


def registrable_domain_of_host(host: str) -> str:
    labels = host.lower().strip(".").split(".")
    if len(labels) < 2:
        return host.lower()
    suffix_label_count = 3 if ".".join(labels[-2:]) in TWO_PART_PUBLIC_SUFFIXES else 2
    return ".".join(labels[-suffix_label_count:])


def extract_links(message: EmailMessage) -> tuple[tuple[EmailLink, ...], bool]:
    """The HTML part's anchors even when a plain part exists (the Reader still sees only text_body).

    Returns (links, links_unreadable); an HTML part that will not decode is reported, never treated as link-free.
    """
    html_part = message.get_body(preferencelist=("html",))
    if html_part is None:
        return (), False
    try:
        html_text = html_part.get_content()
    except (LookupError, UnicodeDecodeError, KeyError):
        return (), True
    extractor = _AnchorExtractor()
    extractor.feed(html_text)
    extractor.close()
    return tuple(extractor.links), False
