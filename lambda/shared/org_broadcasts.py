"""A home's safety alert, in-app only (spec.md > Stretch > Mass send path, its in-app part; no WhatsApp, SMS or email, whose
templates need Meta review): BROADCAST#<orgId>#<id>/META {kind, text, createdBy, createdAt}, one RECIPIENT#<receiverEmail>
row per member resident {residentUserId, residentName, orgName, text, state, createdAt}, indexed for her sync on UserIndex
"<residentUserId>#ORGNOTICE", and ORG#<orgId>/BROADCASTIDX#<createdAt>#<id>, written with the sender's ORGAUDIT# row in one
transaction after the org's hourly BROADCASTRATE#<orgId> slot (fail closed). A resident answers only her own RECIPIENT# row
of her own home's alert, and only once: the first answer wins."""

import logging
import re
import uuid
from dataclasses import dataclass

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import graph, hourly_counters, org_audit, org_view, orgs, sanitize
from shared.agent_enums import UnsafeTextKind
from shared.enums import ActionOutcome
from shared.org_enums import AckAnswer, BroadcastKind, DeliveryState, OrgAuditAction
from shared.orgs import OrgAdmin
from shared.profiles import ReceiverProfile
from shared.sources import query_all_pages

logger = logging.getLogger(__name__)
BROADCAST_PARTITION_PREFIX = "BROADCAST#"
RECIPIENT_SORT_KEY_PREFIX = "RECIPIENT#"
BROADCAST_INDEX_SORT_KEY_PREFIX = "BROADCASTIDX#"
BROADCAST_RATE_PARTITION_PREFIX = "BROADCASTRATE#"
ORG_NOTICE_ROW_KIND = "ORGNOTICE"
BROADCAST_ID_PATTERN = re.compile(r"bc-[0-9a-f-]{36}")  # used with fullmatch
ALERT_TEXT_MAX_CHARACTERS = 280
BROADCAST_RETENTION_SECONDS = 90 * 24 * 3600  # as long as the org's ORGLOG# rows
REPORTED_BROADCASTS_LIMIT = 5  # the admin's report: the newest alerts
SYNCED_NOTICES_LIMIT = 5  # the resident's Pidge tab: her newest alerts
ROWS_BESIDE_RECIPIENTS = 3  # META, the BROADCASTIDX# item and the audit row share the transaction


@dataclass(frozen=True)
class SentAlert:
    broadcast_id: str
    recipient_rows: list[dict]


def broadcast_partition(org_id: str, broadcast_id: str) -> str:
    return f"{BROADCAST_PARTITION_PREFIX}{org_id}#{broadcast_id}"


def recipient_sort_key(receiver_address: str) -> str:
    return f"{RECIPIENT_SORT_KEY_PREFIX}{receiver_address}"


def parse_alert_text(value) -> str:
    """The alert with its outer spaces removed; ValueError when it is not text or is 0 or more than ALERT_TEXT_MAX_CHARACTERS
    characters."""
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= ALERT_TEXT_MAX_CHARACTERS:
        raise ValueError(f"alert text must be 1 to {ALERT_TEXT_MAX_CHARACTERS} characters")
    return value.strip()


def unsafe_kinds_in(alert_text: str) -> list[UnsafeTextKind]:
    """What the agent output gate finds in the alert (a link, address, handle, long number or a character outside the
    allow-list); any one refuses it, like Pidge's own text to a person."""
    return sanitize.find_unsafe_spans(alert_text)


def parse_broadcast_id(value) -> str:
    """"bc-<uuid4>"; ValueError for anything else."""
    if not isinstance(value, str) or not BROADCAST_ID_PATTERN.fullmatch(value):
        raise ValueError("broadcast id")
    return value


def take_broadcast_slot(org_id: str, broadcasts_per_hour: int) -> bool:
    """One of the org's hourly broadcast slots; False at the cap or when the counter cannot be written (fail closed)."""
    return hourly_counters.take_hourly_slot(f"{BROADCAST_RATE_PARTITION_PREFIX}{org_id}", broadcasts_per_hour)


