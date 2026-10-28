"""A live feed's bytes become rows in the lists.public.json seed shape, read by the source's FeedParser.

Rows are never widened: a domain row only for an entry whose host is its own registrable domain (or www. + it) with no
path, query or port; any other web host or URL is a url row (subdomains, hosts under a shared suffix, shorteners); an
AMF email is an address row only, never its domain. A firm name, a defanged or unreadable entry is skipped and counted,
never stored. Nothing here logs.
"""

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from shared.email_parse import TWO_PART_PUBLIC_SUFFIXES
from shared.enums import IndicatorKind, ListCategory, ListSource, ListStrength
from shared.links import KNOWN_SHORTENER_HOSTS
from shared.list_enums import FeedParser
from shared.lists import HOST_PATTERN, canonical_url, domain_indicator_key, indicator_key
from shared.sources import SourceRow

FEED_TEXT_ENCODING = "utf-8-sig"  # strips the BOM, so the first header name reads clean
FEED_DATE_FORMAT = "%Y-%m-%d"
FEED_ROW_TIER = 1
URL_SCHEME_SEPARATOR = "://"
SCHEMELESS_ENTRY_PREFIX = "https://"  # AMF writes hosts without a scheme
WWW_PREFIX = "www."
BARE_PATHS = ("", "/")
KISA_DELIMITER = ","
KISA_DATE_COLUMN = "날짜"
KISA_URL_COLUMN = "홈페이지주소"
AMF_DELIMITER = ";"
AMF_NAME_COLUMN = "nom"
AMF_CATEGORY_COLUMN = "categorie"
AMF_DATE_COLUMN = "date_inscription"
AMF_COLUMNS = (AMF_NAME_COLUMN, AMF_CATEGORY_COLUMN, AMF_DATE_COLUMN)
AMF_CATEGORY_SEPARATOR = "|"
AMF_IMPERSONATION_CATEGORIES = frozenset({"Usurpation", "Usurpation_AMF"})
ADDRESS_LOCAL_PART_PATTERN = re.compile(r"[a-z0-9._%+-]+")


class FeedParseError(ValueError):
    """The feed does not decode as strict utf-8, does not read as csv, or its header lacks a column the parser reads."""


@dataclass(frozen=True)
class ParsedFeed:
    rows: list[dict]    # one per distinct (kind, key), first entry wins
    skipped_count: int  # entries that are not a plain web address, an address or a dated record


@dataclass(frozen=True)
class FeedEntry:
    """One record read from a feed: its indicator (None when the record is skipped), strength and reason."""

    indicator: tuple[IndicatorKind, str] | None
    strength: ListStrength
    reason: str


def read_feed_records(feed_bytes: bytes, delimiter: str, required_columns: tuple[str, ...]) -> list[dict]:
    try:
        feed_text = feed_bytes.decode(FEED_TEXT_ENCODING, errors="strict")
        reader = csv.DictReader(io.StringIO(feed_text, newline=""), delimiter=delimiter)
        if reader.fieldnames is None or not set(required_columns) <= set(reader.fieldnames):
            raise FeedParseError("the header lacks a column this parser reads")
        return list(reader)
    except (UnicodeDecodeError, csv.Error) as error:
        raise FeedParseError("the feed does not read as utf-8 csv") from error


def stripped_cell(record: dict, column: str) -> str:
    """A short record has None in its missing cells; that reads as an empty cell, which skips the record."""
    cell = record[column]
    return "" if cell is None else cell.strip()


def is_feed_date(text: str) -> bool:
    try:
        datetime.strptime(text, FEED_DATE_FORMAT)
    except ValueError:
        return False
    return True


def ascii_host_of(host: str) -> str | None:
    """The host in its ASCII (xn--) form when it is a real host name; None for a firm name, an IP address or garbage."""
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return ascii_host if HOST_PATTERN.fullmatch(ascii_host) else None


def is_bare_registrable_host(ascii_host: str) -> bool:
    """armen.ltd or www.armen.ltd; never a subdomain, a host under a shared suffix (co.com), a public suffix itself (co.uk),
    a punycode host or a shortener."""
    domain = ascii_host.removeprefix(WWW_PREFIX)
    if domain in KNOWN_SHORTENER_HOSTS or domain in TWO_PART_PUBLIC_SUFFIXES:
        return False
    try:
        return domain_indicator_key(domain) == domain
    except ValueError:
        return False


