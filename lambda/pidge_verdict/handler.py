"""WebSocket $default: sync, the contact's Yes / No, and the receiver's Confirm. The user comes from the connection row."""

import json
import logging
import os
from datetime import datetime, timezone

import boto3

from shared import channels, graph, payloads, verdict
from shared.enums import ActionOutcome, Channel, ClientAction, PushMessageType, UserRole, VerdictAnswer

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RECEIVER_ADDRESS = os.environ["RECEIVER_ADDRESS"]
RECEIVER_USER_ID = os.environ["RECEIVER_USER_ID"]
RECEIVER_NAME = os.environ["RECEIVER_NAME"]
EMAIL_BUCKET = os.environ["EMAIL_BUCKET"]
RECEIPT_SENDER = os.environ["RECEIPT_SENDER"]
SYNC_EMAIL_LIMIT = 20
SYNC_REQUEST_LIMIT = 10
HANDLED = {"statusCode": 200}
FORBIDDEN = {"statusCode": 403}

s3_client = boto3.client("s3")
ses_client = boto3.client("sesv2")


def handle(event, context):
    request_context = event["requestContext"]
    connection_id = request_context["connectionId"]
    user_id = graph.get_connection_user_id(connection_id)
    if user_id is None:
        logger.warning("message from unregistered connection %s", connection_id)
        return FORBIDDEN
    try:
        body = json.loads(event.get("body") or "")
        action = ClientAction(body["action"])
    except (ValueError, TypeError, KeyError):
        reject(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return HANDLED
    if action is ClientAction.SYNC:
        handle_sync(connection_id, user_id)
    elif action is ClientAction.ANSWER:
        handle_answer(connection_id, user_id, body, request_context["messageId"])
    else:
        handle_confirm(connection_id, user_id, body)
    return HANDLED


def reject(connection_id: str, request_id: str | None, outcome: ActionOutcome) -> None:
    channels.push_to_connection(
        connection_id, {"type": PushMessageType.ACTION_REJECTED, "requestId": request_id, "reason": outcome}
    )


def contact_name_for(request_row: dict) -> str:
    contact_row = graph.get_contact_row(request_row["receiverEmail"], request_row["contactSK"])
    return contact_row["contactName"] if contact_row else request_row["claimedPerson"]


def handle_sync(connection_id: str, user_id: str) -> None:
    """Reconnect or reload: the role and the rows this user should see right now."""
    if user_id == RECEIVER_USER_ID:
        emails = []
        for email_row in graph.list_recent_rows(user_id, graph.EMAIL_ROW_KIND, SYNC_EMAIL_LIMIT):
            if "state" not in email_row:
                continue  # the Reader is still on it; its push will follow
            request_row = graph.get_request(email_row["requestId"]) if email_row.get("requestId") else None
            contact_name = contact_name_for(request_row) if request_row else None
            emails.append(payloads.email_for_receiver(email_row, request_row, contact_name))
        message = {"type": PushMessageType.SYNC_STATE, "role": UserRole.RECEIVER, "emails": emails}
    else:
        request_rows = graph.list_recent_rows(user_id, graph.REQUEST_ROW_KIND, SYNC_REQUEST_LIMIT)
        cards = [payloads.card_for_contact(request_row, RECEIVER_NAME) for request_row in request_rows]
        message = {"type": PushMessageType.SYNC_STATE, "role": UserRole.CONTACT, "requests": cards}
    channels.push_to_connection(connection_id, message)


def handle_answer(connection_id: str, user_id: str, body: dict, websocket_message_id: str) -> None:
    request_id = body.get("requestId")
    try:
        answer = VerdictAnswer(body.get("answer"))
    except ValueError:
        reject(connection_id, request_id, ActionOutcome.BAD_MESSAGE)
        return
    if not isinstance(request_id, str):
        reject(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    now = graph.iso_timestamp(datetime.now(timezone.utc))
    sender = verdict.SenderIdentity(Channel.INAPP, user_id)
    result = verdict.record_answer(request_id, answer, sender, f"inapp:{websocket_message_id}", now)
    if result.outcome is not ActionOutcome.RECORDED:
        reject(connection_id, request_id, result.outcome)
        return
    message = payloads.request_state_message(result.request_row, result.status, result.contact_row["contactName"], False)
    channels.push_to_user(RECEIVER_USER_ID, message)
    channels.push_to_user(user_id, message)
    try:  # the verdict already reached the receiver; a failed receipt is logged, never blocking
        verdict.send_receipt(ses_client, RECEIPT_SENDER, result.contact_row, result.status, RECEIVER_NAME)
    except Exception:
        logger.exception("receipt for %s failed", request_id)


def handle_confirm(connection_id: str, user_id: str, body: dict) -> None:
    request_id = body.get("requestId")
    if not isinstance(request_id, str):
        reject(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    result = verdict.confirm_quarantine(
        request_id, user_id, RECEIVER_USER_ID, RECEIVER_ADDRESS, s3_client, EMAIL_BUCKET
    )
    if result.outcome is not ActionOutcome.RECORDED:
        reject(connection_id, request_id, result.outcome)
        return
    contact_row = graph.get_contact_row(result.request_row["receiverEmail"], result.request_row["contactSK"])
    message = payloads.request_state_message(result.request_row, result.status, contact_row["contactName"], True)
    channels.push_to_user(RECEIVER_USER_ID, message)
    channels.push_to_user(contact_row["contactPwaUserId"], message)
