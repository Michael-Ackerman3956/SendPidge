"""The scam list: LIST#<kind> rows read from the same table, turned into reasons. Policy code decides what a reason means.

A hit is a reason, never a verdict. Rows are hand-written samples (source: sample), the tier-0 rows pidge-verdict writes on
Confirm (source: pidge-confirmed) and public-feed rows synced from SOURCE# feeds (source: public-feed, with a sourceId).
check() answers a list of indicators with hits, attributions and a coverage that reads unavailable, never clean, when a
feed it relies on is stale or the table cannot be read.
"""

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import urlsplit

from botocore.exceptions import ClientError

from shared import graph
from shared.email_parse import registrable_domain_of_host
from shared.enums import IndicatorKind, ListCategory, ListSource, ListStrength, ReasonCode
from shared.list_enums import ScamListCoverageState
from shared.metrics import emit_count_metric

logger = logging.getLogger(__name__)

LIST_PARTITION_PREFIX = "LIST#"
SOURCE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,59}$")
HOST_PATTERN = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")
WEB_SCHEMES = ("http", "https")
DEFAULT_WEB_PORTS = (80, 443)
PUBLIC_FEED_INDICATOR_KINDS = (IndicatorKind.DOMAIN, IndicatorKind.URL, IndicatorKind.ADDRESS)
LIST_CHECK_FAILED_METRIC_NAME = "ListCheckFailed"
COMPLETE_CACHE_SECONDS = 86400    # a complete answer may be reused for a day
UNAVAILABLE_CACHE_SECONDS = 300   # an unavailable answer is asked again after five minutes
STALE_SYNC_INTERVAL_MULTIPLIER = 3  # a feed not synced for 3 x its interval is stale
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Digits and symbols that read as letters, plus Cyrillic and Greek letters that print like Latin ones.
CONFUSABLE_CHARACTERS = str.maketrans({
    "0": "o", "1": "l", "3": "e", "5": "s", "7": "t", "8": "b", "9": "g", "|": "l",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "ӏ": "l",
    "α": "a", "ο": "o", "ν": "v", "ι": "i", "κ": "k", "τ": "t",
})
CONFUSABLE_PAIRS = (("rn", "m"), ("vv", "w"), ("cl", "d"))
MINIMUM_NAME_LENGTH_FOR_EDIT_DISTANCE = 4  # "Jim" vs "Kim" is not a lookalike; "Jimln" vs "Jimin" is


class ListLookupFailed(Exception):
    """The table could not be read; the caller marks the email rather than passing it."""


class ListStore(Protocol):
    def get_row(self, kind: IndicatorKind, key: str) -> dict | None: ...


class DynamoListStore:
    """Reads LIST# rows from the production table."""

    def get_row(self, kind: IndicatorKind, key: str) -> dict | None:
        try:
            return graph.get_table().get_item(Key={"PK": list_partition(kind), "SK": key}).get("Item")
        except ClientError as error:
            raise ListLookupFailed(str(error)) from error


class SeedListStore:
    """Reads the same rows from a seed file's list, for scripts/read_eml.py; the row shape is identical."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows_by_key = {(row["PK"], row["SK"]): row for row in rows}

    def get_row(self, kind: IndicatorKind, key: str) -> dict | None:
        return self._rows_by_key.get((list_partition(kind), key))


def list_partition(kind: IndicatorKind) -> str:
    return f"{LIST_PARTITION_PREFIX}{kind}"


def normalise_registrable_domain(domain_or_host: str) -> str:
    """'Mail.Jimin-Help.Example.' → 'jimin-help.example'; the same public-suffix rule as the link parser."""
    return registrable_domain_of_host(unicodedata.normalize("NFKC", domain_or_host.strip()).casefold().strip("."))


def normalise_phone_e164(phone_text: str) -> str:
    """Keeps digits only and one leading '+'; '00' international prefixes become '+'. Raises on anything that is not a number."""
    digits = re.sub(r"[^\d+]", "", phone_text.strip())
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    digits = "+" + digits.lstrip("+") if digits else ""
    if not re.fullmatch(r"\+\d{7,15}", digits):
        raise ValueError(f"not a phone number: {phone_text!r}")
    return digits


def canonical_url(url: str) -> str:
    """'HTTPS://Bit.ly/AbC#x' → 'https://bit.ly/AbC': scheme and host lower-cased, default port and fragment dropped, the
    path's case kept (a short link's token is case-sensitive). Raises ValueError on a non-web scheme or a user name."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in WEB_SCHEMES or parts.username is not None or parts.password is not None or not parts.hostname:
        raise ValueError(f"not a plain web URL: {url!r}")
    port = parts.port
    host = parts.hostname if port is None or port in DEFAULT_WEB_PORTS else f"{parts.hostname}:{port}"
    query = f"?{parts.query}" if parts.query else ""
    return f"{scheme}://{host}{parts.path or '/'}{query}"


