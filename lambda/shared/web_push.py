"""Web Push (VAPID) send: one fixed notice to every saved browser of a user. No AWS push service involved."""

import json
import logging
import os

import boto3
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from shared import orgs, push_subscriptions
from shared.metrics import emit_count_metric
from shared.enums import PushNotice

logger = logging.getLogger()

EXPIRED_SUBSCRIPTION_STATUS_CODES = (404, 410)
NOTICE_TIME_TO_LIVE_SECONDS = 86400
PUSH_SERVICE_TIMEOUT_SECONDS = 5
FAILED_METRIC_NAME = "PushFailed"
VAPID_SUBJECT = "mailto:pidge@sendpidge.com"
# Fixed text only: a lock screen must never show email text, amounts or names from a reply. The only variable is the
# contact's name from the family graph, filled into the {contact_name} notices.
NOTICE_TEXT = {
    PushNotice.BANK_REPLIED: ("SendPidge", "Your bank replied to your Pidge case."),
    PushNotice.FAMILY_CALL_TODAY: ("SendPidge", "{contact_name} will call you today."),
    PushNotice.FAMILY_CALL_TOMORROW: ("SendPidge", "{contact_name} will call you tomorrow."),
}
NOTICES_NEEDING_CONTACT_NAME = (PushNotice.FAMILY_CALL_TODAY, PushNotice.FAMILY_CALL_TOMORROW)

_vapid_signer = None


def get_vapid_signer() -> Vapid:
    """The private key lives in Secrets Manager as the 32-byte raw key, base64url; it is never logged."""
    global _vapid_signer
    if _vapid_signer is None:
        secret = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["VAPID_SECRET_NAME"])
        _vapid_signer = Vapid.from_raw(secret["SecretString"].strip().encode())
    return _vapid_signer


def emit_failed_metric(notice: PushNotice) -> None:
    emit_count_metric(FAILED_METRIC_NAME, "Notice", str(notice))


def send_to_subscription(row: dict, payload: str) -> None:
    subscription = {"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}}
    webpush(subscription, data=payload, vapid_private_key=get_vapid_signer(), vapid_claims={"sub": VAPID_SUBJECT},
            ttl=NOTICE_TIME_TO_LIVE_SECONDS, timeout=PUSH_SERVICE_TIMEOUT_SECONDS)


def notice_title_and_text(notice: PushNotice, contact_name: str | None) -> tuple[str, str]:
    """The fixed title and sentence; a family-call notice without a contact name raises ValueError."""
    title, text = NOTICE_TEXT[notice]
    if notice in NOTICES_NEEDING_CONTACT_NAME:
        if not contact_name:
            raise ValueError(f"notice {notice} needs a contact name")
        text = text.format(contact_name=contact_name)
    return title, text


def send_notice(user_id: str, notice: PushNotice, contact_name: str | None = None) -> int:
    """Sends the notice to every saved browser of the user, and for an org user (org-<orgId>) of each of its admins instead;
    returns how many accepted it.

    A family-call notice without contact_name raises ValueError before anything is sent. A 404/410 means the browser
    unsubscribed, so its row is deleted. Any other send failure is logged with a PushFailed metric and never raised:
    the in-app card already shows the same event.
    """
    title, text = notice_title_and_text(notice, contact_name)
    payload = json.dumps({"title": title, "body": text, "notice": str(notice)})
    return sum(send_to_saved_browsers(recipient_user_id, notice, payload) for recipient_user_id in orgs.recipient_user_ids_for(user_id))


def send_to_saved_browsers(user_id: str, notice: PushNotice, payload: str) -> int:
    """One user's saved browsers; returns how many accepted the payload (failures handled as send_notice says)."""
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
