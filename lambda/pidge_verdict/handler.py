"""WebSocket $default: sync (an org admin's in org_admin_handlers), the contact's Yes / No / Undo / Reverse, the receiver's
Confirm, case actions and questions to Pidge, a browser's Web Push subscription, the receiver's Add-someone form and demo
code reply (circle_handlers), the receiver's home visibility and office row (org_preference_handlers); SNS: phone-channel
taps and a pending contact's code typed on WhatsApp; S3 asks/: a contact's reply to the ask by email.

The user comes from the connection row, never from the message body; their role and resident (or org) come from their own
USER#<id>/PROFILE row, and an answer's resident from the REQUEST# row's receiverEmail.
"""

import json
import logging
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote_plus

import boto3

from pidge_verdict import circle_handlers, org_admin_handlers, org_preference_handlers
from shared import (
    cases, channels, chat, email_ask, graph, inbox_rows, org_preferences, orgs, payloads, profiles, push_subscriptions, rcs, schedules,
    verdict, whatsapp,
)
from shared.enums import (
    ActionOutcome, CaseEvent, Channel, ChatCardKind, ChatVia, ClientAction, EmailedAnswerOutcome, PushMessageType, RequestState,
    UserRole, VerdictAnswer,
)
from shared.metrics import emit_count_metric
from shared.org_ids import is_org_user_id, org_id_of_org_user
from shared.profiles import ReceiverProfile, UnknownReceiverError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

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
SYNC_MESSAGE_LIMIT = 20
SYNC_REQUEST_LIMIT = 10
SYNC_NOTE_LIMIT = 20
SYNC_CHAT_LIMIT = 20
HANDLED = {"statusCode": 200}
FORBIDDEN = {"statusCode": 403}
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
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return HANDLED
    if action in RESIDENT_ACTION_HANDLERS:
        route_resident_action(connection_id, user_id, action, body)
    elif action in ACTION_ROUTES:
        ACTION_ROUTES[action](connection_id, user_id, body, request_context["messageId"])
    else:
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)  # a member nobody routed: never fall through to a write
    return HANDLED


def route_resident_action(connection_id: str, user_id: str, action: ClientAction, body: dict) -> None:
    """Authorization first: the caller's own resident from USER#<id>/PROFILE (role RECEIVER, named back by RECEIVER#), never
    an id from the body. Anyone else is NOT_ALLOWED before the action reads the body."""
    resident = profiles.require_resident_caller(user_id)
    if resident is None:
        channels.reject_action(connection_id, None, ActionOutcome.NOT_ALLOWED)
        return
    RESIDENT_ACTION_HANDLERS[action](connection_id, resident, body)


def contact_name_for(request_row: dict) -> str:
    contact_row = graph.get_contact_row(request_row["receiverEmail"], request_row["contactSK"])
    return contact_row["contactName"] if contact_row else request_row["claimedPerson"]


def handle_sync(connection_id: str, user_id: str) -> None:
    """Reconnect or reload: the role from the caller's own PROFILE row and the rows that role should see right now (a past
    releaseAt reads as confirmed). No PROFILE, an unknown role, an unmatched resident, an org admin without a complete
    PROFILE + ORG#<orgId>/ADMIN# pair (orgs.require_org_admin) or whose org has no PROFILE: NOT_ALLOWED."""
    sync_message = build_sync_message(user_id)
    if sync_message is None:
        channels.reject_action(connection_id, None, ActionOutcome.NOT_ALLOWED)
        return
    channels.push_to_connection(connection_id, sync_message)


def build_sync_message(user_id: str) -> dict | None:
    """The sync_state for the caller's own role; None when they may see nothing (each refusal is logged and counted where it
    is found). An org admin's org comes from their PROFILE and ADMIN# rows, never from the message body."""
    user_profile = profiles.get_user_profile(user_id)
    role = user_profile.role if user_profile is not None else None
    if role is UserRole.RECEIVER:
        resident = profiles.require_resident_caller(user_id)
        return receiver_sync_message(resident) if resident is not None else None
    if role is UserRole.CONTACT:
        return contact_sync_message(user_id)
    if role is UserRole.ORG_ADMIN:
        org_admin = orgs.require_org_admin(user_id)
        return org_admin_handlers.org_admin_sync_message(org_admin) if org_admin is not None else None
    return None  # no PROFILE or an unknown role: get_user_profile logged and counted it


