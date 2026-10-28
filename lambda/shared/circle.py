"""Growing the receiver's circle from the app: the Add-someone form is parsed at the boundary, the CONTACT# row is written
PENDING with every number and email as a reach point and a hashed one-time code. Each WhatsApp number is checked only when
that code comes back from it (or, with CONTACT_VERIFICATION_MOCK on, from the receiver's own labelled demo button); the row
turns verified on the first. A PENDING contact or number is never asked, so a typo can never send an ask to a stranger."""

import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import graph, reach_points
from shared.enums import ActionOutcome, Channel, Relationship, UserRole, VerifiedVia
from shared.org_ids import is_org_contact_sort_key
from shared.reach_points import ReachPoint, verified_via_of

CIRCLE_MAXIMUM_SIZE = 10
CONTACT_NAME_MAXIMUM_LENGTH = 40
WHATSAPP_NUMBERS_MAXIMUM_COUNT = 3
PHONE_NUMBERS_MAXIMUM_COUNT = 3
EMAIL_ADDRESSES_MAXIMUM_COUNT = 6
VERIFICATION_CODE_DIGITS = 6
VERIFICATION_CODE_LIFETIME = timedelta(hours=24)
E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")
EMAIL_ADDRESS_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
EMAIL_ADDRESS_MAXIMUM_LENGTH = 254
PROFILE_SORT_KEY = "PROFILE"
CODE_FIELDS = ("verificationCodeHash", "verificationExpiresAt")
TRANSACTION_CANCELLED = "TransactionCanceledException"
CONDITIONAL_CHECK_FAILED_REASON = "ConditionalCheckFailed"  # a transaction cancellation reason's Code
WHATSAPP_CODE_PARTITION_PREFIX = "WACODE#"  # WACODE#<E.164>/RECEIVER#<address>: which residents a number's code may answer

CONTACT_NOTE_TEMPLATE = (
    "{receiver_name} is using Pidge to catch fake emails. If something arrives in your name, Pidge will ask you first.\n"
    "Your code is {code}.{reply_line}"
)
PWA_LINK_LINE_TEMPLATE = "\nYour Pidge link: {pwa_link}"
WHATSAPP_REPLY_LINE_TEMPLATE = " Reply with it to Pidge on WhatsApp: https://wa.me/{pidge_digits}?text={code}"
WELCOME_TEXT_TEMPLATE = ("You're in {receiver_name}'s circle. When an email in your name asks {receiver_name} for something, "
                         "Pidge will ask you first.")


@dataclass(frozen=True)
class ContactForm:
    contact_name: str
    relationship: Relationship
    whatsapp_numbers: tuple[str, ...]  # at least one; each is asked once it sends the code back
    phone_numbers: tuple[str, ...]  # saved, never asked: no way to check a phone exists yet (inbound SMS, RCS off)
    email_addresses: tuple[str, ...]  # at least one; every one is asked and is an address they may write from

    @property
    def reach_points(self) -> tuple[ReachPoint, ...]:
        return (*(ReachPoint(Channel.WHATSAPP, number, VerifiedVia.PENDING) for number in self.whatsapp_numbers),
                *(ReachPoint(Channel.RCS, number, VerifiedVia.PENDING) for number in self.phone_numbers),
                *(ReachPoint(Channel.EMAIL, address, None) for address in self.email_addresses))


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
    return ContactForm(
        contact_name, relationship,
        parse_text_list(body, "whatsappNumbers", 1, WHATSAPP_NUMBERS_MAXIMUM_COUNT, parse_phone_number),
        parse_text_list(body, "phoneNumbers", 0, PHONE_NUMBERS_MAXIMUM_COUNT, parse_phone_number),
        parse_text_list(body, "emailAddresses", 1, EMAIL_ADDRESSES_MAXIMUM_COUNT, parse_email_address),
    )


def parse_text_list(body: dict, field_name: str, minimum_count: int, maximum_count: int, parse_one) -> tuple[str, ...]:
    """A required list of minimum..maximum entries, each parsed; an entry typed twice is kept once."""
    values = body.get(field_name)
    if not isinstance(values, list) or not minimum_count <= len(values) <= maximum_count:
        raise ValueError(f"{field_name} count")
    return tuple(dict.fromkeys(parse_one(value) for value in values))


def parse_phone_number(value) -> str:
    if not isinstance(value, str) or not E164_PATTERN.match(value.strip()):
        raise ValueError("number is not E.164")
    return value.strip()


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


