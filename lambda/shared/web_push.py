"""Web Push (VAPID) send: one fixed notice to every saved browser of a user. No AWS push service involved."""

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from shared import push_subscriptions
from shared.enums import PushNotice

logger = logging.getLogger()

EXPIRED_SUBSCRIPTION_STATUS_CODES = (404, 410)
NOTICE_TIME_TO_LIVE_SECONDS = 86400
PUSH_SERVICE_TIMEOUT_SECONDS = 5
FAILED_METRIC_NAME = "PushFailed"
VAPID_SUBJECT = "mailto:pidge@sendpidge.com"
# Fixed text only: a lock screen must never show email text, amounts or names from a reply.
NOTICE_TEXT = {
    PushNotice.BANK_REPLIED: ("SendPidge", "Your bank replied to your Pidge case."),
}

_vapid_signer = None


def get_vapid_signer() -> Vapid:
    """The private key lives in Secrets Manager as the 32-byte raw key, base64url; it is never logged."""
    global _vapid_signer
    if _vapid_signer is None:
        secret = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["VAPID_SECRET_NAME"])
        _vapid_signer = Vapid.from_raw(secret["SecretString"].strip().encode())
    return _vapid_signer


def emit_failed_metric(notice: PushNotice) -> None:
    print(json.dumps({
        "_aws": {"Timestamp": int(datetime.now(timezone.utc).timestamp() * 1000), "CloudWatchMetrics": [
            {"Namespace": "SendPidge", "Dimensions": [["Notice"]], "Metrics": [{"Name": FAILED_METRIC_NAME, "Unit": "Count"}]}]},
        "Notice": str(notice), FAILED_METRIC_NAME: 1,
    }))


def send_to_subscription(row: dict, payload: str) -> None:
    subscription = {"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}}
    webpush(subscription, data=payload, vapid_private_key=get_vapid_signer(), vapid_claims={"sub": VAPID_SUBJECT},
            ttl=NOTICE_TIME_TO_LIVE_SECONDS, timeout=PUSH_SERVICE_TIMEOUT_SECONDS)


def send_notice(user_id: str, notice: PushNotice) -> int:
    """Sends the notice to every saved browser of the user; returns how many accepted it.

    A 404/410 means the browser unsubscribed, so its row is deleted. Any other failure is logged with a PushFailed
    metric and never raised: the in-app card already shows the same event.
    """
    title, text = NOTICE_TEXT[notice]
    payload = json.dumps({"title": title, "body": text, "notice": str(notice)})
    delivered_count = 0
    for row in push_subscriptions.list_subscription_rows(user_id):
        try:
            send_to_subscription(row, payload)
            delivered_count += 1
        except WebPushException as error:
            status_code = getattr(error.response, "status_code", None)
            if status_code in EXPIRED_SUBSCRIPTION_STATUS_CODES:
                push_subscriptions.delete_subscription_row(row)
                continue
            logger.error("web push %s to %s failed with status %s", notice, row["SK"], status_code)
            emit_failed_metric(notice)
        except Exception:
            logger.exception("web push %s to %s failed", notice, row["SK"])
            emit_failed_metric(notice)
    return delivered_count