def list_alert_recipients(org_id: str) -> list[ReceiverProfile]:
    """The resident of every MEMBER# row of the org; a member without a profile or enrolled in another org is left out
    (org_view.resident_of_member logs and counts it)."""
    member_refs = [member_row["SK"].removeprefix(orgs.MEMBER_SORT_KEY_PREFIX) for member_row in orgs.list_member_rows(org_id)]
    residents = [org_view.resident_of_member(org_id, member_ref) for member_ref in member_refs]
    return [resident for resident in residents if resident is not None]


def build_recipient_row(org_id: str, broadcast_id: str, org_name: str, alert_text: str, resident: ReceiverProfile,
                        created_at: str, expiry: int) -> dict:
    return {
        "PK": broadcast_partition(org_id, broadcast_id), "SK": recipient_sort_key(resident.receiver_address),
        "broadcastId": broadcast_id, "residentUserId": resident.receiver_user_id, "residentName": resident.resident_name,
        "orgName": org_name, "text": alert_text, "state": str(DeliveryState.SENT), "createdAt": created_at,
        graph.USER_INDEX_PARTITION_KEY: graph.user_index_partition(resident.receiver_user_id, ORG_NOTICE_ROW_KIND),
        graph.USER_INDEX_SORT_KEY: created_at, "ttlEpochSeconds": expiry,
    }


def write_safety_alert(org_admin: OrgAdmin, org_name: str, alert_text: str, recipients: list[ReceiverProfile]) -> SentAlert:
    """In ONE transaction, each item only when absent: the META row, the ORG#<orgId>/BROADCASTIDX# item, one RECIPIENT# row
    per recipient and the broadcast_send audit row. Raises ValueError, writing nothing, when there are more recipients than
    one transaction holds; any write failure raises."""
    if len(recipients) > orgs.TRANSACTION_ITEMS_MAXIMUM - ROWS_BESIDE_RECIPIENTS:
        raise ValueError(f"more than {orgs.TRANSACTION_ITEMS_MAXIMUM - ROWS_BESIDE_RECIPIENTS} recipients for one transaction")
    table = graph.get_table()
    broadcast_id = f"bc-{uuid.uuid4()}"
    created_at = graph.now_timestamp()
    expiry = graph.retention_epoch(BROADCAST_RETENTION_SECONDS)
    partition_key = broadcast_partition(org_admin.org_id, broadcast_id)
    meta_row = {"PK": partition_key, "SK": graph.METADATA_SORT_KEY, "kind": str(BroadcastKind.SAFETY_ALERT), "text": alert_text,
                "createdBy": org_admin.user_id, "createdAt": created_at, "ttlEpochSeconds": expiry}
    index_row = {"PK": orgs.org_partition(org_admin.org_id), "SK": f"{BROADCAST_INDEX_SORT_KEY_PREFIX}{created_at}#{broadcast_id}",
                 "broadcastId": broadcast_id, "ttlEpochSeconds": expiry}
    recipient_rows = [build_recipient_row(org_admin.org_id, broadcast_id, org_name, alert_text, resident, created_at, expiry)
                      for resident in recipients]
    audit_row = org_audit.build_audit_row(org_admin, OrgAuditAction.BROADCAST_SEND, broadcast_id)
    table.meta.client.transact_write_items(TransactItems=[
        {"Put": {"TableName": table.name, "Item": row, "ConditionExpression": "attribute_not_exists(SK)"}}
        for row in [meta_row, index_row, *recipient_rows, audit_row]
    ])
    return SentAlert(broadcast_id, recipient_rows)


