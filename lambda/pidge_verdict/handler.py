"""WebSocket $default: sync, the contact's Yes / No / Undo / Reverse, the receiver's Confirm, case actions and questions
to Pidge, a browser's Web Push subscription, the receiver's contact verification code; SNS: phone-channel taps;
S3 asks/: a contact's reply to the ask by email.

The user comes from the connection row, never from the message body.
"""

import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote_plus

import boto3

from shared import cases, channels, chat, email_ask, graph, payloads, push_subscriptions, rcs, schedules, verdict, whatsapp
from shared.enums import (
    ActionOutcome, CaseEvent, Channel, ChatCardKind, ChatVia, ClientAction, EmailedAnswerOutcome, PushMessageType, RequestState,
    UserRole, VerdictAnswer, VerifiedVia,
)
from shared.metrics import emit_count_metric

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
MOCK_BANK_MAILBOX = os.environ.get("MOCK_BANK_MAILBOX", "")  # the SIMULATED bank; empty → Send to my bank is refused
CASES_ADDRESS = os.environ.get("CASES_ADDRESS", "")  # Reply-To on the report; Mail Manager stores replies under cases/
BANK_FOLLOW_UP_ENABLED = os.environ.get("BANK_FOLLOW_UP_ENABLED") == "true"
BANK_FOLLOW_UP_DELAY_SECONDS = int(os.environ.get("BANK_FOLLOW_UP_DELAY_SECONDS", "120"))
CASE_FOLLOWUP_FUNCTION_ARN = os.environ.get("CASE_FOLLOWUP_FUNCTION_ARN", "")
SYNC_CASE_LIMIT = 10
SYNC_EMAIL_LIMIT = 20
SYNC_REQUEST_LIMIT = 10
SYNC_NOTE_LIMIT = 20
SYNC_CHAT_LIMIT = 20
DEFAULT_CONTACT_VERIFICATION_MOCK_CODE = "4821"  # the PWA plays the reply with this code (onboarding.js); a real code is never configured here
HANDLED = {"statusCode": 200}
FORBIDDEN = {"statusCode": 403}
ANSWER_FOR_ACTION = {ClientAction.UNDO: VerdictAnswer.UNDO, ClientAction.REVERSE: VerdictAnswer.REVERSE}
EMAILED_ANSWER_DROPPED_METRIC_NAME = "EmailedAnswerDropped"

s3_client = boto3.client("s3")
ses_client = boto3.client("sesv2")


def handle(event, context):
    if "Records" in event:
        if event["Records"] and "s3" in event["Records"][0]:
            handle_emailed_answer_records(event["Records"])
        else:
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
    elif action is ClientAction.SEND_REPORT:
        handle_send_report(connection_id, user_id, body)
    elif action is ClientAction.CLOSE_CASE:
        handle_close_case(connection_id, user_id, body)
    elif action is ClientAction.SAVE_PUSH_SUBSCRIPTION:
        handle_save_push_subscription(connection_id, user_id, body)
    elif action is ClientAction.ASK_PIDGE:
        handle_ask_pidge(connection_id, user_id, body)
    elif action is ClientAction.VERIFY_CONTACT:
        handle_verify_contact(connection_id, user_id, body)
    elif action is ClientAction.CONFIRM:
        handle_confirm(connection_id, user_id, body)
    else:
        reject(connection_id, None, ActionOutcome.BAD_MESSAGE)  # a member nobody routed: never fall through to a write
    return HANDLED


def reject(connection_id: str, request_id: str | None, outcome: ActionOutcome) -> None:
    channels.push_to_connection(
        connection_id, {"type": PushMessageType.ACTION_REJECTED, "requestId": request_id, "reason": outcome}
    )


def contact_name_for(request_row: dict) -> str:
    contact_row = graph.get_contact_row(request_row["receiverEmail"], request_row["contactSK"])
    return contact_row["contactName"] if contact_row else request_row["claimedPerson"]


