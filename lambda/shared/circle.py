"""Growing the receiver's circle from the app: the Add-someone form is parsed at the boundary, the CONTACT# row is written
PENDING with a hashed one-time code, and the row turns verified only when that code comes back from the contact's own
WhatsApp number (or, with CONTACT_VERIFICATION_MOCK on, from the receiver's own labelled demo button). A PENDING contact is
never asked (graph.list_contacts leaves it out), so a typo in the number can never send an ask to a stranger."""

import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from shared import graph
from shared.enums import ActionOutcome, Relationship, VerifiedVia
from shared.graph import verified_via_of

CIRCLE_MAXIMUM_SIZE = 10
CONTACT_NAME_MAXIMUM_LENGTH = 40
EXTRA_ADDRESSES_MAXIMUM_COUNT = 5
VERIFICATION_CODE_DIGITS = 6
VERIFICATION_CODE_LIFETIME = timedelta(hours=24)
E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")
EMAIL_ADDRESS_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
EMAIL_ADDRESS_MAXIMUM_LENGTH = 254
PROFILE_SORT_KEY = "PROFILE"
TRANSACTION_CANCELLED = "TransactionCanceledException"

CONTACT_NOTE_TEMPLATE = (
    "{receiver_name} is using Pidge to catch fake emails. If something arrives in your name, Pidge will ask you first.\n"
    "Your code is {code}.{reply_line}\n"
    "Your Pidge link: {pwa_link}"
)
WHATSAPP_REPLY_LINE_TEMPLATE = " Reply with it to Pidge on WhatsApp: https://wa.me/{pidge_digits}?text={code}"
WELCOME_TEXT_TEMPLATE = ("You're in {receiver_name}'s circle. When an email in your name asks {receiver_name} for something, "
                         "Pidge will ask you first.")


@dataclass(frozen=True)
class ContactForm:
    contact_name: str
    relationship: Relationship
    whatsapp_number: str
    email_address: str
    extra_addresses: tuple[str, ...]

    @property
    def known_addresses(self) -> list[str]:
        return list(dict.fromkeys((self.email_address, *self.extra_addresses)))


@dataclass(frozen=True)
class Enrolment:
    outcome: ActionOutcome
    contact_row: dict | None = None
    code: str | None = None
    access_token: str | None = None


def parse_contact_form(body: dict) -> ContactForm:
    """The form exactly as typed, checked field by field; raises ValueError on any shape the row must not hold."""
    contact_name = " ".join(require_text(body, "contactName").split())
    if not 1 <= len(contact_name) <= CONTACT_NAME_MAXIMUM_LENGTH:
        raise ValueError("contactName length")
    try:
        relationship = Relationship(require_text(body, "relationship"))
    except ValueError:
        raise ValueError("relationship") from None  # never echo the typed value into a log
    whatsapp_number = require_text(body, "whatsappNumber").strip()
    if not E164_PATTERN.match(whatsapp_number):
        raise ValueError("whatsappNumber is not E.164")
    extra_addresses = body.get("extraAddresses", [])
    if not isinstance(extra_addresses, list) or len(extra_addresses) > EXTRA_ADDRESSES_MAXIMUM_COUNT:
        raise ValueError("extraAddresses")
    return ContactForm(contact_name, relationship, whatsapp_number, parse_email_address(require_text(body, "emailAddress")),
                       tuple(parse_email_address(address) for address in extra_addresses))


def require_text(body: dict, field_name: str) -> str:
    value = body.get(field_name)
    if not isinstance(value, str):
        raise ValueError(f"{field_name} missing")
    return value


def parse_email_address(value) -> str:
    if not isinstance(value, str):
        raise ValueError("address is not text")
    address = value.strip().casefold()
    if len(address) > EMAIL_ADDRESS_MAXIMUM_LENGTH or not EMAIL_ADDRESS_PATTERN.match(address):
        raise ValueError("address is not an email address")
    return address


def access_token_rows(user_id: str) -> tuple[str, list[dict]]:
    """A fresh PWA token for one user and its two rows: TOKEN#<sha256> (what the socket checks) and USER#/PROFILE (remembers
    the hash so a later rotation can delete it). The token itself is returned once; the caller alone decides where it goes."""
    access_token = secrets.token_urlsafe(32)
    token_hash = graph.hash_access_token(access_token)
    return access_token, [
        {"PK": f"TOKEN#{token_hash}", "SK": graph.METADATA_SORT_KEY, "userId": user_id},
        {"PK": f"USER#{user_id}", "SK": PROFILE_SORT_KEY, "tokenHash": token_hash},
    ]


def issue_access_token(user_id: str) -> str:
    """Writes a fresh token's rows for an existing user (scripts/seed_graph.py); returns the token."""
    access_token, rows = access_token_rows(user_id)
    for row in rows:
        graph.get_table().put_item(Item=row)
    return access_token


def find_same_name_row(contact_rows: list[dict], contact_name: str) -> dict | None:
    wanted_name = contact_name.casefold()
    return next((row for row in contact_rows if " ".join(row["contactName"].split()).casefold() == wanted_name), None)


