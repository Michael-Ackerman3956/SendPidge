"""Turn a raw .eml into the few fields the Reader and the policy need."""

from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser

PIDGE_AUTH_HEADER_NAME = "X-Pidge-Auth"


@dataclass(frozen=True)
class ParsedEmail:
    sender_name: str
    sender_address: str
    sender_domain: str
    subject: str
    text_body: str
    pidge_auth_header_values: tuple[str, ...] = field(default_factory=tuple)


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


def parse_eml(raw_bytes: bytes) -> ParsedEmail:
    message = message_from_bytes(raw_bytes, policy=policy.default)
    sender_name, sender_address = parseaddr(message.get("From", ""))
    sender_domain = sender_address.rpartition("@")[2].lower()
    return ParsedEmail(
        sender_name=sender_name,
        sender_address=sender_address,
        sender_domain=sender_domain,
        subject=collapse_whitespace(message.get("Subject", "")),
        text_body=extract_text_body(message),
        pidge_auth_header_values=tuple(message.get_all(PIDGE_AUTH_HEADER_NAME, [])),
    )


def extract_text_body(message: EmailMessage) -> str:
    """Prefer the text/plain part; otherwise strip the HTML part."""
    plain_part = message.get_body(preferencelist=("plain",))
    if plain_part is not None:
        return collapse_whitespace(plain_part.get_content())
    html_part = message.get_body(preferencelist=("html",))
    if html_part is not None:
        return strip_html(html_part.get_content())
    return ""
