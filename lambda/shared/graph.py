"""DynamoDB access: contacts, access tokens, WebSocket connections, emails and verification requests."""

import hashlib
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared.enums import Channel, PolicyDecision, ReasonCode, RequestState, WarnReason

HOLD_DURATION_SECONDS = 60
REQUEST_RETENTION_SECONDS = 30 * 24 * 3600
EMAIL_RETENTION_SECONDS = 90 * 24 * 3600
CONNECTION_RETENTION_SECONDS = 24 * 3600
CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailedException"
METADATA_SORT_KEY = "META"
# UserIndex (GSI): "<userId>#EMAIL" or "<userId>#REQUEST" → rows newest first, for the PWA's sync.
USER_INDEX_NAME = "UserIndex"
USER_INDEX_PARTITION_KEY = "userIndexPK"
USER_INDEX_SORT_KEY = "userIndexSK"
EMAIL_ROW_KIND = "EMAIL"
REQUEST_ROW_KIND = "REQUEST"

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


def find_contact(receiver_address: str, claimed_person: str | None) -> Contact | None:
    """Exact name match (case and spacing ignored) against the receiver's own contacts."""
    if not claimed_person:
        return None
    wanted_name = " ".join(claimed_person.split()).casefold()
    response = get_table().query(
        KeyConditionExpression=Key("PK").eq(f"RECEIVER#{receiver_address}") & Key("SK").begins_with("CONTACT#")
    )
    for item in response["Items"]:
        if " ".join(item["contactName"].split()).casefold() == wanted_name:
            return Contact(item["SK"], item["contactName"], item["contactPwaUserId"])
    return None


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


def create_request(email_id: str, receiver_address: str, contact: Contact, verdict_fields: dict) -> dict:
    """Stores a pending 'Was this you?' that holds for HOLD_DURATION_SECONDS; returns the stored row."""
    created_at = datetime.now(timezone.utc)
    request_row = {
        "PK": f"REQUEST#req-{uuid.uuid4()}", "SK": METADATA_SORT_KEY,
        "emailId": email_id, "receiverEmail": receiver_address,
        "claimedPerson": verdict_fields["claimed_person"], "contactSK": contact.contact_key,
        "verdict": {**verdict_fields, "confidence": Decimal(str(verdict_fields["confidence"]))},
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


def get_connection_user_id(connection_id: str) -> str | None:
    item = get_table().get_item(Key={"PK": f"CONN#{connection_id}", "SK": METADATA_SORT_KEY}).get("Item")
    return item["userId"] if item else None


def get_request(request_id: str) -> dict | None:
    return get_table().get_item(Key={"PK": f"REQUEST#{request_id}", "SK": METADATA_SORT_KEY}).get("Item")


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
    """Tier 0 lookup row from a Confirm: the full address, never the domain."""
    get_table().put_item(Item={
        "PK": "LIST#address", "SK": sender_address.lower(),
        "category": "scam", "strength": "listed", "source": "pidge-confirmed", "tier": 0,
        "reason": reason, "addedAt": iso_timestamp(datetime.now(timezone.utc)),
    })