def domain_indicator_key(domain_or_host: str) -> str:
    host = unicodedata.normalize("NFKC", domain_or_host.strip()).casefold().strip(".")
    if not HOST_PATTERN.fullmatch(host):
        raise ValueError(f"not a host name: {domain_or_host!r}")
    return normalise_registrable_domain(host)


def address_indicator_key(address: str) -> str:
    folded_address = address.strip().casefold()
    local_part, _, domain = folded_address.partition("@")
    if folded_address.count("@") != 1 or not local_part or not domain:
        raise ValueError(f"not an address: {address!r}")
    return folded_address


def indicator_key(kind: IndicatorKind, value: str) -> str:
    """The LIST#<kind> sort key of one value; raises ValueError when the value is not of that kind. URL keys are the
    SHA-256 of the canonical URL."""
    if kind is IndicatorKind.DOMAIN:
        return domain_indicator_key(value)
    if kind is IndicatorKind.ADDRESS:
        return address_indicator_key(value)
    if kind is IndicatorKind.PHONE:
        return normalise_phone_e164(value)
    if kind is IndicatorKind.URL:
        return hashlib.sha256(canonical_url(value).encode()).hexdigest()
    if kind is IndicatorKind.LOOKALIKE:
        return confusable_skeleton(value)
    raise ValueError(f"no key rule for {kind!r}")


