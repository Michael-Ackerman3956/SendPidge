"""DynamoDB access for inbound rows, keyed by (channel, item id): EMAIL#<id> for email, MESSAGE#<id> for a text channel. The
pipeline claims and decides them; a request finds the row it holds through REQUEST#.inboundChannel and REQUEST#.emailId."""

from datetime import datetime, timezone

from botocore.exceptions import ClientError

from shared import graph
from shared.enums import Channel, PolicyDecision, ReasonCode, WarnReason

MESSAGE_ROW_KIND = "MESSAGE"
MESSAGE_ROW_CHANNELS = frozenset({Channel.WHATSAPP, Channel.RCS})  # the channels whose inbound rows are MESSAGE#
READER_RECLAIM_AFTER_SECONDS = 55  # just under pidge-reader's 60 s Lambda timeout; its first async retry comes a minute after a crash


def inbound_channel_of(request_row: dict) -> Channel:
    """Rows written before 2026-10-26 carry no inboundChannel; email was the only inbound channel then."""
    return Channel(request_row.get("inboundChannel", Channel.EMAIL))


def message_item_id(channel: Channel, provider_message_id: str) -> str:
    return f"{channel}#{provider_message_id}"


def inbound_partition_key(channel: Channel, item_id: str) -> str:
    """EMAIL#<id> for email, MESSAGE#<id> for a text channel; a channel nothing comes in on raises ValueError."""
    if channel is Channel.EMAIL:
        return f"{graph.EMAIL_ROW_KIND}#{item_id}"
    if channel in MESSAGE_ROW_CHANNELS:
        return f"{MESSAGE_ROW_KIND}#{item_id}"
    raise ValueError(f"no inbound rows on {channel}")


def thread_key_for(channel: Channel, address: str) -> str:
    """One conversation per channel and address; an email address compares case-insensitively, a number exactly."""
    return f"{channel}#{address.casefold() if channel is Channel.EMAIL else address}"


def row_key(channel: Channel, item_id: str) -> dict:
    return {"PK": inbound_partition_key(channel, item_id), "SK": graph.METADATA_SORT_KEY}


def get_row(channel: Channel, item_id: str) -> dict | None:
    return graph.get_table().get_item(Key=row_key(channel, item_id)).get("Item")


def get_held_row(request_row: dict) -> dict | None:
    """The row a request holds (REQUEST#.emailId on REQUEST#.inboundChannel); None only when that row is missing."""
    return get_row(inbound_channel_of(request_row), request_row["emailId"])


def require_held_row(request_row: dict) -> dict:
    """get_held_row for a caller that cannot go on without the row: a missing row raises LookupError (ids only in the text)."""
    held_row = get_held_row(request_row)
    if held_row is None:
        raise LookupError(f"the row held by {graph.request_id_of(request_row)} is missing")
    return held_row


def claim_row(channel: Channel, item_id: str, row_fields: dict) -> bool:
    """Writes the row once, stamped claimedAt; False means another run holds this item (see reclaim_abandoned_row)."""
    try:
        graph.get_table().put_item(
            Item={**row_key(channel, item_id), **row_fields, "claimedAt": graph.now_timestamp(),
                  "ttlEpochSeconds": graph.retention_epoch(graph.EMAIL_RETENTION_SECONDS)},
            ConditionExpression="attribute_not_exists(PK)",
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == graph.CONDITIONAL_CHECK_FAILED:
            return False
        raise


def reclaim_abandoned_row(channel: Channel, item_id: str, now: datetime) -> bool:
    """True when a claimed row never got its state and the claim is older than the Reader's timeout: that run crashed, so the
    retry may carry on (otherwise the row would read "Pidge is reading" for ever and the dead-letter alarm never fire). A claim
    younger than that is a concurrent duplicate event and stays refused."""
    claimed_row = get_row(channel, item_id)
    if claimed_row is None or "state" in claimed_row or "claimedAt" not in claimed_row:
        return False
    claimed_at = datetime.fromisoformat(claimed_row["claimedAt"].replace("Z", "+00:00"))
    if (now - claimed_at).total_seconds() < READER_RECLAIM_AFTER_SECONDS:
        return False
    graph.get_table().update_item(Key=row_key(channel, item_id), UpdateExpression="SET claimedAt = :now",
                                  ExpressionAttributeValues={":now": graph.iso_timestamp(now)})
    return True


def record_outcome(
    channel: Channel,
    item_id: str,
    decision: PolicyDecision,
    warn_reason: WarnReason | None,
    reasons: tuple[ReasonCode, ...],
    request_id: str | None,
) -> None:
    graph.get_table().update_item(
        Key=row_key(channel, item_id),
        UpdateExpression="SET #state = :state, warnReason = :warnReason, reasons = :reasons, requestId = :requestId",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={
            ":state": str(decision),
            ":warnReason": str(warn_reason) if warn_reason else None,
            ":reasons": [str(reason) for reason in reasons],
            ":requestId": request_id,
        },
    )


def record_escalation(channel: Channel, item_id: str, escalation_fields: dict) -> None:
    """The row's escalation: the second reviewer's answer on a WARN that was cleared (no REQUEST# row exists for it)."""
    graph.get_table().update_item(
        Key=row_key(channel, item_id),
        UpdateExpression="SET escalation = :escalation",
        ExpressionAttributeValues={":escalation": graph.decimal_fields(escalation_fields)},
    )


def mark_quarantined(channel: Channel, item_id: str) -> bool:
    """False when the row was already quarantined."""
    try:
        graph.get_table().update_item(
            Key=row_key(channel, item_id),
            UpdateExpression="SET quarantined = :true, quarantinedAt = :now",
            ConditionExpression="attribute_exists(PK) AND quarantined = :false",
            ExpressionAttributeValues={":true": True, ":false": False,
                                       ":now": graph.iso_timestamp(datetime.now(timezone.utc))},
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == graph.CONDITIONAL_CHECK_FAILED:
            return False
        raise