def notice_for_resident(recipient_row: dict) -> dict:
    """The resident's view of her copy: the alert, her home's name, when it was sent and her answer (None until she answers)."""
    is_answered = DeliveryState(recipient_row["state"]) is DeliveryState.ACKNOWLEDGED
    return {"broadcastId": recipient_row["broadcastId"], "orgName": recipient_row["orgName"], "text": recipient_row["text"],
            "sentAt": recipient_row["createdAt"], "answer": str(AckAnswer(recipient_row["ackAnswer"])) if is_answered else None}


def notices_for_resident(resident: ReceiverProfile) -> list[dict]:
    """Her newest alerts, newest first, for her sync."""
    recipient_rows = graph.list_recent_rows(resident.receiver_user_id, ORG_NOTICE_ROW_KIND, SYNCED_NOTICES_LIMIT)
    return [notice_for_resident(recipient_row) for recipient_row in recipient_rows]


def record_acknowledgement(resident: ReceiverProfile, broadcast_id: str, answer: AckAnswer) -> tuple[ActionOutcome, dict | None]:
    """Her own RECIPIENT# row of an alert of her own home (the keys come from her profile, the body names only the alert):
    sent → acknowledged with the answer, only from sent, so the first answer wins. NOT_ALLOWED when she has no such row,
    NOT_APPLIED when it is already answered; RECORDED with the updated row."""
    table = graph.get_table()
    row_key = {"PK": broadcast_partition(resident.org_id, broadcast_id), "SK": recipient_sort_key(resident.receiver_address)}
    if "Item" not in table.get_item(Key=row_key):
        return ActionOutcome.NOT_ALLOWED, None
    try:
        response = table.update_item(
            Key=row_key, UpdateExpression="SET #state = :acknowledged, ackAnswer = :answer, ackAt = :at",
            ConditionExpression="#state = :sent", ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":acknowledged": str(DeliveryState.ACKNOWLEDGED), ":answer": str(answer),
                                       ":at": graph.now_timestamp(), ":sent": str(DeliveryState.SENT)},
            ReturnValues="ALL_NEW",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == graph.CONDITIONAL_CHECK_FAILED:
            return ActionOutcome.NOT_APPLIED, None
        raise
    return ActionOutcome.RECORDED, response["Attributes"]


def recipient_report(recipient_row: dict) -> dict:
    is_answered = DeliveryState(recipient_row["state"]) is DeliveryState.ACKNOWLEDGED
    return {"memberRef": recipient_row["SK"].removeprefix(RECIPIENT_SORT_KEY_PREFIX), "residentName": recipient_row["residentName"],
            "state": str(DeliveryState(recipient_row["state"])),
            "answer": str(AckAnswer(recipient_row["ackAnswer"])) if is_answered else None}


def broadcast_report(org_id: str, broadcast_id: str) -> dict:
    """One alert as the admin's report shows it: its text, when it went and each recipient's state and answer, by name."""
    rows = query_all_pages(KeyConditionExpression=Key("PK").eq(broadcast_partition(org_id, broadcast_id)))
    [meta_row] = [row for row in rows if row["SK"] == graph.METADATA_SORT_KEY]
    recipient_rows = sorted((row for row in rows if row["SK"].startswith(RECIPIENT_SORT_KEY_PREFIX)),
                            key=lambda row: row["residentName"])
    return {"broadcastId": broadcast_id, "kind": str(BroadcastKind(meta_row["kind"])), "text": meta_row["text"],
            "sentAt": meta_row["createdAt"], "recipients": [recipient_report(row) for row in recipient_rows]}


def broadcast_reports_for(org_id: str) -> list[dict]:
    """The org's newest REPORTED_BROADCASTS_LIMIT alerts, newest first (a Query on its own ORG# partition, never a Scan)."""
    response = graph.get_table().query(
        KeyConditionExpression=Key("PK").eq(orgs.org_partition(org_id)) & Key("SK").begins_with(BROADCAST_INDEX_SORT_KEY_PREFIX),
        ScanIndexForward=False, Limit=REPORTED_BROADCASTS_LIMIT,
    )
    return [broadcast_report(org_id, index_row["broadcastId"]) for index_row in response["Items"]]