def inbox_payloads_for_sync(
    user_id: str, row_kind: str, limit: int, reading_payload: Callable[[dict], dict],
    decided_payload: Callable[[dict, dict | None, str | None, RequestState | None], dict], now: str,
) -> list[dict]:
    """The newest EMAIL# or MESSAGE# rows of one kind: a text she sent as its out payload, a row still being read as its
    reading payload, a decided row with the request it holds (if any), that request's contact name and effective status."""
    inbox_payloads = []
    for inbox_row in graph.list_recent_rows(user_id, row_kind, limit):
        if payloads.is_out_message_row(inbox_row):
            inbox_payloads.append(payloads.out_message_for_receiver(inbox_row))  # no Reader, no verdict, no request
            continue
        if "state" not in inbox_row:
            inbox_payloads.append(reading_payload(inbox_row))  # the Reader is on it, or its retry will be
            continue
        request_row = graph.get_request(inbox_row["requestId"]) if inbox_row.get("requestId") else None
        contact_name = contact_name_for(request_row) if request_row else None
        status = verdict.effective_status(request_row, now) if request_row else None
        inbox_payloads.append(decided_payload(inbox_row, request_row, contact_name, status))
    return inbox_payloads


def receiver_sync_message(resident: ReceiverProfile) -> dict:
    now = graph.now_timestamp()
    user_id = resident.receiver_user_id
    emails = inbox_payloads_for_sync(user_id, graph.EMAIL_ROW_KIND, SYNC_EMAIL_LIMIT, payloads.reading_email_for_receiver,
                                     payloads.email_for_receiver, now)
    messages = inbox_payloads_for_sync(user_id, inbox_rows.MESSAGE_ROW_KIND, SYNC_MESSAGE_LIMIT,
                                       payloads.reading_message_for_receiver, payloads.message_for_receiver, now)
    notes = [payloads.note_for_receiver(note_row) for note_row in graph.list_recent_rows(user_id, graph.NOTE_ROW_KIND, SYNC_NOTE_LIMIT)]
    case_rows = graph.list_recent_rows(user_id, graph.CASE_ROW_KIND, SYNC_CASE_LIMIT)
    case_cards = [payloads.case_for_receiver(case_row, graph.list_case_replies(graph.case_id_of(case_row))) for case_row in case_rows]
    chat_messages = [chat.chat_message_for_sync(message_row) for message_row in chat.list_recent_messages(user_id, SYNC_CHAT_LIMIT)]
    circle_settings = circle_handlers.read_settings()
    return {"type": PushMessageType.SYNC_STATE, "role": UserRole.RECEIVER, "receiverName": resident.resident_name, "emails": emails,
            "messages": messages, "notes": notes, "cases": case_cards, "chatEnabled": chat.is_chat_enabled(), "chat": chat_messages,
            "circle": circle_handlers.circle_entries(circle_settings, resident),
            "contactVerification": circle_handlers.verification_flags(circle_settings),
            "orgPreference": org_preferences.org_preference_for_sync(resident)}


def contact_sync_message(user_id: str) -> dict:
    return {"type": PushMessageType.SYNC_STATE, "role": UserRole.CONTACT, "requests": contact_cards_for(user_id)}