def handle_sync(connection_id: str, user_id: str) -> None:
    """Reconnect or reload: the role and the rows this user should see right now (a past releaseAt reads as confirmed)."""
    now = graph.now_timestamp()
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
        case_rows = graph.list_recent_rows(user_id, graph.CASE_ROW_KIND, SYNC_CASE_LIMIT)
        case_cards = [payloads.case_for_receiver(case_row, graph.list_case_replies(graph.case_id_of(case_row))) for case_row in case_rows]
        chat_messages = [chat.chat_message_for_sync(message_row) for message_row in chat.list_recent_messages(user_id, SYNC_CHAT_LIMIT)]
        circle = [payloads.circle_entry_for_receiver(contact_row) for contact_row in graph.list_contact_rows(RECEIVER_ADDRESS)]
        message = {"type": PushMessageType.SYNC_STATE, "role": UserRole.RECEIVER, "receiverName": RECEIVER_NAME, "emails": emails,
                   "notes": notes, "cases": case_cards, "chatEnabled": chat.is_chat_enabled(), "chat": chat_messages, "circle": circle}
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
    result = verdict.record_answer(request_id, answer, sender, f"inapp:{websocket_message_id}", graph.now_timestamp())
    if result.outcome is not ActionOutcome.RECORDED:
        reject(connection_id, request_id, result.outcome)
        return
    announce_recorded_answer(request_id, result)


def announce_recorded_answer(request_id: str, result: verdict.ActionResult) -> None:
    """Both PWAs learn the new status and Jimin gets the receipt (shared with the ask link); a Yes starts the release timer."""
    channels.announce_recorded_answer(result, RECEIVER_USER_ID, RECEIVER_NAME, RECEIPT_SENDER, ses_client)
    if result.status is RequestState.CONFIRMED_PENDING:
        schedule_release(request_id, result.request_row["releaseAt"])


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
            failed_status = whatsapp.parse_failed_status(sns_message)
            if failed_status is not None:  # Meta could not deliver our ask; the hold still counts down as if it was sent
                logger.warning("WhatsApp message %s failed with Meta error %s", failed_status.message_id, failed_status.error_code)
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
        result = verdict.record_answer(request_id, answer, sender, inbound_message_id, graph.now_timestamp())
        if result.outcome is not ActionOutcome.RECORDED:
            logger.info("%s answer on %s not recorded: %s", channel, request_id, result.outcome)
            continue
        announce_recorded_answer(request_id, result)
        if channel is Channel.WHATSAPP:
            acknowledge_on_whatsapp(request_id, result.status, sender.value)


def acknowledge_on_whatsapp(request_id: str, status: RequestState, destination_number: str) -> None:
    """Only after a tap on WhatsApp itself (it opens Meta's 24-h window); a failed send is logged, the answer already landed."""
    try:
        channels.acknowledge_on_whatsapp(request_id, status, destination_number, RECEIVER_NAME)
    except Exception:
        logger.exception("whatsapp acknowledgement for %s failed", request_id)


def handle_emailed_answer_records(records: list) -> None:
    """A contact's reply to the ask by email, stored by Mail Manager under asks/. Every drop is logged and counted."""
    for record in records:
        handle_emailed_answer_object(record["s3"]["bucket"]["name"], unquote_plus(record["s3"]["object"]["key"]))


def handle_emailed_answer_object(bucket_name: str, object_key: str) -> None:
    raw_email = s3_client.get_object(Bucket=bucket_name, Key=object_key)["Body"].read()
    emailed_answer = email_ask.read_emailed_answer(raw_email)
    if emailed_answer.outcome is not EmailedAnswerOutcome.ACCEPTED:
        logger.warning("emailed answer %s dropped: %s", object_key, emailed_answer.outcome)
        emit_count_metric(EMAILED_ANSWER_DROPPED_METRIC_NAME, "Outcome", str(emailed_answer.outcome))
        return
    sender = verdict.SenderIdentity(Channel.EMAIL, emailed_answer.sender_address)
    inbound_message_id = f"email:{emailed_answer.message_id or object_key}"
    result = verdict.record_answer(
        emailed_answer.request_id, emailed_answer.answer, sender, inbound_message_id, graph.now_timestamp()
    )
    if result.outcome is not ActionOutcome.RECORDED:
        logger.info("email answer on %s not recorded: %s", emailed_answer.request_id, result.outcome)
        emit_count_metric(EMAILED_ANSWER_DROPPED_METRIC_NAME, "Outcome", str(result.outcome))
        return
    announce_recorded_answer(emailed_answer.request_id, result)


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
        return
    channels.push_to_user(RECEIVER_USER_ID, payloads.case_state_message(result.email_row, [], CaseEvent.OPENED))


def case_id_from(body: dict) -> str | None:
    case_id = body.get("caseId")
    return case_id if isinstance(case_id, str) and case_id else None


