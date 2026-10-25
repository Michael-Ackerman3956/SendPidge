"""DynamoDB access: contacts, access tokens, WebSocket connections, emails and verification requests."""

import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared.enums import (
    CaseState, Channel, Friction, ListCategory, ListSource, ListStrength, PolicyDecision, ReasonCode, RequestState, VerifiedVia,
    WarnReason,
)

# An answer is accepted for one re-ask interval; pidge-retry extends it by another and marks timeout on attempt 2.
HOLD_DURATION_SECONDS = int(os.environ.get("RE_ASK_DELAY_SECONDS", "600"))
REQUEST_RETENTION_SECONDS = 30 * 24 * 3600
EMAIL_RETENTION_SECONDS = 90 * 24 * 3600
CONNECTION_RETENTION_SECONDS = 24 * 3600
HOURLY_COUNTER_RETENTION_SECONDS = 2 * 3600
ASK_COUNTER_PARTITION_PREFIX = "ASKRATE#"
HOUR_SORT_KEY_PREFIX = "HOUR#"
CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailedException"
logger = logging.getLogger(__name__)
METADATA_SORT_KEY = "META"
# UserIndex (GSI): "<userId>#EMAIL" or "<userId>#REQUEST" → rows newest first, for the PWA's sync.
USER_INDEX_NAME = "UserIndex"
USER_INDEX_PARTITION_KEY = "userIndexPK"
USER_INDEX_SORT_KEY = "userIndexSK"
EMAIL_ROW_KIND = "EMAIL"
REQUEST_ROW_KIND = "REQUEST"
NOTE_ROW_KIND = "NOTE"
CASE_ROW_KIND = "CASE"
NOTE_SORT_KEY_PREFIX = "NOTE#"
CASE_REPLY_SORT_KEY_PREFIX = "REPLY#"
CASE_NOTE_WRITER = "case_agent"

_table = None


def get_table():
    """The single 'sendpidge' table, created once per Lambda container."""
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def hash_access_token(access_token: str) -> str:
    """Only the hash is stored, so a table read never reveals a usable token."""
    return hashlib.sha256(access_token.encode()).hexdigest()


def retention_epoch(retention_seconds: int) -> int:
    return int(time.time()) + retention_seconds


def iso_timestamp(moment: datetime) -> str:
    """Fixed-width UTC 'YYYY-MM-DDTHH:MM:SSZ', so stored times compare correctly as strings."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def user_index_partition(user_id: str, row_kind: str) -> str:
    return f"{user_id}#{row_kind}"


@dataclass(frozen=True)
class Contact:
    contact_key: str
    contact_name: str
    contact_pwa_user_id: str
    contact_rcs: str | None = None  # E.164, exactly as RCS reports originationNumber
    contact_whatsapp: str | None = None  # E.164 with "+"; whatsapp.parse_inbound_reply adds the "+" Meta omits
    known_addresses: tuple[str, ...] = ()  # CONTACT#.knownAddresses, lower-cased; gate 1 compares the sender address here

    def is_known_address(self, sender_address: str) -> bool:
        return sender_address.casefold() in self.known_addresses


def find_user_id_for_access_token(access_token: str) -> str | None:
    item = get_table().get_item(
        Key={"PK": f"TOKEN#{hash_access_token(access_token)}", "SK": METADATA_SORT_KEY}
    ).get("Item")
    return item["userId"] if item else None


def register_connection(connection_id: str, user_id: str) -> None:
    """Two rows: by connection (for $disconnect) and by user (for pushes)."""
    expiry = retention_epoch(CONNECTION_RETENTION_SECONDS)
    table = get_table()
    table.put_item(Item={"PK": f"CONN#{connection_id}", "SK": METADATA_SORT_KEY, "userId": user_id,
                         "ttlEpochSeconds": expiry})
    table.put_item(Item={"PK": f"USER#{user_id}", "SK": f"CONN#{connection_id}", "ttlEpochSeconds": expiry})


def remove_connection(connection_id: str) -> None:
    table = get_table()
    item = table.get_item(Key={"PK": f"CONN#{connection_id}", "SK": METADATA_SORT_KEY}).get("Item")
    table.delete_item(Key={"PK": f"CONN#{connection_id}", "SK": METADATA_SORT_KEY})
    if item:
        table.delete_item(Key={"PK": f"USER#{item['userId']}", "SK": f"CONN#{connection_id}"})


def list_connection_ids(user_id: str) -> list[str]:
    response = get_table().query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("CONN#")
    )
    return [item["SK"].removeprefix("CONN#") for item in response["Items"]]


def contact_from_row(item: dict) -> Contact:
    return Contact(
        item["SK"], item["contactName"], item["contactPwaUserId"], item.get("contactRcs"), item.get("contactWhatsApp"),
        tuple(str(address).casefold() for address in item.get("knownAddresses", [])),
    )


def find_contact(receiver_address: str, person_name: str | None) -> Contact | None:
    """Exact name match (case and spacing ignored) against the receiver's own contacts.

    Called with the sender's display name for gate 1 and with the Reader's claimed_person for gate 2.
    """
    return find_contact_among(list_contacts(receiver_address), person_name)


def find_contact_among(contacts: list[Contact], person_name: str | None) -> Contact | None:
    """The same rule over an already-fetched contact list, so one email costs one contacts query."""
    if not person_name:
        return None
    wanted_name = " ".join(person_name.split()).casefold()
    return next((contact for contact in contacts if " ".join(contact.contact_name.split()).casefold() == wanted_name), None)


def list_contacts(receiver_address: str) -> list[Contact]:
    return [contact_from_row(item) for item in list_contact_rows(receiver_address)]


def list_contact_rows(receiver_address: str) -> list[dict]:
    """The receiver's raw CONTACT# rows (numbers and addresses included: callers decide what leaves the server)."""
    response = get_table().query(
        KeyConditionExpression=Key("PK").eq(f"RECEIVER#{receiver_address}") & Key("SK").begins_with("CONTACT#")
    )
    return response["Items"]