def contact_cards_for(user_id: str) -> list:
    """The cards asked of this PWA user (a contact, or an org's org-<orgId> user), each named for its own resident; a card
    whose resident has no profile is skipped (and counted), and an org's card whose resident's orgId is not that org
    (orgs.is_residents_own_org: a former home after re-enrolment) is skipped (logged)."""
    now = graph.now_timestamp()
    card_org_id = org_id_of_org_user(user_id) if is_org_user_id(user_id) else None
    residents_by_address: dict[str, ReceiverProfile] = {}
    cards = []
    for request_row in graph.list_recent_rows(user_id, graph.REQUEST_ROW_KIND, SYNC_REQUEST_LIMIT):
        receiver_address = request_row["receiverEmail"]
        if receiver_address not in residents_by_address:
            try:
                residents_by_address[receiver_address] = profiles.require_receiver_profile(receiver_address)
            except UnknownReceiverError:
                logger.warning("request %s skipped in sync: its receiver has no profile", graph.request_id_of(request_row))
                continue
        resident = residents_by_address[receiver_address]
        if card_org_id is not None and not orgs.is_residents_own_org(resident, card_org_id):
            logger.info("request %s skipped in %s's sync: its resident's home is another org", graph.request_id_of(request_row), user_id)
            continue
        email_row = inbox_rows.require_held_row(request_row)
        cards.append(payloads.card_for_contact(request_row, email_row, resident.resident_name, verdict.effective_status(request_row, now)))
    return cards


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
        channels.reject_action(connection_id, request_id if isinstance(request_id, str) else None, ActionOutcome.BAD_MESSAGE)
        return
    request_row = graph.get_request(request_id)
    try:
        resident = resident_of_request_row(request_row)
    except UnknownReceiverError:
        channels.reject_action(connection_id, request_id, ActionOutcome.NOT_ALLOWED)
        return
    if request_row is not None:
        org_admin_handlers.audit_office_answer(user_id, request_row)  # an office answer is audited first: a failed write raises
    sender = verdict.SenderIdentity(Channel.INAPP, user_id)
    result = verdict.record_answer(request_id, answer, sender, f"inapp:{websocket_message_id}", graph.now_timestamp())
    if result.outcome is not ActionOutcome.RECORDED:
        channels.reject_action(connection_id, request_id, result.outcome)
        return
    announce_recorded_answer(request_id, result, resident)


def resident_of_request(request_id: str) -> ReceiverProfile | None:
    """The resident the REQUEST# row's receiverEmail names, read before any answer is written. None when there is no such
    request (record_answer then says UNKNOWN_REQUEST); raises UnknownReceiverError (counted) when the address has no profile."""
    return resident_of_request_row(graph.get_request(request_id))


def resident_of_request_row(request_row: dict | None) -> ReceiverProfile | None:
    """resident_of_request for a REQUEST# row already read (None: no such request)."""
    if request_row is None:
        return None
    return profiles.require_receiver_profile(request_row["receiverEmail"])


def announce_recorded_answer(request_id: str, result: verdict.ActionResult, resident: ReceiverProfile) -> None:
    """Both PWAs learn the new status and Jimin gets the receipt (shared with the ask link); a Yes starts the release timer.
    Only called after a RECORDED answer, so the request existed and resident is set."""
    channels.announce_recorded_answer(result, resident.receiver_user_id, resident.resident_name, RECEIPT_SENDER, ses_client)
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


def parse_phone_reply(topic_arn: str, sns_message: dict) -> tuple[Channel, verdict.SenderIdentity, str, VerdictAnswer, str] | None:
    """Routes one SNS message by its topic: the RCS two-way topic or the WhatsApp event topic. Never by content."""
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


def whatsapp_text_of(topic_arn: str, sns_message: dict) -> whatsapp.InboundText | None:
    """Typed text on Pidge's WhatsApp number (the only channel that carries one); taps and statuses → None."""
    if topic_arn != WHATSAPP_TOPIC_ARN:
        return None
    return whatsapp.parse_inbound_text(sns_message)


def handle_sns_records(records: list) -> None:
    """Taps from the phone channels, and a pending contact's code typed on WhatsApp. An unreadable record is skipped, never
    raised (SNS would only redeliver it); a failing table write still raises so the record is redelivered."""
    for record in records:
        try:
            topic_arn, sns_message = record["Sns"]["TopicArn"], json.loads(record["Sns"]["Message"])
            inbound_text = whatsapp_text_of(topic_arn, sns_message)
            parsed = None if inbound_text is not None else parse_phone_reply(topic_arn, sns_message)
        except (KeyError, TypeError, ValueError):
            logger.warning("unreadable SNS record ignored")
            continue
        if inbound_text is not None:
            circle_handlers.handle_whatsapp_text(inbound_text)
            continue
        if parsed is None:
            continue
        channel, sender, request_id, answer, inbound_message_id = parsed
        resident = resident_of_request(request_id)  # no profile raises: the record is redelivered
        result = verdict.record_answer(request_id, answer, sender, inbound_message_id, graph.now_timestamp())
        if result.outcome is not ActionOutcome.RECORDED:
            logger.info("%s answer on %s not recorded: %s", channel, request_id, result.outcome)
            continue
        announce_recorded_answer(request_id, result, resident)
        if channel is Channel.WHATSAPP:
            acknowledge_on_whatsapp(request_id, result.status, sender.value, resident.resident_name)


