"""The scam list: LIST#<kind> rows read from the same table, turned into reasons. Policy code decides what a reason means.

A hit is a reason, never a verdict. Rows in the POC are hand-written samples (source: sample) plus the tier-0 rows that
pidge-verdict writes on Confirm (source: pidge-confirmed). Real feed data never lives in the repo.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from botocore.exceptions import ClientError

from shared import graph
from shared.email_parse import registrable_domain_of_host
from shared.enums import IndicatorKind, ListCategory, ListSource, ListStrength, ReasonCode

logger = logging.getLogger(__name__)

LIST_PARTITION_PREFIX = "LIST#"
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


def parse_list_row(row: dict) -> ListRow:
    partition = str(row["PK"])
    if not partition.startswith(LIST_PARTITION_PREFIX):
        raise ValueError(f"not a LIST# row: {partition}")
    return ListRow(
        kind=IndicatorKind(partition[len(LIST_PARTITION_PREFIX):]), key=str(row["SK"]),
        category=ListCategory(row["category"]), strength=ListStrength(row["strength"]), source=ListSource(row["source"]),
        tier=int(row["tier"]), reason=str(row["reason"]), contact_name=row.get("contactName"),
    )


def list_row_from_seed(seed: dict) -> dict:
    """The stored shape of a sample row; parse_list_row must accept the result before it is written."""
    kind = IndicatorKind(seed["kind"])
    key = {IndicatorKind.DOMAIN: normalise_registrable_domain, IndicatorKind.ADDRESS: str.casefold,
           IndicatorKind.LOOKALIKE: confusable_skeleton, IndicatorKind.PHONE: normalise_phone_e164,
           IndicatorKind.URL: str.casefold}[kind](seed["value"])
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


def lookup_sender_reasons(
    store: ListStore, sender_address: str, sender_domain: str, sender_name: str, contacts: list[graph.Contact],
) -> list[ReasonCode]:
    """Address row, domain row, lookalike row, then code-only lookalikes against the family. Raises ListLookupFailed."""
    reasons: list[ReasonCode] = []
    address_row = store.get_row(IndicatorKind.ADDRESS, sender_address.casefold())
    if address_row is not None:
        is_reported = parse_list_row(address_row).source is ListSource.PIDGE_CONFIRMED
        reasons.append(ReasonCode.REPORTED_SENDER if is_reported else ReasonCode.LISTED_SENDER_ADDRESS)
    registrable_domain = normalise_registrable_domain(sender_domain) if sender_domain else ""
    if registrable_domain and store.get_row(IndicatorKind.DOMAIN, registrable_domain) is not None:
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
    lookalike_row = store.get_row(IndicatorKind.LOOKALIKE, confusable_skeleton(sender_name))
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