def new_access_token(user_id: str) -> tuple[str, str, dict]:
    """A fresh PWA token, its sha256 hash and its TOKEN#<hash> row (what the socket checks)."""
    access_token = secrets.token_urlsafe(32)
    token_hash = graph.hash_access_token(access_token)
    return access_token, token_hash, {"PK": f"TOKEN#{token_hash}", "SK": graph.METADATA_SORT_KEY, "userId": user_id}


def access_token_rows(user_id: str, role: UserRole) -> tuple[str, list[dict]]:
    """A fresh PWA token for a new user and its two rows: TOKEN#<sha256> and USER#/PROFILE {tokenHash, role} (the hash, so a
    later rotation can delete it). The token itself is returned once; the caller alone decides where it goes."""
    access_token, token_hash, token_row = new_access_token(user_id)
    return access_token, [token_row, {"PK": f"USER#{user_id}", "SK": PROFILE_SORT_KEY, "tokenHash": token_hash, "role": str(role)}]


def issue_access_token(user_id: str) -> str:
    """Writes a fresh TOKEN# row for a user and sets tokenHash on USER#/PROFILE in place, so role, receiverEmail and orgId
    survive the rotation (scripts/seed_graph.py, scripts/enrol_resident.py); returns the token. Deleting the previous
    TOKEN# row is the caller's job."""
    access_token, token_hash, token_row = new_access_token(user_id)
    table = graph.get_table()
    table.put_item(Item=token_row)
    table.update_item(Key={"PK": f"USER#{user_id}", "SK": PROFILE_SORT_KEY}, UpdateExpression="SET tokenHash = :tokenHash",
                      ExpressionAttributeValues={":tokenHash": token_hash})
    return access_token


def names_of_row(contact_row: dict) -> tuple[str, ...]:
    """The names asks are matched by (graph.find_contact_among): contactName, plus nameVariants on the home office's row,
    which must carry them (KeyError when absent)."""
    if is_org_contact_sort_key(contact_row["SK"]):
        return (contact_row["contactName"], *contact_row["nameVariants"])
    return (contact_row["contactName"],)


def find_same_name_row(contact_rows: list[dict], contact_name: str) -> dict | None:
    """The row one of whose names (names_of_row) is this name, case and spacing ignored; None when no row has it."""
    wanted_name = graph.normalize_person_name(contact_name)
    return next((row for row in contact_rows
                 if any(graph.normalize_person_name(name) == wanted_name for name in names_of_row(row))), None)


def enrol_contact(receiver_address: str, form: ContactForm, now: datetime) -> Enrolment:
    """Writes the PENDING row with a server-made id, a hashed six-digit code, a PWA token for the in-app channel and a WACODE#
    row per WhatsApp number, in one transaction (no row without its token, no token without its row). Adding a name that is still PENDING re-issues it: the
    row takes the new form, a new code and a new token, and the old token dies, so a lost note or an expired code is recovered
    by adding the person again. A name a verified row holds (names_of_row: the office's name variants too) is refused, as asks
    are matched by name, and so is a full circle; both write nothing."""
    contact_rows = graph.list_contact_rows(receiver_address)
    same_name_row = find_same_name_row(contact_rows, form.contact_name)
    if same_name_row is not None and verified_via_of(same_name_row) is not VerifiedVia.PENDING:
        return Enrolment(ActionOutcome.DUPLICATE_NAME)
    if same_name_row is None and len(contact_rows) >= CIRCLE_MAXIMUM_SIZE:
        return Enrolment(ActionOutcome.CIRCLE_FULL)
    contact_user_id = same_name_row["contactPwaUserId"] if same_name_row is not None else secrets.token_hex(8)
    code = new_verification_code()
    contact_row = {
        "PK": f"RECEIVER#{receiver_address}",
        "SK": f"CONTACT#{contact_user_id}",
        "contactName": form.contact_name,
        "relationship": str(form.relationship),
        "contactPwaUserId": contact_user_id,
        "contactReachPoints": [reach_points.item_of(point) for point in form.reach_points],
        "verifiedVia": str(VerifiedVia.PENDING),
        "verificationCodeHash": graph.hash_access_token(code),
        "verificationExpiresAt": graph.iso_timestamp(now + VERIFICATION_CODE_LIFETIME),
        "addedAt": same_name_row["addedAt"] if same_name_row is not None else graph.iso_timestamp(now),
    }
    access_token, token_rows = access_token_rows(contact_user_id, UserRole.CONTACT)
    table = graph.get_table()
    # The table resource's client takes plain Python values (boto3 converts them), unlike a bare dynamodb client.
    row_condition = ({"ConditionExpression": "attribute_not_exists(PK)"} if same_name_row is None else
                     {"ConditionExpression": "verifiedVia = :pending", "ExpressionAttributeValues": {":pending": str(VerifiedVia.PENDING)}})
    transaction_items = [
        {"Put": {"TableName": table.name, "Item": contact_row, **row_condition}},
        *({"Put": {"TableName": table.name, "Item": row}} for row in token_rows),
        *dead_token_deletions(table.name, contact_user_id, same_name_row),
        *whatsapp_code_index_puts(table.name, receiver_address, contact_row),
    ]
    try:
        table.meta.client.transact_write_items(TransactItems=transaction_items)
    except ClientError as error:
        if error.response["Error"]["Code"] == TRANSACTION_CANCELLED:
            return Enrolment(ActionOutcome.NOT_APPLIED)  # a 64-bit id collision, or the row was verified meanwhile; try again
        raise
    return Enrolment(ActionOutcome.RECORDED, contact_row, code, access_token)