def find_contact_row_for_user(receiver_address: str, contact_user_id: str) -> dict | None:
    """The receiver's own CONTACT# row whose contactPwaUserId matches; the sort key is never built from the caller's text."""
    return next((row for row in list_contact_rows(receiver_address) if row["contactPwaUserId"] == contact_user_id), None)


def mark_contact_verified(receiver_address: str, contact_sort_key: str, verified_via: VerifiedVia, verified_at: str) -> bool:
    """Sets verifiedVia and verifiedAt on an existing CONTACT# row (never a put: the row holds the numbers); False if it is gone."""
    try:
        get_table().update_item(
            Key={"PK": f"RECEIVER#{receiver_address}", "SK": contact_sort_key},
            UpdateExpression="SET verifiedVia = :via, verifiedAt = :at",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={":via": str(verified_via), ":at": verified_at},
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == CONDITIONAL_CHECK_FAILED:
            return False
        raise


def contact_domains(receiver_address: str) -> frozenset[str]:
    """The domains the family writes from (from every contact's knownAddresses); link lookalikes are judged against these."""
    return domains_of(list_contacts(receiver_address))


def domains_of(contacts: list[Contact]) -> frozenset[str]:
    return frozenset(address.rpartition("@")[2] for contact in contacts for address in contact.known_addresses if "@" in address)


def current_hour_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def take_hourly_slot(partition_key: str, limit: int) -> bool:
    """One conditional increment of this hour's counter under the partition. False at the cap or on any write failure (fail closed)."""
    try:
        get_table().update_item(
            Key={"PK": partition_key, "SK": f"{HOUR_SORT_KEY_PREFIX}{current_hour_label()}"},
            UpdateExpression="ADD slotCount :one SET ttlEpochSeconds = :expiry",
            ConditionExpression="attribute_not_exists(slotCount) OR slotCount < :limit",
            ExpressionAttributeValues={":one": 1, ":limit": limit, ":expiry": retention_epoch(HOURLY_COUNTER_RETENTION_SECONDS)},
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] != CONDITIONAL_CHECK_FAILED:
            logger.error("hourly counter write failed for %s; refusing: %s", partition_key, error)
        return False


def take_ask_slot(contact_key: str, asks_per_hour_limit: int) -> bool:
    """The ask cap: a contact is asked at most this many times an hour; past it the hold stays and nothing is sent."""
    return take_hourly_slot(f"{ASK_COUNTER_PARTITION_PREFIX}{contact_key}", asks_per_hour_limit)


def record_ask_capped(request_id: str) -> None:
    get_table().update_item(
        Key={"PK": f"REQUEST#{request_id}", "SK": METADATA_SORT_KEY},
        UpdateExpression="SET channelsSent = :none, askCapped = :true",
        ExpressionAttributeValues={":none": [], ":true": True},
    )


def claim_email(email_id: str, email_fields: dict) -> bool:
    """Writes EMAIL#<id> once; False means this S3 event was already processed."""
    try:
        get_table().put_item(
            Item={"PK": f"EMAIL#{email_id}", "SK": METADATA_SORT_KEY, **email_fields,
                  "ttlEpochSeconds": retention_epoch(EMAIL_RETENTION_SECONDS)},
            ConditionExpression="attribute_not_exists(PK)",
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == CONDITIONAL_CHECK_FAILED:
            return False
        raise


def record_email_outcome(
    email_id: str,
    decision: PolicyDecision,
    warn_reason: WarnReason | None,
    reasons: tuple[ReasonCode, ...],
    request_id: str | None,
) -> None:
    get_table().update_item(
        Key={"PK": f"EMAIL#{email_id}", "SK": METADATA_SORT_KEY},
        UpdateExpression="SET #state = :state, warnReason = :warnReason, reasons = :reasons, requestId = :requestId",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={
            ":state": str(decision),
            ":warnReason": str(warn_reason) if warn_reason else None,
            ":reasons": [str(reason) for reason in reasons],
            ":requestId": request_id,
        },
    )


def record_email_escalation(email_id: str, escalation_fields: dict) -> None:
    """EMAIL#.escalation: the second reviewer's answer on a WARN that was cleared (no REQUEST# row exists for it)."""
    get_table().update_item(
        Key={"PK": f"EMAIL#{email_id}", "SK": METADATA_SORT_KEY},
        UpdateExpression="SET escalation = :escalation",
        ExpressionAttributeValues={":escalation": decimal_fields(escalation_fields)},
    )


def decimal_fields(fields: dict) -> dict:
    """DynamoDB refuses floats; every float in a model dump becomes a Decimal."""
    return {key: Decimal(str(value)) if isinstance(value, float) else value for key, value in fields.items()}


def create_request(
    email_id: str, receiver_address: str, contact: Contact, verdict_fields: dict,
    friction: Friction, escalation_fields: dict | None,
) -> dict:
    """Stores a pending 'Was this you?' that holds for HOLD_DURATION_SECONDS; returns the stored row."""
    created_at = datetime.now(timezone.utc)
    request_row = {
        "PK": f"REQUEST#req-{uuid.uuid4()}", "SK": METADATA_SORT_KEY,
        "emailId": email_id, "receiverEmail": receiver_address,
        "claimedPerson": verdict_fields["claimed_person"], "contactSK": contact.contact_key,
        "verdict": decimal_fields(verdict_fields),
        "friction": str(friction),
        "escalation": decimal_fields(escalation_fields) if escalation_fields else None,
        "status": str(RequestState.PENDING), "channelsSent": [], "inboundMessageIds": [],
        "createdAt": iso_timestamp(created_at),
        "expiresAt": iso_timestamp(created_at + timedelta(seconds=HOLD_DURATION_SECONDS)),
        USER_INDEX_PARTITION_KEY: user_index_partition(contact.contact_pwa_user_id, REQUEST_ROW_KIND),
        USER_INDEX_SORT_KEY: iso_timestamp(created_at),
        "ttlEpochSeconds": retention_epoch(REQUEST_RETENTION_SECONDS),
    }
    get_table().put_item(Item=request_row, ConditionExpression="attribute_not_exists(PK)")
    return request_row


def request_id_of(request_row: dict) -> str:
    return request_row["PK"].removeprefix("REQUEST#")


def record_channels_sent(request_id: str, channels_sent: list[Channel]) -> None:
    get_table().update_item(
        Key={"PK": f"REQUEST#{request_id}", "SK": METADATA_SORT_KEY},
        UpdateExpression="SET channelsSent = :channels",
        ExpressionAttributeValues={":channels": [str(channel) for channel in channels_sent]},
    )


def record_rcs_message_id(request_id: str, rcs_message_id: str) -> None:
    get_table().update_item(
        Key={"PK": f"REQUEST#{request_id}", "SK": METADATA_SORT_KEY},
        UpdateExpression="SET rcsMessageId = :messageId",
        ExpressionAttributeValues={":messageId": rcs_message_id},
    )


def record_whatsapp_message_id(request_id: str, whatsapp_message_id: str) -> None:
    get_table().update_item(
        Key={"PK": f"REQUEST#{request_id}", "SK": METADATA_SORT_KEY},
        UpdateExpression="SET whatsappMessageId = :messageId",
        ExpressionAttributeValues={":messageId": whatsapp_message_id},
    )


def get_connection_user_id(connection_id: str) -> str | None:
    item = get_table().get_item(Key={"PK": f"CONN#{connection_id}", "SK": METADATA_SORT_KEY}).get("Item")
    return item["userId"] if item else None


def get_request(request_id: str) -> dict | None:
    """Strongly consistent: the case agent reads the row one second after the stream event that changed it."""
    return get_table().get_item(Key={"PK": f"REQUEST#{request_id}", "SK": METADATA_SORT_KEY}, ConsistentRead=True).get("Item")


def get_email(email_id: str) -> dict | None:
    return get_table().get_item(Key={"PK": f"EMAIL#{email_id}", "SK": METADATA_SORT_KEY}).get("Item")


def get_contact_row(receiver_address: str, contact_key: str) -> dict | None:
    return get_table().get_item(Key={"PK": f"RECEIVER#{receiver_address}", "SK": contact_key}).get("Item")


def list_recent_rows(user_id: str, row_kind: str, limit: int) -> list[dict]:
    """Newest-first EMAIL or REQUEST rows that belong to one PWA user (via UserIndex), then the full rows."""
    response = get_table().query(
        IndexName=USER_INDEX_NAME,
        KeyConditionExpression=Key(USER_INDEX_PARTITION_KEY).eq(user_index_partition(user_id, row_kind)),
        ScanIndexForward=False,
        Limit=limit,
    )
    return [get_table().get_item(Key={"PK": item["PK"], "SK": item["SK"]})["Item"] for item in response["Items"]]


def write_answer(request_id: str, update_arguments: dict) -> bool:
    """Applies one conditional answer update; False when the condition refused it."""
    try:
        get_table().update_item(Key={"PK": f"REQUEST#{request_id}", "SK": METADATA_SORT_KEY}, **update_arguments)
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == CONDITIONAL_CHECK_FAILED:
            return False
        raise


def mark_email_quarantined(email_id: str) -> bool:
    """False when the email was already quarantined."""
    try:
        get_table().update_item(
            Key={"PK": f"EMAIL#{email_id}", "SK": METADATA_SORT_KEY},
            UpdateExpression="SET quarantined = :true, quarantinedAt = :now",
            ConditionExpression="attribute_exists(PK) AND quarantined = :false",
            ExpressionAttributeValues={":true": True, ":false": False,
                                       ":now": iso_timestamp(datetime.now(timezone.utc))},
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == CONDITIONAL_CHECK_FAILED:
            return False
        raise


def write_listed_address(sender_address: str, reason: str) -> None:
    """Tier 0 scam-list row from a Confirm: the full address, never the domain; the same shape as every other LIST# row."""
    get_table().put_item(Item={
        "PK": "LIST#address", "SK": sender_address.lower(),
        "category": str(ListCategory.SCAM), "strength": str(ListStrength.LISTED), "source": str(ListSource.PIDGE_CONFIRMED),
        "tier": 0, "reason": reason, "attribution": "Reported by the receiver after the contact denied it",
        "addedAt": iso_timestamp(datetime.now(timezone.utc)), "expiresAt": None, "generation": 0,
    })


def delete_listed_address(sender_address: str) -> None:
    """Removes the Confirm row for an address; a reversed No means the report no longer stands."""
    get_table().delete_item(Key={"PK": "LIST#address", "SK": sender_address.lower()})


def write_case_note(request_id: str, receiver_user_id: str, note_text: str, recovery_pack: dict | None, note_for: str) -> dict:
    """NOTE#<writtenAt> under the request; note_for names the trigger so the same verdict never gets two notes."""
    written_at = iso_timestamp(datetime.now(timezone.utc))
    note_row = {
        "PK": f"REQUEST#{request_id}", "SK": f"{NOTE_SORT_KEY_PREFIX}{written_at}",
        "noteText": note_text, "recoveryPack": recovery_pack, "writtenBy": CASE_NOTE_WRITER, "noteFor": note_for,
        "writtenAt": written_at,
        USER_INDEX_PARTITION_KEY: user_index_partition(receiver_user_id, NOTE_ROW_KIND),
        USER_INDEX_SORT_KEY: written_at,
        "ttlEpochSeconds": retention_epoch(REQUEST_RETENTION_SECONDS),
    }
    get_table().put_item(Item=note_row)
    return note_row


def list_case_notes(request_id: str) -> list[dict]:
    """The notes written for one request, oldest first."""
    response = get_table().query(
        KeyConditionExpression=Key("PK").eq(f"REQUEST#{request_id}") & Key("SK").begins_with(NOTE_SORT_KEY_PREFIX)
    )
    return response["Items"]


def create_case(receiver_user_id: str, case_fields: dict) -> dict:
    """CASE#<uuid>: Grandma's record of a payment she already made; the stream wakes the case agent."""
    created_at = iso_timestamp(datetime.now(timezone.utc))
    case_row = {
        "PK": f"CASE#case-{uuid.uuid4()}", "SK": METADATA_SORT_KEY, **case_fields,
        "status": str(CaseState.OPEN), "disputeNumber": None, "createdAt": created_at,
        USER_INDEX_PARTITION_KEY: user_index_partition(receiver_user_id, CASE_ROW_KIND),
        USER_INDEX_SORT_KEY: created_at,
        "ttlEpochSeconds": retention_epoch(EMAIL_RETENTION_SECONDS),
    }
    get_table().put_item(Item=case_row, ConditionExpression="attribute_not_exists(PK)")
    return case_row


def get_case(case_id: str) -> dict | None:
    return get_table().get_item(Key={"PK": f"CASE#{case_id}", "SK": METADATA_SORT_KEY}).get("Item")


def list_case_replies(case_id: str) -> list[dict]:
    """Bank replies stored verbatim under the case, oldest first. Never handed to the agent."""
    response = get_table().query(
        KeyConditionExpression=Key("PK").eq(f"CASE#{case_id}") & Key("SK").begins_with(CASE_REPLY_SORT_KEY_PREFIX)
    )
    return response["Items"]


def write_case_token(reply_token: str, case_id: str) -> None:
    """CASETOKEN#<token> → case, written before the report is sent so a reply always resolves."""
    get_table().put_item(Item={"PK": f"CASETOKEN#{reply_token}", "SK": METADATA_SORT_KEY, "caseId": case_id,
                               "ttlEpochSeconds": retention_epoch(EMAIL_RETENTION_SECONDS)})


def find_case_id_for_token(reply_token: str) -> str | None:
    item = get_table().get_item(Key={"PK": f"CASETOKEN#{reply_token}", "SK": METADATA_SORT_KEY}).get("Item")
    return item["caseId"] if item else None


def update_case(case_id: str, update_arguments: dict) -> bool:
    """One conditional case update; False when the condition refused it."""
    try:
        get_table().update_item(Key={"PK": f"CASE#{case_id}", "SK": METADATA_SORT_KEY}, **update_arguments)
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == CONDITIONAL_CHECK_FAILED:
            return False
        raise


def write_case_reply(case_id: str, s3_key: str, reply_fields: dict) -> bool:
    """REPLY#<s3Key>: the same object twice writes one row (False the second time)."""
    try:
        get_table().put_item(
            Item={"PK": f"CASE#{case_id}", "SK": f"{CASE_REPLY_SORT_KEY_PREFIX}{s3_key}", **reply_fields,
                  "ttlEpochSeconds": retention_epoch(EMAIL_RETENTION_SECONDS)},
            ConditionExpression="attribute_not_exists(SK)",
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == CONDITIONAL_CHECK_FAILED:
            return False
        raise
