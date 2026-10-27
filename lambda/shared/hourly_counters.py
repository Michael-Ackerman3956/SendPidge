"""Per-hour counters that fail closed: the ask cap per contact (pidge-reader) and the question cap (chat). One conditional
increment per slot; a write failure counts as the cap being reached, never as a free pass."""

import logging
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from shared import graph

logger = logging.getLogger(__name__)

HOURLY_COUNTER_RETENTION_SECONDS = 2 * 3600
ASK_COUNTER_PARTITION_PREFIX = "ASKRATE#"
HOUR_SORT_KEY_PREFIX = "HOUR#"


def current_hour_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def take_hourly_slot(partition_key: str, limit: int) -> bool:
    """One conditional increment of this hour's counter under the partition. False at the cap or on any write failure (fail closed)."""
    try:
        graph.get_table().update_item(
            Key={"PK": partition_key, "SK": f"{HOUR_SORT_KEY_PREFIX}{current_hour_label()}"},
            UpdateExpression="ADD slotCount :one SET ttlEpochSeconds = :expiry",
            ConditionExpression="attribute_not_exists(slotCount) OR slotCount < :limit",
            ExpressionAttributeValues={":one": 1, ":limit": limit, ":expiry": graph.retention_epoch(HOURLY_COUNTER_RETENTION_SECONDS)},
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] != graph.CONDITIONAL_CHECK_FAILED:
            logger.error("hourly counter write failed for %s; refusing: %s", partition_key, error)
        return False


def take_ask_slot(contact_key: str, asks_per_hour_limit: int) -> bool:
    """The ask cap: a contact is asked at most this many times an hour; past it the hold stays and nothing is sent."""
    return take_hourly_slot(f"{ASK_COUNTER_PARTITION_PREFIX}{contact_key}", asks_per_hour_limit)