def edit_contact(receiver_address: str, contact_user_id: str, form: ContactForm, now: datetime) -> Enrolment:
    """Saves the edited form on the contact's row in place, as a contactReachPoints list. A point already on the row keeps
    its stored entry, so a checked number keeps being asked; a new WhatsApp number waits for a fresh code (one per edit, for
    every number still waiting), returned once. The row and a WACODE# row per number still waiting are written in one
    transaction. Refused, writing nothing: an unknown contact, the home office's CONTACT#org-<orgId> row (NOT_ALLOWED: only
    orgs.org_contact_row builds it), a name another row has (names_of_row), an edit that drops every checked WhatsApp
    number, and a row whose points, check state or code changed since it was read (two edits that change only the name or
    relationship: the last one wins)."""
    contact_rows = graph.list_contact_rows(receiver_address)
    contact_row = next((row for row in contact_rows if row["contactPwaUserId"] == contact_user_id), None)
    if contact_row is None:
        return Enrolment(ActionOutcome.NOT_APPLIED)
    if is_org_contact_sort_key(contact_row["SK"]):
        return Enrolment(ActionOutcome.NOT_ALLOWED)
    same_name_row = find_same_name_row(contact_rows, form.contact_name)
    if same_name_row is not None and same_name_row["SK"] != contact_row["SK"]:
        return Enrolment(ActionOutcome.DUPLICATE_NAME)
    stored_items = reach_points.stored_items_by_point(contact_row)
    edited_items = [stored_items[(point.channel, point.address)] if (point.channel, point.address) in stored_items
                    else reach_points.item_of(point) for point in form.reach_points]
    edited_points = tuple(reach_points.reach_point_from_item(item) for item in edited_items)
    had_checked_number = reach_points.has_checked_whatsapp_number(reach_points.reach_points_of(contact_row))
    if had_checked_number and not reach_points.has_checked_whatsapp_number(edited_points):
        return Enrolment(ActionOutcome.LAST_CHECKED_NUMBER)
    dropped_fields = (*reach_points.SINGLE_POINT_ROW_FIELDS, *CODE_FIELDS)
    edited_row = {name: value for name, value in contact_row.items() if name not in dropped_fields}
    edited_row.update({"contactName": form.contact_name, "relationship": str(form.relationship), "contactReachPoints": edited_items})
    code = code_for_edit(contact_row, edited_row, stored_items, form, now)
    table = graph.get_table()
    transaction_items = [
        {"Put": {"TableName": table.name, "Item": edited_row, **unchanged_since_read_condition(contact_row)}},
        *whatsapp_code_index_puts(table.name, receiver_address, edited_row),
    ]
    try:
        table.meta.client.transact_write_items(TransactItems=transaction_items)
    except ClientError as error:
        if error.response["Error"]["Code"] == TRANSACTION_CANCELLED:
            return Enrolment(ActionOutcome.NOT_APPLIED)  # a number was checked or the row edited meanwhile; try again
        raise
    return Enrolment(ActionOutcome.RECORDED, edited_row, code)


