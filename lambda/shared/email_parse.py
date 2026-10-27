"""Turn a raw .eml into the few fields the Reader and the policy need."""

import re
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from shared.enums import HiddenBy, LinkKind, LinkSource
from shared.html_links import SKIPPED_TEXT_TAGS, ExtractedHtmlDocument, FoundLink, collapse_whitespace, extract_links_from_html

PIDGE_AUTH_HEADER_NAME = "X-Pidge-Auth"
LINKS_MAX_COUNT = 20
LINK_TEXT_MAX_CHARACTERS = 200
WEB_SCHEMES = ("http", "https")
LINK_KIND_BY_SCHEME = {
    "http": LinkKind.WEB, "https": LinkKind.WEB, "mailto": LinkKind.MAILTO, "tel": LinkKind.TEL,
    "javascript": LinkKind.SCRIPT, "vbscript": LinkKind.SCRIPT, "data": LinkKind.DATA,
}
URL_SCHEME_PATTERN = re.compile(r"([a-z][a-z0-9+.\-]*):", re.IGNORECASE)
URL_IGNORED_CHARACTERS = re.compile(r"[\t\n\r]")  # a browser drops these anywhere in a URL
URL_EDGE_IGNORED_CHARACTERS = "".join(chr(code_point) for code_point in range(0x21))  # and C0 controls and spaces at either end
PUNYCODE_LABEL_PREFIX = "xn--"
HTML_DOCUMENT_CONTENT_TYPES = frozenset({"text/html", "image/svg+xml"})
HTML_DOCUMENT_FILENAME_SUFFIXES = (".htm", ".html", ".shtml", ".svg")
# Approximate: the public suffix list is not shipped; these two-part suffixes cover the family's likely domains.
TWO_PART_PUBLIC_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.kr", "or.kr", "ac.kr", "go.kr", "ne.kr",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "co.nz", "com.br", "com.mx", "co.in", "com.sg", "com.hk", "co.za",
})


@dataclass(frozen=True)
class EmailLink:
    """One link from an HTML document in the message, from parsing alone: nothing is fetched, opened or resolved."""

    visible_text: str
    href: str
    registrable_domain: str  # empty for mailto:, tel:, javascript:, data:, fragments and bare paths
    is_hidden: bool  # no visible text at all (text in a hidden element is not visible)
    kind: LinkKind
    hidden_by: HiddenBy
    source: LinkSource
    # Visible anchor text characters ÷ visible characters of the HTML it sits in; 1.0 for an anchor holding every image of a
    # document with no visible characters; 0.0 for an <area>.
    body_share: float
    has_internationalised_host: bool  # a punycode (xn--) or non-ASCII host label


@dataclass(frozen=True)
class ParsedEmail:
    sender_name: str
    sender_address: str
    sender_domain: str
    subject: str
    text_body: str
    pidge_auth_header_values: tuple[str, ...] = field(default_factory=tuple)
    links: tuple[EmailLink, ...] = field(default_factory=tuple)
    links_unreadable: bool = False  # an HTML document exists in the message but would not decode, so its links are unknown
    links_truncated: bool = False  # more than LINKS_MAX_COUNT links; the ones past the cap are not in links
    image_count: int = 0  # <img> tags in the HTML body, tracking pixels included
    # Characters of the HTML body's visible text (outside script/style/head/title and hidden elements, format characters
    # dropped, whitespace collapsed); 0 when there is no readable HTML body.
    visible_html_characters: int = 0


class _TextOnlyExtractor(HTMLParser):
    """Collects visible text and drops scripts, styles and tags."""

    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIPPED_TEXT_TAGS:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TEXT_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skip_depth == 0:
            self.text_parts.append(data)


def strip_html(html_text: str) -> str:
    extractor = _TextOnlyExtractor()
    extractor.feed(html_text)
    return collapse_whitespace(" ".join(extractor.text_parts))


def collapse_whitespace_keeping_paragraphs(text: str) -> str:
    """Wrapped lines join into one; a blank line stays a paragraph break, so the PWA shows the sender's paragraphs."""
    paragraphs = (collapse_whitespace(paragraph) for paragraph in re.split(r"\n[ \t\r]*\n", text))
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