def parse_timestamp(iso_text: str) -> datetime:
    return datetime.strptime(iso_text, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def add_seconds_to_timestamp(iso_text: str, seconds: int) -> str:
    return graph.iso_timestamp(parse_timestamp(iso_text) + timedelta(seconds=seconds))


def confusable_skeleton(text: str) -> str:
    """'PayPa1.com' and 'paypal.com', 'Jіmin' (Cyrillic і) and 'Jimin' share a skeleton."""
    folded = unicodedata.normalize("NFKD", text).casefold()
    folded = "".join(character for character in folded if not unicodedata.combining(character))
    skeleton = folded.translate(CONFUSABLE_CHARACTERS)
    for pair, replacement in CONFUSABLE_PAIRS:
        skeleton = skeleton.replace(pair, replacement)
    return re.sub(r"[^a-z0-9.]", "", skeleton)


def is_within_one_edit(first: str, second: str) -> bool:
    """One insertion, deletion or substitution apart (both already skeletonised)."""
    if abs(len(first) - len(second)) > 1:
        return False
    if len(first) == len(second):
        return sum(a != b for a, b in zip(first, second)) == 1
    longer, shorter = (first, second) if len(first) > len(second) else (second, first)
    for index in range(len(longer)):
        if longer[:index] + longer[index + 1:] == shorter:
            return True
    return False


def is_lookalike_of(candidate_domain: str, family_domain: str) -> bool:
    """Same skeleton but a different domain, or the family label hyphenated into a different domain (zelle-pay-help)."""
    if not candidate_domain or candidate_domain == family_domain:
        return False
    if confusable_skeleton(candidate_domain) == confusable_skeleton(family_domain):
        return True
    family_label = family_domain.split(".")[0]
    candidate_label = candidate_domain.split(".")[0]
    return len(family_label) >= 4 and family_label in candidate_label.split("-") and candidate_label != family_label


def is_lookalike_name(sender_name: str, contact_name: str) -> bool:
    """A different name that reads like the contact's: same skeleton, or one edit apart on names of four letters or more."""
    folded_sender = " ".join(sender_name.split()).casefold()
    folded_contact = " ".join(contact_name.split()).casefold()
    if not folded_sender or folded_sender == folded_contact:
        return False
    sender_skeleton, contact_skeleton = confusable_skeleton(folded_sender), confusable_skeleton(folded_contact)
    if sender_skeleton == contact_skeleton:
        return True
    if len(contact_skeleton) < MINIMUM_NAME_LENGTH_FOR_EDIT_DISTANCE:
        return False
    return is_within_one_edit(sender_skeleton, contact_skeleton)


@dataclass(frozen=True)
class ListRow:
    """The parsed envelope of one LIST# row; parse_list_row rejects anything outside the enums before it is used or seeded."""

    kind: IndicatorKind
    key: str
    category: ListCategory
    strength: ListStrength
    source: ListSource
    tier: int
    reason: str
    contact_name: str | None = None  # lookalike rows: the contact the skeleton belongs to
    source_id: str | None = None     # public-feed rows: the SOURCE# id the row was synced from


def parse_list_row(row: dict) -> ListRow:
    partition = str(row["PK"])
    if not partition.startswith(LIST_PARTITION_PREFIX):
        raise ValueError(f"not a LIST# row: {partition}")
    return ListRow(
        kind=IndicatorKind(partition[len(LIST_PARTITION_PREFIX):]), key=str(row["SK"]),
        category=ListCategory(row["category"]), strength=ListStrength(row["strength"]), source=ListSource(row["source"]),
        tier=int(row["tier"]), reason=str(row["reason"]), contact_name=row.get("contactName"), source_id=row.get("sourceId"),
    )


def is_row_expired(row: dict, now_timestamp: str) -> bool:
    """Past its expiresAt: DynamoDB's TTL deletion lags by up to days, so a read checks the time itself."""
    expires_at = row.get("expiresAt")
    return expires_at is not None and expires_at <= now_timestamp


def list_row_from_seed(seed: dict) -> dict:
    """The stored shape of a sample row; parse_list_row must accept the result before it is written."""
    kind = IndicatorKind(seed["kind"])
    key = indicator_key(kind, seed["value"])
    row = {
        "PK": list_partition(kind), "SK": key,
        "category": str(ListCategory(seed["category"])), "strength": str(ListStrength(seed["strength"])),
        "source": str(ListSource.SAMPLE), "tier": int(seed.get("tier", 1)), "reason": seed["reason"],
        "attribution": seed.get("attribution", "sample data"), "addedAt": seed["addedAt"], "expiresAt": seed.get("expiresAt"),
        "generation": 0,
    }
    if "contactName" in seed:
        row["contactName"] = seed["contactName"]
    return row


def public_list_row_from_seed(seed: dict, generation: int, expires_at: str) -> dict:
    """A public-feed row (the lists.public.json shape) as stored, with its generation and expiry; ttlEpochSeconds is the
    table's TTL attribute. Refuses another source, a kind feeds may not write, a bad sourceId, and a domain that would
    widen to its registrable domain (app-ledger.co.com would list every co.com host)."""
    if ListSource(seed["source"]) is not ListSource.PUBLIC_FEED:
        raise ValueError(f"not a public-feed row: {seed['source']!r}")
    kind = IndicatorKind(seed["kind"])
    if kind not in PUBLIC_FEED_INDICATOR_KINDS:
        raise ValueError(f"a feed row may not be of kind {kind}")
    key = indicator_key(kind, seed["value"])
    if kind is IndicatorKind.DOMAIN and key != seed["value"].casefold():
        raise ValueError(f"domain {seed['value']!r} would widen to {key!r}")
    source_id = str(seed["sourceId"])
    if not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise ValueError(f"bad sourceId {source_id!r}")
    row = {
        "PK": list_partition(kind), "SK": key,
        "category": str(ListCategory(seed["category"])), "strength": str(ListStrength(seed["strength"])),
        "source": str(ListSource.PUBLIC_FEED), "sourceId": source_id, "tier": int(seed["tier"]), "reason": str(seed["reason"]),
        "attribution": str(seed["attribution"]), "addedAt": seed["addedAt"], "expiresAt": expires_at,
        "ttlEpochSeconds": int(parse_timestamp(expires_at).timestamp()), "generation": int(generation),
    }
    parse_list_row(row)
    return row


def get_unexpired_row(store: ListStore, kind: IndicatorKind, key: str, now_timestamp: str) -> dict | None:
    row = store.get_row(kind, key)
    return None if row is None or is_row_expired(row, now_timestamp) else row


def lookup_sender_reasons(
    store: ListStore, sender_address: str, sender_domain: str, sender_name: str, contacts: list[graph.Contact],
) -> list[ReasonCode]:
    """Address row, domain row, lookalike row, then code-only lookalikes against the family; expired rows are skipped.
    Raises ListLookupFailed."""
    reasons: list[ReasonCode] = []
    now_timestamp = graph.now_timestamp()
    address_row = get_unexpired_row(store, IndicatorKind.ADDRESS, sender_address.casefold(), now_timestamp)
    if address_row is not None:
        is_reported = parse_list_row(address_row).source is ListSource.PIDGE_CONFIRMED
        reasons.append(ReasonCode.REPORTED_SENDER if is_reported else ReasonCode.LISTED_SENDER_ADDRESS)
    registrable_domain = normalise_registrable_domain(sender_domain) if sender_domain else ""
    if registrable_domain and get_unexpired_row(store, IndicatorKind.DOMAIN, registrable_domain, now_timestamp) is not None:
        reasons.append(ReasonCode.LISTED_SENDER_DOMAIN)
    if has_lookalike_name(store, sender_name, sender_address, contacts):
        reasons.append(ReasonCode.LOOKALIKE_SENDER_NAME)
    family_domains = graph.domains_of(contacts)
    if registrable_domain and any(is_lookalike_of(registrable_domain, family_domain) for family_domain in family_domains):
        reasons.append(ReasonCode.LOOKALIKE_SENDER_DOMAIN)
    return reasons


def has_lookalike_name(store: ListStore, sender_name: str, sender_address: str, contacts: list[graph.Contact]) -> bool:
    """Never for an exact name (gate 1 owns those) and never from an address the near-match contact is known to use."""
    if not sender_name.strip():
        return False
    near_matches = [contact for contact in contacts if is_lookalike_name(sender_name, contact.contact_name)]
    lookalike_row = get_unexpired_row(store, IndicatorKind.LOOKALIKE, confusable_skeleton(sender_name), graph.now_timestamp())
    if lookalike_row is not None:
        listed = parse_list_row(lookalike_row)
        listed_contact = next((contact for contact in contacts if contact.contact_name.casefold() == (listed.contact_name or "").casefold()), None)
        if listed_contact is not None and sender_name.casefold() != listed_contact.contact_name.casefold():
            near_matches.append(listed_contact)
    return any(not contact.is_known_address(sender_address) for contact in near_matches)


def sender_reasons_or_lookup_failure(
    store: ListStore, sender_address: str, sender_domain: str, sender_name: str, contacts: list[graph.Contact],
) -> list[ReasonCode]:
    """A failed read becomes LIST_LOOKUP_FAILED (a mark the PWA shows) plus a logged error; never an empty list."""
    try:
        return lookup_sender_reasons(store, sender_address, sender_domain, sender_name, contacts)
    except ListLookupFailed as error:
        logger.error("scam-list lookup failed for %s: %s", sender_address, error)
        return [ReasonCode.LIST_LOOKUP_FAILED]


@dataclass(frozen=True)
class Indicator:
    kind: IndicatorKind
    value: str


@dataclass(frozen=True)
class ListHit:
    indicator: Indicator  # the asked indicator this row answered
    category: ListCategory
    strength: ListStrength
    source: ListSource
    source_id: str | None
    reason: str
    attribution: str


@dataclass(frozen=True)
class CheckResult:
    hits: tuple[ListHit, ...]
    coverage: ScamListCoverageState
    cache_until: str

    def as_payload(self) -> dict:
        return {
            "reasons": [{"kind": str(hit.indicator.kind), "category": str(hit.category), "strength": str(hit.strength), "reason": hit.reason}
                        for hit in self.hits],
            "attributions": [{"source": str(hit.source), "sourceId": hit.source_id, "text": hit.attribution} for hit in self.hits],
            "coverage": str(self.coverage),
            "cacheUntil": self.cache_until,
        }


class LiveSourceFreshness(Protocol):
    """What check() reads from a live source (a sources.SourceRow); lists.py never imports sources.py."""

    indicator_kinds: tuple[IndicatorKind, ...]
    last_sync_at: str | None
    sync_interval_minutes: int


def is_feed_stale(last_sync_at: str | None, interval_minutes: int, now: str) -> bool:
    """Never synced, or not synced for more than 3 x its interval."""
    if last_sync_at is None:
        return True
    return parse_timestamp(now) - parse_timestamp(last_sync_at) > timedelta(minutes=STALE_SYNC_INTERVAL_MULTIPLIER * interval_minutes)


def unavailable_check_result(now: str) -> CheckResult:
    return CheckResult((), ScamListCoverageState.UNAVAILABLE, add_seconds_to_timestamp(now, UNAVAILABLE_CACHE_SECONDS))


def list_hit_from_row(indicator: Indicator, row: dict) -> ListHit:
    parsed = parse_list_row(row)
    return ListHit(indicator, parsed.category, parsed.strength, parsed.source, parsed.source_id, parsed.reason,
                   str(row["attribution"]))


def find_list_hits(store: ListStore, indicators: list[Indicator], now: str) -> tuple[ListHit, ...]:
    """One read per indicator; a value that is not of its kind raises ValueError. Raises ListLookupFailed."""
    hits = []
    for indicator in indicators:
        row = get_unexpired_row(store, indicator.kind, indicator_key(indicator.kind, indicator.value), now)
        if row is not None:
            hits.append(list_hit_from_row(indicator, row))
    return tuple(hits)


def has_stale_source_for(indicators: list[Indicator], live_sources: list[LiveSourceFreshness], now: str) -> bool:
    asked_kinds = {indicator.kind for indicator in indicators}
    return any(is_feed_stale(source.last_sync_at, source.sync_interval_minutes, now)
               for source in live_sources if asked_kinds.intersection(source.indicator_kinds))


def check(store: ListStore, indicators: list[Indicator], live_sources: list[LiveSourceFreshness], now: str) -> CheckResult:
    """Hits for the indicators, with their attributions. A failed read is logged, counted and answered UNAVAILABLE with no
    hits; a stale live source of an asked kind makes the answer UNAVAILABLE with its hits kept. Never a clean default."""
    try:
        hits = find_list_hits(store, indicators, now)
    except ListLookupFailed as error:
        logger.error("scam-list check failed: %s", error)
        emit_count_metric(LIST_CHECK_FAILED_METRIC_NAME)
        return unavailable_check_result(now)
    if has_stale_source_for(indicators, live_sources, now):
        return CheckResult(hits, ScamListCoverageState.UNAVAILABLE, add_seconds_to_timestamp(now, UNAVAILABLE_CACHE_SECONDS))
    return CheckResult(hits, ScamListCoverageState.COMPLETE, add_seconds_to_timestamp(now, COMPLETE_CACHE_SECONDS))
