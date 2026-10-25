"""Web Push subscriptions: validate a browser's PushSubscription and keep it under the user's own USER# row.

No third-party imports, so the WebSocket handler can save a subscription without the dependency layer.
"""

from hashlib import sha256
from urllib.parse import urlsplit

from boto3.dynamodb.conditions import Key

from shared import graph
from shared.enums import ActionOutcome

PUSH_SUBSCRIPTION_PREFIX = "PUSHSUB#"
MAXIMUM_SUBSCRIPTIONS_PER_USER = 5
ENDPOINT_MAX_CHARACTERS = 2048
KEY_MAX_CHARACTERS = 256
# The browsers' push services; any other endpoint host is refused so a client cannot make us POST to an arbitrary URL.
ALLOWED_PUSH_HOSTS = frozenset({"web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com"})
ALLOWED_PUSH_HOST_SUFFIXES = (".notify.windows.com",)


def is_allowed_push_endpoint(endpoint: object) -> bool:
    if not isinstance(endpoint, str) or len(endpoint) > ENDPOINT_MAX_CHARACTERS:
        return False
    try:
        parts = urlsplit(endpoint)
        port = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or port not in (None, 443) or parts.username or parts.password:
        return False
    return host in ALLOWED_PUSH_HOSTS or host.endswith(ALLOWED_PUSH_HOST_SUFFIXES)


def parse_subscription(body: dict) -> dict | None:
    """The browser's PushSubscription.toJSON(): {endpoint, keys: {p256dh, auth}}; None when anything is off."""
    subscription = body.get("subscription")
    if not isinstance(subscription, dict) or not is_allowed_push_endpoint(subscription.get("endpoint")):
        return None
    keys = subscription.get("keys")
    if not isinstance(keys, dict):
        return None
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not all(isinstance(value, str) and 0 < len(value) <= KEY_MAX_CHARACTERS for value in (p256dh, auth)):
        return None
    return {"endpoint": subscription["endpoint"], "keys": {"p256dh": p256dh, "auth": auth}}


def subscription_sort_key(endpoint: str) -> str:
    return PUSH_SUBSCRIPTION_PREFIX + sha256(endpoint.encode()).hexdigest()


def list_subscription_rows(user_id: str) -> list[dict]:
    response = graph.get_table().query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with(PUSH_SUBSCRIPTION_PREFIX)
    )
    return response["Items"]


def save_subscription(user_id: str, body: dict) -> ActionOutcome:
    """user_id comes from the caller's connection row, never from the body. Re-saving the same browser overwrites its row."""
    subscription = parse_subscription(body)
    if subscription is None:
        return ActionOutcome.BAD_MESSAGE
    sort_key = subscription_sort_key(subscription["endpoint"])
    existing_sort_keys = {row["SK"] for row in list_subscription_rows(user_id)}
    if sort_key not in existing_sort_keys and len(existing_sort_keys) >= MAXIMUM_SUBSCRIPTIONS_PER_USER:
        return ActionOutcome.NOT_ALLOWED
    graph.get_table().put_item(Item={
        "PK": f"USER#{user_id}", "SK": sort_key, "endpoint": subscription["endpoint"],
        "p256dh": subscription["keys"]["p256dh"], "auth": subscription["keys"]["auth"],
        "createdAt": graph.now_timestamp(),
    })
    return ActionOutcome.RECORDED


def delete_subscription_row(row: dict) -> None:
    graph.get_table().delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