def web_indicator_of(entry: str) -> tuple[IndicatorKind, str] | None:
    """A host or URL → (DOMAIN, the bare registrable domain) or (URL, the entry as a URL); None when it is not one."""
    url = entry if URL_SCHEME_SEPARATOR in entry else SCHEMELESS_ENTRY_PREFIX + entry
    try:
        canonical_url(url)  # raises on a non-web scheme (hxxp, ftp), a user name or no host
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    ascii_host = None if parts.hostname is None else ascii_host_of(parts.hostname)
    if ascii_host is None:
        return None
    if parts.path in BARE_PATHS and not parts.query and port is None and is_bare_registrable_host(ascii_host):
        return IndicatorKind.DOMAIN, ascii_host.removeprefix(WWW_PREFIX)
    return IndicatorKind.URL, url


def address_indicator_of(entry: str) -> tuple[IndicatorKind, str] | None:
    folded_address = entry.casefold()
    local_part, _, domain = folded_address.partition("@")
    if folded_address.count("@") != 1 or not ADDRESS_LOCAL_PART_PATTERN.fullmatch(local_part) or not HOST_PATTERN.fullmatch(domain):
        return None
    return IndicatorKind.ADDRESS, folded_address


def amf_indicator_of(name: str) -> tuple[IndicatorKind, str] | None:
    """A website or URL through the web rule, an email as an address row; a firm name is neither and is skipped."""
    if "@" in name and URL_SCHEME_SEPARATOR not in name:
        return address_indicator_of(name)
    return web_indicator_of(name)


def feed_row(source: SourceRow, indicator: tuple[IndicatorKind, str], entry: FeedEntry, now: str) -> dict:
    kind, value = indicator
    return {
        "kind": str(kind), "value": value, "category": str(ListCategory.SCAM), "strength": str(entry.strength),
        "tier": FEED_ROW_TIER, "reason": entry.reason, "attribution": source.attribution_text, "addedAt": now,
        "source": str(ListSource.PUBLIC_FEED), "sourceId": source.source_id,
    }


def collect_feed_rows(source: SourceRow, entries: Iterable[FeedEntry], now: str) -> ParsedFeed:
    rows_by_key: dict[tuple[IndicatorKind, str], dict] = {}
    skipped_count = 0
    for entry in entries:
        if entry.indicator is None:
            skipped_count += 1
            continue
        kind, value = entry.indicator
        try:
            row_key = (kind, indicator_key(kind, value))
        except ValueError:
            skipped_count += 1
            continue
        if row_key not in rows_by_key:
            rows_by_key[row_key] = feed_row(source, entry.indicator, entry, now)
    return ParsedFeed(list(rows_by_key.values()), skipped_count)


def read_kisa_entries(feed_bytes: bytes) -> list[FeedEntry]:
    entries = []
    for record in read_feed_records(feed_bytes, KISA_DELIMITER, (KISA_DATE_COLUMN, KISA_URL_COLUMN)):
        reported_on, url = stripped_cell(record, KISA_DATE_COLUMN), stripped_cell(record, KISA_URL_COLUMN)
        indicator = web_indicator_of(url) if url and is_feed_date(reported_on) else None
        entries.append(FeedEntry(indicator, ListStrength.LISTED, f"Reported to KISA as a phishing site, {reported_on}"))
    return entries


def read_amf_entries(feed_bytes: bytes) -> list[FeedEntry]:
    """Strength listed for an impersonation category, suspect for any other unauthorised firm."""
    entries = []
    for record in read_feed_records(feed_bytes, AMF_DELIMITER, AMF_COLUMNS):
        name, categories, listed_on = (stripped_cell(record, column) for column in AMF_COLUMNS)
        indicator = amf_indicator_of(name) if name and is_feed_date(listed_on) else None
        category_set = {category.strip() for category in categories.split(AMF_CATEGORY_SEPARATOR)}
        strength = ListStrength.LISTED if category_set & AMF_IMPERSONATION_CATEGORIES else ListStrength.SUSPECT
        entries.append(FeedEntry(indicator, strength, f"On the AMF blacklist ({categories}) since {listed_on}"))
    return entries


ENTRY_READERS = {FeedParser.KISA_PHISHING_CSV: read_kisa_entries, FeedParser.AMF_BLACKLIST_CSV: read_amf_entries}


def parse_feed(source: SourceRow, feed_bytes: bytes, now: str) -> ParsedFeed:
    """The source's rows, deduplicated, in the lists.public.json seed shape. Raises FeedParseError."""
    if source.feed_parser is None:
        raise FeedParseError(f"{source.source_id} is a snapshot source with no feed parser")
    return collect_feed_rows(source, ENTRY_READERS[source.feed_parser](feed_bytes), now)