def enrol_contact(receiver_address: str, form: ContactForm, now: datetime) -> Enrolment:
    """Writes the PENDING row with a server-made id, a hashed six-digit code and a PWA token for the in-app channel, in one
    transaction (no row without its token, no token without its row). Adding a name that is still PENDING re-issues it: the
    row takes the new form, a new code and a new token, and the old token dies, so a lost note or an expired code is recovered
    by adding the person again. A verified name is refused (asks are matched by name), as is a full circle; both write nothing."""
    contact_rows = graph.list_contact_rows(receiver_address)
    same_name_row = find_same_name_row(contact_rows, form.contact_name)
    if same_name_row is not None and verified_via_of(same_name_row) is not VerifiedVia.PENDING:
        return Enrolment(ActionOutcome.DUPLICATE_NAME)
    if same_name_row is None and len(contact_rows) >= CIRCLE_MAXIMUM_SIZE:
        return Enrolment(ActionOutcome.CIRCLE_FULL)
    contact_user_id = same_name_row["contactPwaUserId"] if same_name_row is not None else secrets.token_hex(8)
    code = f"{secrets.randbelow(10 ** VERIFICATION_CODE_DIGITS):0{VERIFICATION_CODE_DIGITS}d}"
    contact_row = {
        "PK": f"RECEIVER#{receiver_address}",
        "SK": f"CONTACT#{contact_user_id}",
        "contactName": form.contact_name,
        "relationship": str(form.relationship),
        "contactPwaUserId": contact_user_id,
        "contactWhatsApp": form.whatsapp_number,
        "contactEmail": form.email_address,
        "knownAddresses": form.known_addresses,
        "verifiedVia": str(VerifiedVia.PENDING),
        "verificationCodeHash": graph.hash_access_token(code),
        "verificationExpiresAt": graph.iso_timestamp(now + VERIFICATION_CODE_LIFETIME),
        "addedAt": same_name_row["addedAt"] if same_name_row is not None else graph.iso_timestamp(now),
    }
    access_token, token_rows = access_token_rows(contact_user_id)
    table = graph.get_table()
    # The table resource's client takes plain Python values (boto3 converts them), unlike a bare dynamodb client.
    row_condition = ({"ConditionExpression": "attribute_not_exists(PK)"} if same_name_row is None else
                     {"ConditionExpression": "verifiedVia = :pending", "ExpressionAttributeValues": {":pending": str(VerifiedVia.PENDING)}})
    transaction_items = [
        {"Put": {"TableName": table.name, "Item": contact_row, **row_condition}},
        *({"Put": {"TableName": table.name, "Item": row}} for row in token_rows),
        *dead_token_deletions(table.name, contact_user_id, same_name_row),
    ]
    try:
        table.meta.client.transact_write_items(TransactItems=transaction_items)
    except ClientError as error:
        if error.response["Error"]["Code"] == TRANSACTION_CANCELLED:
            return Enrolment(ActionOutcome.NOT_APPLIED)  # a 64-bit id collision, or the row was verified meanwhile; try again
        raise
    return Enrolment(ActionOutcome.RECORDED, contact_row, code, access_token)


def dead_token_deletions(table_name: str, contact_user_id: str, same_name_row: dict | None) -> list[dict]:
    """On a re-issue, the previous token row is deleted in the same transaction, so the old link stops opening the PWA."""
    if same_name_row is None:
        return []
    profile = graph.get_table().get_item(Key={"PK": f"USER#{contact_user_id}", "SK": PROFILE_SORT_KEY}).get("Item")
    if profile is None:
        return []
    return [{"Delete": {"TableName": table_name, "Key": {"PK": f"TOKEN#{profile['tokenHash']}", "SK": graph.METADATA_SORT_KEY}}}]


def build_contact_note(receiver_name: str, code: str, pwa_link: str, pidge_whatsapp_number: str | None) -> str:
    """The note the receiver sends from her own phone. The WhatsApp reply line needs Pidge's own number; without it the
    note carries the code and the link only (then only the labelled demo can verify)."""
    reply_line = ""
    if pidge_whatsapp_number:
        reply_line = WHATSAPP_REPLY_LINE_TEMPLATE.format(pidge_digits=pidge_whatsapp_number.removeprefix("+"), code=code)
    return CONTACT_NOTE_TEMPLATE.format(receiver_name=receiver_name, code=code, reply_line=reply_line, pwa_link=pwa_link)


def build_welcome_text(receiver_name: str) -> str:
    return WELCOME_TEXT_TEMPLATE.format(receiver_name=receiver_name)


def find_pending_contact_by_whatsapp(receiver_address: str, sender_number: str) -> dict | None:
    return next((row for row in graph.list_contact_rows(receiver_address)
                 if verified_via_of(row) is VerifiedVia.PENDING and row.get("contactWhatsApp") == sender_number), None)


def verify_with_code(receiver_address: str, contact_row: dict, code: str, verified_via: VerifiedVia, now: datetime) -> bool:
    """True when the row is still PENDING, its code has not expired and `code` matches (constant time); the row then turns
    verified in place. False writes nothing."""
    if verified_via_of(contact_row) is not VerifiedVia.PENDING or "verificationCodeHash" not in contact_row:
        return False
    if datetime.fromisoformat(contact_row["verificationExpiresAt"].replace("Z", "+00:00")) < now.astimezone(timezone.utc):
        return False
    if not hmac.compare_digest(graph.hash_access_token(code.strip()), contact_row["verificationCodeHash"]):
        return False
    return graph.mark_contact_verified(receiver_address, contact_row["SK"], verified_via, graph.iso_timestamp(now),
                                       contact_row["verificationCodeHash"])