def handle_send_report(connection_id: str, user_id: str, body: dict) -> None:
    """Send to my bank: code emails the SIMULATED bank mailbox and books the one reminder."""
    case_id = case_id_from(body)
    if case_id is None:
        reject(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    if not MOCK_BANK_MAILBOX or not CASES_ADDRESS:
        logger.error("MOCK_BANK_MAILBOX / CASES_ADDRESS not configured; Send to my bank refused")
        reject(connection_id, case_id, ActionOutcome.NOT_ALLOWED)
        return
    result = cases.send_report(case_id, user_id, RECEIVER_USER_ID, RECEIVER_NAME, ses_client, RECEIPT_SENDER, CASES_ADDRESS, MOCK_BANK_MAILBOX)
    if result.outcome is not ActionOutcome.RECORDED:
        reject(connection_id, case_id, result.outcome)
        return
    channels.push_to_user(RECEIVER_USER_ID, payloads.case_state_message(result.case_row, graph.list_case_replies(case_id), CaseEvent.REPORT_SENT))
    if BANK_FOLLOW_UP_ENABLED:
        try:
            fire_at = datetime.now(timezone.utc) + timedelta(seconds=BANK_FOLLOW_UP_DELAY_SECONDS)
            schedules.schedule_one_time_invocation(f"followup-{case_id}", fire_at, CASE_FOLLOWUP_FUNCTION_ARN, {"caseId": case_id})
        except Exception:
            logger.exception("follow-up schedule for %s failed; no reminder will be sent", case_id)


def handle_close_case(connection_id: str, user_id: str, body: dict) -> None:
    case_id = case_id_from(body)
    if case_id is None:
        reject(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    result = cases.close_case(case_id, user_id, RECEIVER_USER_ID)
    if result.outcome is not ActionOutcome.RECORDED:
        reject(connection_id, case_id, result.outcome)
        return
    channels.push_to_user(RECEIVER_USER_ID, payloads.case_state_message(result.case_row, graph.list_case_replies(case_id), CaseEvent.CLOSED))


def handle_save_push_subscription(connection_id: str, user_id: str, body: dict) -> None:
    """Stores this browser's Web Push subscription under the caller's own USER# row; only a refusal is answered."""
    outcome = push_subscriptions.save_subscription(user_id, body)
    if outcome is not ActionOutcome.RECORDED:
        reject(connection_id, None, outcome)


def handle_ask_pidge(connection_id: str, user_id: str, body: dict) -> None:
    """The receiver's question: cap and grandma row here; pidge-dispatch answers from the stream. A refusal is a reply bubble."""
    if user_id != RECEIVER_USER_ID:
        reject(connection_id, None, ActionOutcome.NOT_ALLOWED)
        return
    accepted = chat.accept_question(user_id, body.get("text"), ChatVia.WINDOW)
    if accepted.refusal_text is not None:
        logger.info("question from %s refused: %s", user_id, accepted.refusal_text)
        channels.push_to_connection(connection_id, chat.pidge_reply_message(None, accepted.refusal_text, ChatCardKind.REFUSAL))
        return
    logger.info("question %s stored for %s (%d characters)", accepted.message_row["SK"], user_id, len(accepted.message_row["text"]))


def is_contact_verification_mock_on() -> bool:
    """Read per call: no real channel carries the code yet, so verification works only in mock mode."""
    return os.environ.get("CONTACT_VERIFICATION_MOCK") == "true"


def handle_verify_contact(connection_id: str, user_id: str, body: dict) -> None:
    """Onboarding's code step: the receiver only, mock mode only; the contact is looked up server-side, then sync is re-sent."""
    if user_id != RECEIVER_USER_ID or not is_contact_verification_mock_on():
        reject(connection_id, None, ActionOutcome.NOT_ALLOWED)
        return
    contact_user_id, code = body.get("contactUserId"), body.get("code")
    if not isinstance(contact_user_id, str) or not isinstance(code, str):
        reject(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    contact_row = graph.find_contact_row_for_user(RECEIVER_ADDRESS, contact_user_id)
    if contact_row is None or not hmac.compare_digest(code.encode(), DEFAULT_CONTACT_VERIFICATION_MOCK_CODE.encode()):
        logger.info("contact verification refused for %s: %s", user_id, "unknown contact" if contact_row is None else "wrong code")
        reject(connection_id, None, ActionOutcome.NOT_APPLIED)
        return
    if not graph.mark_contact_verified(RECEIVER_ADDRESS, contact_row["SK"], VerifiedVia.MOCK, graph.now_timestamp()):
        reject(connection_id, None, ActionOutcome.NOT_APPLIED)  # the row vanished between the lookup and the update
        return
    logger.info("contact %s verified via %s", contact_row["SK"], VerifiedVia.MOCK)
    handle_sync(connection_id, user_id)