def code_for_edit(contact_row: dict, edited_row: dict, stored_items: dict, form: ContactForm, now: datetime) -> str | None:
    """Puts the code fields on the edited row while a WhatsApp number waits: the row's code while it is live and the edit
    adds no number; else a fresh code (returned), so saving again renews an expired one. None once nothing waits."""
    if reach_points.pending_whatsapp_index(edited_row, None) is None:
        return None
    is_number_added = any(point.channel is Channel.WHATSAPP and (point.channel, point.address) not in stored_items
                          for point in form.reach_points)
    if not is_number_added and is_code_live(contact_row, now):
        edited_row.update({field_name: contact_row[field_name] for field_name in CODE_FIELDS})
        return None
    code = new_verification_code()
    edited_row.update({"verificationCodeHash": graph.hash_access_token(code),
                       "verificationExpiresAt": graph.iso_timestamp(now + VERIFICATION_CODE_LIFETIME)})
    return code


def new_verification_code() -> str:
    return f"{secrets.randbelow(10 ** VERIFICATION_CODE_DIGITS):0{VERIFICATION_CODE_DIGITS}d}"


def is_code_live(contact_row: dict, now: datetime) -> bool:
    """The row holds a code that has not expired."""
    return "verificationCodeHash" in contact_row and (
        datetime.fromisoformat(contact_row["verificationExpiresAt"].replace("Z", "+00:00")) >= now.astimezone(timezone.utc))


def unchanged_since_read_condition(contact_row: dict) -> dict:
    """The put applies only to the row as it was read: same points, same check state, same code."""
    clauses, values = ["attribute_exists(PK)"], {}
    for field_name in ("contactReachPoints", "verifiedVia", "verificationCodeHash"):
        if field_name in contact_row:
            clauses.append(f"{field_name} = :read{field_name[0].upper()}{field_name[1:]}")
            values[f":read{field_name[0].upper()}{field_name[1:]}"] = contact_row[field_name]
        else:
            clauses.append(f"attribute_not_exists({field_name})")
    return {"ConditionExpression": " AND ".join(clauses), **({"ExpressionAttributeValues": values} if values else {})}


def is_first_item_condition_failed(error: ClientError) -> bool:
    """A transaction cancelled because its first item's condition failed; False for any other cancellation reason (a
    conflict, a throttle) and any other error."""
    return (error.response["Error"]["Code"] == TRANSACTION_CANCELLED and "CancellationReasons" in error.response
            and error.response["CancellationReasons"][0]["Code"] == CONDITIONAL_CHECK_FAILED_REASON)


def dead_token_deletions(table_name: str, contact_user_id: str, same_name_row: dict | None) -> list[dict]:
    """On a re-issue, the previous token row is deleted in the same transaction, so the old link stops opening the PWA."""
    if same_name_row is None:
        return []
    profile = graph.get_table().get_item(Key={"PK": f"USER#{contact_user_id}", "SK": PROFILE_SORT_KEY}).get("Item")
    if profile is None:
        return []
    return [{"Delete": {"TableName": table_name, "Key": {"PK": f"TOKEN#{profile['tokenHash']}", "SK": graph.METADATA_SORT_KEY}}}]


def build_contact_note(receiver_name: str, code: str, pwa_link: str | None, pidge_whatsapp_number: str | None) -> str:
    """The note the receiver sends from her own phone. The WhatsApp reply line needs Pidge's own number; without it the
    note carries the code and the link only (then only the labelled demo can verify). An edit's note has no link: the contact
    already has one, and a new token would end it."""
    reply_line = ""
    if pidge_whatsapp_number:
        reply_line = WHATSAPP_REPLY_LINE_TEMPLATE.format(pidge_digits=pidge_whatsapp_number.removeprefix("+"), code=code)
    note_text = CONTACT_NOTE_TEMPLATE.format(receiver_name=receiver_name, code=code, reply_line=reply_line)
    return note_text + (PWA_LINK_LINE_TEMPLATE.format(pwa_link=pwa_link) if pwa_link is not None else "")


def build_welcome_text(receiver_name: str) -> str:
    return WELCOME_TEXT_TEMPLATE.format(receiver_name=receiver_name)