def parse_eml(raw_bytes: bytes) -> ParsedEmail:
    message = message_from_bytes(raw_bytes, policy=policy.default)
    sender_name, sender_address = parseaddr(message.get("From", ""))
    sender_domain = sender_address.rpartition("@")[2].lower()
    link_scan = scan_html_for_links(message)
    return ParsedEmail(
        sender_name=sender_name,
        sender_address=sender_address,
        sender_domain=sender_domain,
        subject=collapse_whitespace(message.get("Subject", "")),
        text_body=extract_text_body(message),
        pidge_auth_header_values=tuple(message.get_all(PIDGE_AUTH_HEADER_NAME, [])),
        links=link_scan.links,
        links_unreadable=link_scan.links_unreadable,
        links_truncated=link_scan.links_truncated,
        image_count=link_scan.image_count,
        visible_html_characters=link_scan.visible_html_characters,
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


def browser_cleaned_href(href: str) -> str:
    """Tabs and newlines anywhere, and C0 controls and spaces at either end, dropped the way a browser drops them."""
    return URL_IGNORED_CHARACTERS.sub("", href).strip(URL_EDGE_IGNORED_CHARACTERS)


def link_kind_of_href(href: str) -> LinkKind:
    """The scheme decides the kind, read the way a browser reads it: tabs and newlines inside are ignored."""
    cleaned_href = browser_cleaned_href(href)
    scheme_match = URL_SCHEME_PATTERN.match(cleaned_href)
    if scheme_match is None:
        return LinkKind.OTHER
    scheme = scheme_match.group(1).lower()
    return LINK_KIND_BY_SCHEME[scheme] if scheme in LINK_KIND_BY_SCHEME else LinkKind.OTHER  # any other scheme is named OTHER


def web_hostname_of_href(href: str) -> str:
    """The lower-cased, percent-decoded host of an http(s) href read the WHATWG way: every backslash is a slash and any run
    of slashes after the scheme is '//'. '' for anything that is not a web URL or will not split."""
    cleaned_href = browser_cleaned_href(href)
    scheme_match = URL_SCHEME_PATTERN.match(cleaned_href)
    if scheme_match is None or scheme_match.group(1).lower() not in WEB_SCHEMES:
        return ""
    after_scheme = cleaned_href[scheme_match.end():].replace("\\", "/").lstrip("/")
    try:
        hostname = urlsplit(f"{scheme_match.group(1).lower()}://{after_scheme}").hostname
    except ValueError:
        return ""
    return unquote(hostname).lower() if hostname else ""


def registrable_domain_of_href(href: str) -> str:
    """'https://pay.zelle-help.example/x' -> 'zelle-help.example'; anything that is not a web URL -> ''."""
    hostname = web_hostname_of_href(href)
    return registrable_domain_of_host(hostname) if hostname else ""


def has_internationalised_host(href: str) -> bool:
    """True for a web host with a punycode (xn--) label or any non-ASCII character."""
    hostname = web_hostname_of_href(href)
    return not hostname.isascii() or any(label.startswith(PUNYCODE_LABEL_PREFIX) for label in hostname.split("."))


def decode_punycode_host(host: str) -> str:
    """'xn--80ak6aa92e.com' -> 'аррӏе.com'; a host that will not decode is returned as it came (the link is still marked IDN)."""
    if PUNYCODE_LABEL_PREFIX not in host or not host.isascii():
        return host
    try:
        return host.encode("ascii").decode("idna").lower()
    except UnicodeError:
        return host


def registrable_domain_of_host(host: str) -> str:
    """The last two labels (three under a two-part public suffix), after punycode labels are decoded."""
    decoded_host = decode_punycode_host(host.lower().strip("."))
    labels = decoded_host.split(".")
    if len(labels) < 2:
        return decoded_host
    suffix_label_count = 3 if ".".join(labels[-2:]) in TWO_PART_PUBLIC_SUFFIXES else 2
    return ".".join(labels[-suffix_label_count:])


def hidden_by_of(found_link: FoundLink) -> HiddenBy:
    if found_link.is_css_hidden:
        return HiddenBy.CSS
    if found_link.source is LinkSource.IMAGE_MAP or (found_link.full_text == "" and found_link.image_count > 0):
        return HiddenBy.IMAGE_ONLY
    return HiddenBy.NO_TEXT if found_link.full_text == "" else HiddenBy.NONE


def body_share_of(found_link: FoundLink, document: ExtractedHtmlDocument) -> float:
    if found_link.source is LinkSource.IMAGE_MAP:
        return 0.0
    if document.visible_characters == 0:
        holds_every_image = 0 < document.image_count == found_link.image_count
        return 1.0 if holds_every_image else 0.0
    return len(found_link.full_text) / document.visible_characters


def build_email_link(found_link: FoundLink, document: ExtractedHtmlDocument) -> EmailLink:
    visible_text = found_link.full_text[:LINK_TEXT_MAX_CHARACTERS]
    return EmailLink(
        visible_text=visible_text,
        href=found_link.href,
        registrable_domain=registrable_domain_of_href(found_link.href),
        is_hidden=visible_text == "",
        kind=link_kind_of_href(found_link.href),
        hidden_by=hidden_by_of(found_link),
        source=found_link.source,
        body_share=body_share_of(found_link, document),
        has_internationalised_host=has_internationalised_host(found_link.href),
    )


@dataclass(frozen=True)
class HtmlLinkScan:
    """Every link in the message's HTML documents, plus what the HTML body is made of."""

    links: tuple[EmailLink, ...]
    links_unreadable: bool
    links_truncated: bool
    image_count: int
    visible_html_characters: int


@dataclass(frozen=True)
class _HtmlDocumentScan:
    links: tuple[EmailLink, ...]
    is_truncated: bool
    image_count: int
    visible_characters: int


def scan_html_document(html_text: str, anchor_source: LinkSource, capacity: int) -> _HtmlDocumentScan:
    document = extract_links_from_html(html_text, anchor_source, capacity)
    links = tuple(build_email_link(found_link, document) for found_link in document.found_links)
    return _HtmlDocumentScan(links, document.is_truncated, document.image_count, document.visible_characters)


def is_html_document_part(part: EmailMessage) -> bool:
    """text/html or image/svg+xml, or a file named .htm/.html/.shtml/.svg whatever its content type."""
    if part.get_content_type() in HTML_DOCUMENT_CONTENT_TYPES:
        return True
    filename = part.get_filename()
    return filename is not None and filename.lower().endswith(HTML_DOCUMENT_FILENAME_SUFFIXES)


def html_documents_of(message: EmailMessage) -> list[tuple[EmailMessage, LinkSource]]:
    """The part get_body picks first, then every other HTML document at any depth (inside multipart/* and forwarded
    message/rfc822 parts too); parsed only, never rendered or opened."""
    html_part = message.get_body(preferencelist=("html",))
    documents = [(html_part, LinkSource.HTML_PART)] if html_part is not None else []
    for part in message.walk():
        if part is not html_part and not part.is_multipart() and is_html_document_part(part):
            documents.append((part, LinkSource.HTML_ATTACHMENT))
    return documents


def read_html_text(part: EmailMessage) -> str:
    """The part's text; bytes content (an SVG or an octet-stream .html) is read as UTF-8. Raises when it will not decode."""
    content = part.get_content()
    return content.decode("utf-8") if isinstance(content, bytes) else content


def scan_html_for_links(message: EmailMessage) -> HtmlLinkScan:
    """Links of every HTML document in the message even when a plain part exists (the Reader still sees only text_body).

    All documents share LINKS_MAX_COUNT. A document that will not decode sets links_unreadable, never reads as link-free.
    """
    links: list[EmailLink] = []
    is_unreadable = is_truncated = False
    image_count = visible_html_characters = 0
    for part, anchor_source in html_documents_of(message):
        try:
            html_text = read_html_text(part)
        except (LookupError, UnicodeDecodeError, KeyError):
            is_unreadable = True
            continue
        document_scan = scan_html_document(html_text, anchor_source, LINKS_MAX_COUNT - len(links))
        links.extend(document_scan.links)
        is_truncated = is_truncated or document_scan.is_truncated
        if anchor_source is LinkSource.HTML_PART:
            image_count, visible_html_characters = document_scan.image_count, document_scan.visible_characters
    return HtmlLinkScan(tuple(links), is_unreadable, is_truncated, image_count, visible_html_characters)