def acknowledge_on_whatsapp(request_id: str, status: RequestState, destination_number: str, resident_name: str) -> None:
    """Only after a tap on WhatsApp itself (it opens Meta's 24-h window); a failed send is logged, the answer already landed."""
    try:
        channels.acknowledge_on_whatsapp(request_id, status, destination_number, resident_name)
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
    resident = resident_of_request(emailed_answer.request_id)  # no profile raises: the S3 event is retried
    sender = verdict.SenderIdentity(Channel.EMAIL, emailed_answer.sender_address)
    inbound_message_id = f"email:{emailed_answer.message_id or object_key}"
    result = verdict.record_answer(
        emailed_answer.request_id, emailed_answer.answer, sender, inbound_message_id, graph.now_timestamp()
    )
    if result.outcome is not ActionOutcome.RECORDED:
        logger.info("email answer on %s not recorded: %s", emailed_answer.request_id, result.outcome)
        emit_count_metric(EMAILED_ANSWER_DROPPED_METRIC_NAME, "Outcome", str(result.outcome))
        return
    announce_recorded_answer(emailed_answer.request_id, result, resident)


def handle_confirm(connection_id: str, resident: ReceiverProfile, body: dict) -> None:
    """After a No: the held email or text is quarantined and both PWAs hear it (the resident alone, named after their current
    home, when the office the request was held for is no longer reached: orgs.read_request_contact_row)."""
    request_id = body.get("requestId")
    if not isinstance(request_id, str):
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    result = verdict.confirm_quarantine(
        request_id, resident.receiver_user_id, resident.receiver_user_id, resident.receiver_address, s3_client, EMAIL_BUCKET
    )
    if result.outcome is not ActionOutcome.RECORDED:
        channels.reject_action(connection_id, request_id, result.outcome)
        return
    contact_row = orgs.read_request_contact_row(result.request_row, resident)
    message = payloads.request_state_message(result.request_row, result.status, orgs.contact_name_for_request(contact_row, resident), True)
    channels.push_to_user(resident.receiver_user_id, message)
    if contact_row is not None:
        channels.push_to_user(contact_row["contactPwaUserId"], message)


def handle_report_payment(connection_id: str, resident: ReceiverProfile, body: dict) -> None:
    """Grandma's "I already paid": the CASE# row is written here; the stream wakes the case agent for the pack."""
    try:
        report = verdict.PaymentReport.from_client_body(body)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        channels.reject_action(connection_id, body.get("requestId") if isinstance(body.get("requestId"), str) else None, ActionOutcome.BAD_MESSAGE)
        return
    result = verdict.report_payment(report, resident.receiver_user_id, resident.receiver_user_id, resident.receiver_address)
    if result.outcome is not ActionOutcome.RECORDED:
        channels.reject_action(connection_id, report.request_id, result.outcome)
        return
    channels.push_to_user(resident.receiver_user_id, payloads.case_state_message(result.email_row, [], CaseEvent.OPENED))


def case_id_from(body: dict) -> str | None:
    case_id = body.get("caseId")
    return case_id if isinstance(case_id, str) and case_id else None


def handle_send_report(connection_id: str, resident: ReceiverProfile, body: dict) -> None:
    """Send to my bank: code emails the SIMULATED bank mailbox and books the one reminder."""
    case_id = case_id_from(body)
    if case_id is None:
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    if not MOCK_BANK_MAILBOX or not CASES_ADDRESS:
        logger.error("MOCK_BANK_MAILBOX / CASES_ADDRESS not configured; Send to my bank refused")
        channels.reject_action(connection_id, case_id, ActionOutcome.NOT_ALLOWED)
        return
    result = cases.send_report(case_id, resident.receiver_user_id, resident, ses_client, RECEIPT_SENDER, CASES_ADDRESS, MOCK_BANK_MAILBOX)
    if result.outcome is not ActionOutcome.RECORDED:
        channels.reject_action(connection_id, case_id, result.outcome)
        return
    channels.push_to_user(resident.receiver_user_id, payloads.case_state_message(result.case_row, graph.list_case_replies(case_id), CaseEvent.REPORT_SENT))
    if BANK_FOLLOW_UP_ENABLED:
        try:
            fire_at = datetime.now(timezone.utc) + timedelta(seconds=BANK_FOLLOW_UP_DELAY_SECONDS)
            schedules.schedule_one_time_invocation(f"followup-{case_id}", fire_at, CASE_FOLLOWUP_FUNCTION_ARN, {"caseId": case_id})
        except Exception:
            logger.exception("follow-up schedule for %s failed; no reminder will be sent", case_id)


