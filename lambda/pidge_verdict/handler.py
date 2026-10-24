"""WebSocket $default: sync, the contact's Yes / No / Undo / Reverse, the receiver's Confirm; SNS: phone-channel taps.

The user comes from the connection row, never from the message body.
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3

from shared import channels, graph, payloads, rcs, schedules, verdict, whatsapp
from shared.enums import ActionOutcome, Channel, ClientAction, PushMessageType, RequestState, UserRole, VerdictAnswer

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RECEIVER_ADDRESS = os.environ["RECEIVER_ADDRESS"]
RECEIVER_USER_ID = os.environ["RECEIVER_USER_ID"]
RECEIVER_NAME = os.environ["RECEIVER_NAME"]
EMAIL_BUCKET = os.environ["EMAIL_BUCKET"]
RECEIPT_SENDER = os.environ["RECEIPT_SENDER"]
RCS_TOPIC_ARN = os.environ["RCS_TOPIC_ARN"]
WHATSAPP_TOPIC_ARN = os.environ["WHATSAPP_TOPIC_ARN"]
RELEASE_SCHEDULER_ENABLED = os.environ.get("RELEASE_SCHEDULER_ENABLED") == "true"
RELEASE_FUNCTION_ARN = os.environ.get("RELEASE_FUNCTION_ARN", "")
SYNC_EMAIL_LIMIT = 20
SYNC_REQUEST_LIMIT = 10
SYNC_NOTE_LIMIT = 20
HANDLED = {"statusCode": 200}
FORBIDDEN = {"statusCode": 403}
ANSWER_FOR_ACTION = {ClientAction.UNDO: VerdictAnswer.UNDO, ClientAction.REVERSE: VerdictAnswer.REVERSE}

s3_client = boto3.client("s3")
ses_client = boto3.client("sesv2")


def handle(event, context):
    if "Records" in event:
        handle_sns_records(event["Records"])
        return HANDLED
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
        handle_answer(connection_id, user_id, body, request_context["messageId"], parse_answer(body))
    elif action in ANSWER_FOR_ACTION:
        handle_answer(connection_id, user_id, body, request_context["messageId"], ANSWER_FOR_ACTION[action])
    elif action is ClientAction.REPORT_PAYMENT:
        handle_report_payment(connection_id, user_id, body)
    else:
        handle_confirm(connection_id, user_id, body)
    return HANDLED


def now_timestamp() -> str:
    return graph.iso_timestamp(datetime.now(timezone.utc))


def reject(connection_id: str, request_id: str | None, outcome: ActionOutcome) -> None:
    channels.push_to_connection(
        connection_id, {"type": PushMessageType.ACTION_REJECTED, "requestId": request_id, "reason": outcome}
    )


def contact_name_for(request_row: dict) -> str:
    contact_row = graph.get_contact_row(request_row["receiverEmail"], request_row["contactSK"])
    return contact_row["contactName"] if contact_row else request_row["claimedPerson"]


def handle_sync(connection_id: str, user_id: str) -> None:
    """Reconnect or reload: the role and the rows this user should see right now (a past releaseAt reads as confirmed)."""
    now = now_timestamp()
    if user_id == RECEIVER_USER_ID:
        emails = []
        for email_row in graph.list_recent_rows(user_id, graph.EMAIL_ROW_KIND, SYNC_EMAIL_LIMIT):
            if "state" not in email_row:
                continue  # the Reader is still on it; its push will follow
            request_row = graph.get_request(email_row["requestId"]) if email_row.get("requestId") else None
            contact_name = contact_name_for(request_row) if request_row else None
            status = verdict.effective_status(request_row, now) if request_row else None
            emails.append(payloads.email_for_receiver(email_row, request_row, contact_name, status))
        notes = [payloads.note_for_receiver(note_row) for note_row in graph.list_recent_rows(user_id, graph.NOTE_ROW_KIND, SYNC_NOTE_LIMIT)]
        message = {"type": PushMessageType.SYNC_STATE, "role": UserRole.RECEIVER, "emails": emails, "notes": notes}
    else:
        cards = []
        for request_row in graph.list_recent_rows(user_id, graph.REQUEST_ROW_KIND, SYNC_REQUEST_LIMIT):
            email_row = graph.get_email(request_row["emailId"])
            cards.append(payloads.card_for_contact(request_row, email_row, RECEIVER_NAME, verdict.effective_status(request_row, now)))
        message = {"type": PushMessageType.SYNC_STATE, "role": UserRole.CONTACT, "requests": cards}
    channels.push_to_connection(connection_id, message)


def parse_answer(body: dict) -> VerdictAnswer | None:
    """The in-app Yes / No; undo and reverse have their own actions."""
    try:
        answer = VerdictAnswer(body.get("answer"))
    except ValueError:
        return None
    return answer if answer in (VerdictAnswer.YES, VerdictAnswer.NO) else None


def handle_answer(connection_id: str, user_id: str, body: dict, websocket_message_id: str, answer: VerdictAnswer | None) -> None:
    request_id = body.get("requestId")
    if answer is None or not isinstance(request_id, str):
        reject(connection_id, request_id if isinstance(request_id, str) else None, ActionOutcome.BAD_MESSAGE)
        return
    sender = verdict.SenderIdentity(Channel.INAPP, user_id)
    result = verdict.record_answer(request_id, answer, sender, f"inapp:{websocket_message_id}", now_timestamp())
    if result.outcome is not ActionOutcome.RECORDED:
        reject(connection_id, request_id, result.outcome)
        return
    announce_recorded_answer(request_id, result)


def announce_recorded_answer(request_id: str, result: verdict.ActionResult) -> None:
    """Both PWAs learn the new status; a Yes starts the release timer; a final state emails Jimin the receipt."""
    message = payloads.request_state_message(result.request_row, result.status, result.contact_row["contactName"], False)
    channels.push_to_user(RECEIVER_USER_ID, message)
    channels.push_to_user(result.contact_row["contactPwaUserId"], message)
    if result.status is RequestState.CONFIRMED_PENDING:
        schedule_release(request_id, result.request_row["releaseAt"])
    try:  # the verdict already reached the receiver; a failed receipt is logged, never blocking
        verdict.send_receipt(ses_client, RECEIPT_SENDER, result.contact_row, result.status, RECEIVER_NAME)
    except Exception:
        logger.exception("receipt for %s failed", request_id)


def schedule_release(request_id: str, release_at: str) -> None:
    """Flag off: nothing is scheduled and the release happens on the next read (verdict.effective_status)."""
    if not RELEASE_SCHEDULER_ENABLED:
        return
    try:
        schedules.schedule_one_time_invocation(
            f"release-{request_id}", verdict.parse_timestamp(release_at), RELEASE_FUNCTION_ARN, {"requestId": request_id}
        )
    except Exception:
        logger.exception("release schedule for %s failed; the next read releases it", request_id)


def parse_phone_reply(record: dict) -> tuple[Channel, verdict.SenderIdentity, str, VerdictAnswer, str] | None:
    """Routes one SNS record by its topic: the RCS two-way topic or the WhatsApp event topic. Never by content."""
    topic_arn = record["Sns"]["TopicArn"]
    sns_message = json.loads(record["Sns"]["Message"])
    if topic_arn == RCS_TOPIC_ARN:
        reply = rcs.parse_inbound_reply(sns_message)
        if reply is None:
            return None
        sender = verdict.SenderIdentity(Channel.RCS, reply.origination_number)
        return Channel.RCS, sender, reply.request_id, reply.answer, f"rcs:{reply.inbound_message_id}"
    if topic_arn == WHATSAPP_TOPIC_ARN:
        reply = whatsapp.parse_inbound_reply(sns_message)
        if reply is None:
            return None
        sender = verdict.SenderIdentity(Channel.WHATSAPP, reply.sender_number)
        return Channel.WHATSAPP, sender, reply.request_id, reply.answer, f"whatsapp:{reply.message_id}"
    logger.warning("SNS record from unexpected topic %s ignored", topic_arn)
    return None


def handle_sns_records(records: list) -> None:
    """Taps from the phone channels. Nothing here raises: SNS would only redeliver the same tap."""
    for record in records:
        try:
            parsed = parse_phone_reply(record)
        except (KeyError, TypeError, ValueError):
            logger.warning("unreadable SNS record ignored")
            continue
        if parsed is None:
            continue
        channel, sender, request_id, answer, inbound_message_id = parsed
        result = verdict.record_answer(request_id, answer, sender, inbound_message_id, now_timestamp())
        if result.outcome is not ActionOutcome.RECORDED:
            logger.info("%s answer on %s not recorded: %s", channel, request_id, result.outcome)
            continue
        announce_recorded_answer(request_id, result)


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


def handle_report_payment(connection_id: str, user_id: str, body: dict) -> None:
    """Grandma's "I already paid": the CASE# row is written here; the stream wakes the case agent for the pack."""
    try:
        report = verdict.PaymentReport.from_client_body(body)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        reject(connection_id, body.get("requestId") if isinstance(body.get("requestId"), str) else None, ActionOutcome.BAD_MESSAGE)
        return
    result = verdict.report_payment(report, user_id, RECEIVER_USER_ID, RECEIVER_ADDRESS)
    if result.outcome is not ActionOutcome.RECORDED:
        reject(connection_id, report.request_id, result.outcome)