def whatsapp_code_index_puts(table_name: str, receiver_address: str, contact_row: dict) -> list[dict]:
    """A WACODE#<number>/RECEIVER#<address> Put for every WhatsApp number on the row still waiting for its code, living as
    long as the code; none when the row holds no code."""
    if "verificationCodeHash" not in contact_row:
        return []
    expiry = datetime.fromisoformat(contact_row["verificationExpiresAt"].replace("Z", "+00:00"))
    waiting_numbers = [point.address for point in reach_points.reach_points_of(contact_row)
                       if point.channel is Channel.WHATSAPP and point.verified_via is VerifiedVia.PENDING]
    return [{"Put": {"TableName": table_name, "Item": {
        "PK": f"{WHATSAPP_CODE_PARTITION_PREFIX}{number}", "SK": f"RECEIVER#{receiver_address}", "receiverEmail": receiver_address,
        "contactSK": contact_row["SK"], "ttlEpochSeconds": int(expiry.timestamp()),
    }}} for number in waiting_numbers]


def list_receivers_waiting_on_number(whatsapp_number: str) -> list[str]:
    """The receiver addresses with a live WACODE# row for this number (TTL deletion lags, so an expired row is skipped)."""
    now_epoch_seconds = int(datetime.now(timezone.utc).timestamp())
    response = graph.get_table().query(KeyConditionExpression=Key("PK").eq(f"{WHATSAPP_CODE_PARTITION_PREFIX}{whatsapp_number}"))
    return [row["receiverEmail"] for row in response["Items"] if int(row["ttlEpochSeconds"]) > now_epoch_seconds]


def find_pending_contact_by_whatsapp(receiver_address: str, sender_number: str) -> dict | None:
    """The row on which this number still waits for its code (the row itself may already be verified by another number)."""
    return next((row for row in graph.list_contact_rows(receiver_address)
                 if reach_points.pending_whatsapp_index(row, sender_number) is not None), None)


def verify_with_code(receiver_address: str, contact_row: dict, code: str, verified_via: VerifiedVia, now: datetime,
                     whatsapp_number: str | None) -> bool:
    """True when the row's code is live and `code` matches (constant time) and `whatsapp_number` still waits for it (None, the
    demo button: the first number waiting); that number turns checked, and the row verified on its first. False writes nothing."""
    if not is_code_live(contact_row, now):
        return False
    if not hmac.compare_digest(graph.hash_access_token(code.strip()), contact_row["verificationCodeHash"]):
        return False
    if "contactReachPoints" not in contact_row:  # a slice-10c row: its one number is checked with the row
        return verified_via_of(contact_row) is VerifiedVia.PENDING and graph.mark_contact_verified(
            receiver_address, contact_row["SK"], verified_via, graph.iso_timestamp(now), contact_row["verificationCodeHash"])
    point_index = reach_points.pending_whatsapp_index(contact_row, whatsapp_number)
    return point_index is not None and mark_whatsapp_number_checked(
        receiver_address, contact_row, point_index, verified_via, graph.iso_timestamp(now))


def mark_whatsapp_number_checked(receiver_address: str, contact_row: dict, point_index: int, verified_via: VerifiedVia,
                                 verified_at: str) -> bool:
    """Checks one waiting WhatsApp number in place, only while the row still holds the code it was read with and that index
    is still that waiting number; the row turns verified on its first number, and the code is dropped once none is left
    waiting. False when any of that changed meanwhile (a redelivered text, the demo button racing the real reply)."""
    points = reach_points.reach_points_of(contact_row)
    point_path = f"contactReachPoints[{point_index}]"
    is_row_pending = verified_via_of(contact_row) is VerifiedVia.PENDING
    waiting_count = sum(1 for point in points if point.channel is Channel.WHATSAPP and point.verified_via is VerifiedVia.PENDING)
    set_clauses = [f"{point_path}.verifiedVia = :via", f"{point_path}.verifiedAt = :at"]
    if is_row_pending:
        set_clauses += ["verifiedVia = :via", "verifiedAt = :at"]
    try:
        graph.get_table().update_item(
            Key={"PK": f"RECEIVER#{receiver_address}", "SK": contact_row["SK"]},
            UpdateExpression="SET " + ", ".join(set_clauses)
                             + (" REMOVE verificationCodeHash, verificationExpiresAt" if waiting_count == 1 else ""),
            ConditionExpression=(f"attribute_exists(PK) AND verificationCodeHash = :hash AND {point_path}.address = :number "
                                 f"AND {point_path}.verifiedVia = :pending AND verifiedVia {'=' if is_row_pending else '<>'} :pending"),
            ExpressionAttributeValues={":via": str(verified_via), ":at": verified_at, ":pending": str(VerifiedVia.PENDING),
                                       ":hash": contact_row["verificationCodeHash"], ":number": points[point_index].address},
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == graph.CONDITIONAL_CHECK_FAILED:
            return False
        raise