def handle_close_case(connection_id: str, resident: ReceiverProfile, body: dict) -> None:
    case_id = case_id_from(body)
    if case_id is None:
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    result = cases.close_case(case_id, resident.receiver_user_id, resident)
    if result.outcome is not ActionOutcome.RECORDED:
        channels.reject_action(connection_id, case_id, result.outcome)
        return
    channels.push_to_user(resident.receiver_user_id, payloads.case_state_message(result.case_row, graph.list_case_replies(case_id), CaseEvent.CLOSED))


def handle_save_push_subscription(connection_id: str, user_id: str, body: dict) -> None:
    """Stores this browser's Web Push subscription under the caller's own USER# row; only a refusal is answered."""
    outcome = push_subscriptions.save_subscription(user_id, body)
    if outcome is not ActionOutcome.RECORDED:
        channels.reject_action(connection_id, None, outcome)


def handle_ask_pidge(connection_id: str, resident: ReceiverProfile, body: dict) -> None:
    """The resident's question: cap and grandma row here; pidge-dispatch answers from the stream. A refusal is a reply bubble."""
    user_id = resident.receiver_user_id
    accepted = chat.accept_question(user_id, body.get("text"), ChatVia.WINDOW)
    if accepted.refusal_text is not None:
        logger.info("question from %s refused: %s", user_id, accepted.refusal_text)
        channels.push_to_connection(connection_id, chat.pidge_reply_message(None, accepted.refusal_text, ChatCardKind.REFUSAL))
        return
    logger.info("question %s stored for %s (%d characters)", accepted.message_row["SK"], user_id, len(accepted.message_row["text"]))


def route_sync(connection_id: str, user_id: str, _body: dict, _websocket_message_id: str) -> None:
    handle_sync(connection_id, user_id)


def route_answer(connection_id: str, user_id: str, body: dict, websocket_message_id: str) -> None:
    handle_answer(connection_id, user_id, body, websocket_message_id, parse_answer(body))


def route_undo(connection_id: str, user_id: str, body: dict, websocket_message_id: str) -> None:
    handle_answer(connection_id, user_id, body, websocket_message_id, VerdictAnswer.UNDO)


def route_reverse(connection_id: str, user_id: str, body: dict, websocket_message_id: str) -> None:
    handle_answer(connection_id, user_id, body, websocket_message_id, VerdictAnswer.REVERSE)


def route_save_push_subscription(connection_id: str, user_id: str, body: dict, _websocket_message_id: str) -> None:
    handle_save_push_subscription(connection_id, user_id, body)


RESIDENT_ACTION_HANDLERS = {  # each acts for the caller's own resident, resolved first by route_resident_action
    ClientAction.CONFIRM: handle_confirm,
    ClientAction.REPORT_PAYMENT: handle_report_payment,
    ClientAction.SEND_REPORT: handle_send_report,
    ClientAction.CLOSE_CASE: handle_close_case,
    ClientAction.ASK_PIDGE: handle_ask_pidge,
    ClientAction.ADD_CONTACT: circle_handlers.handle_add_contact,
    ClientAction.VERIFY_CONTACT: circle_handlers.handle_verify_contact,
    ClientAction.EDIT_CONTACT: circle_handlers.handle_edit_contact,
    ClientAction.SET_ORG_VISIBILITY: org_preference_handlers.handle_set_org_visibility,
    ClientAction.ADD_ORG_CONTACT: org_preference_handlers.handle_add_org_contact,
    ClientAction.REMOVE_ORG_CONTACT: org_preference_handlers.handle_remove_org_contact,
}
ACTION_ROUTES = {  # any role; the caller is the connection's user, never an id from the body
    ClientAction.SYNC: route_sync,
    ClientAction.ANSWER: route_answer,
    ClientAction.UNDO: route_undo,
    ClientAction.REVERSE: route_reverse,
    ClientAction.SAVE_PUSH_SUBSCRIPTION: route_save_push_subscription,
}
ROUTED_ACTIONS = frozenset(ACTION_ROUTES) | frozenset(RESIDENT_ACTION_HANDLERS)
