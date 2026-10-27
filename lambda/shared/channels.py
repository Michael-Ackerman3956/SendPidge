"""Sends: WebSocket pushes to a user's open PWAs, and the 'Was this you?' ask on every channel."""

import asyncio
import json
import logging
import os
import time
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from shared import ask_link, email_ask, graph, orgs, payloads, rcs, verdict, whatsapp
from shared.enums import ActionOutcome, Channel, PushMessageType, RequestState

logger = logging.getLogger(__name__)

_management_client = None
_rcs_client = None
_whatsapp_client = None
_ses_client = None

RCS_ENABLED = os.environ.get("RCS_ENABLED") == "true"
WHATSAPP_ENABLED = os.environ.get("WHATSAPP_ENABLED") == "true"
EMAIL_ASK_ENABLED = os.environ.get("EMAIL_ASK_ENABLED") == "true"


def get_management_client():
    """API Gateway management client for the WebSocket stage's callback URL."""
    global _management_client
    if _management_client is None:
        _management_client = boto3.client("apigatewaymanagementapi", endpoint_url=os.environ["WEBSOCKET_ENDPOINT"])
    return _management_client


def json_value_for_push(value):
    """DynamoDB hands numbers back as Decimal; the PWA gets plain JSON numbers."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def encode_push_message(message: dict) -> bytes:
    return json.dumps(message, default=json_value_for_push).encode()


def push_to_user(user_id: str, message: dict, should_remove_gone_connections: bool = True) -> int:
    """Pushes to every open connection of the user, and for an org user (org-<orgId>) of each of its admins instead; returns
    how many received it. A gone connection is removed unless should_remove_gone_connections is False (a caller whose role
    may not delete CONN#/USER# rows)."""
    client = get_management_client()
    message_bytes = encode_push_message(message)
    delivered_count = 0
    for recipient_user_id in orgs.recipient_user_ids_for(user_id):
        for connection_id in graph.list_connection_ids(recipient_user_id):
            try:
                client.post_to_connection(ConnectionId=connection_id, Data=message_bytes)
                delivered_count += 1
            except client.exceptions.GoneException:
                if should_remove_gone_connections:
                    graph.remove_connection(connection_id)
            except ClientError:
                logger.exception("push to connection %s of %s failed", connection_id, recipient_user_id)
    return delivered_count


def ask_in_app(request_row: dict, contact: graph.Contact, receiver_name: str, destination: str) -> Channel:
    """The request row is already stored, so the contact's PWA shows it on sync; the live push is a bonus."""
    email_row = graph.get_email(request_row["emailId"])
    card = payloads.verification_card_message(request_row, email_row, receiver_name, RequestState(request_row["status"]))
    delivered_count = push_to_user(destination, card)
    logger.info("in-app ask %s pushed to %d open connection(s)", graph.request_id_of(request_row), delivered_count)
    return Channel.INAPP


def get_rcs_client():
    global _rcs_client
    if _rcs_client is None:
        _rcs_client = boto3.client("pinpoint-sms-voice-v2")
    return _rcs_client


def ask_on_rcs(request_row: dict, contact: graph.Contact, receiver_name: str, destination: str) -> Channel:
    """Rich card with Yes / No chips from the RCS agent; the tap comes back on the two-way SNS topic."""
    request_id = graph.request_id_of(request_row)
    email_row = graph.get_email(request_row["emailId"]) or {}
    card = rcs.build_rich_card(request_id, email_row, contact.contact_name, receiver_name, os.environ["PIDGE_IMAGE_URL"])
    response = get_rcs_client().send_rcs_message(
        DestinationPhoneNumber=destination,
        OriginationIdentity=os.environ["RCS_AGENT_ID"],
        RcsMessageContent=card,
        TimeToLive=rcs.RICH_CARD_TIME_TO_LIVE_SECONDS,
    )
    graph.record_rcs_message_id(request_id, response["MessageId"])
    logger.info("rcs ask %s sent as %s", request_id, response["MessageId"])
    return Channel.RCS


def get_whatsapp_client():
    global _whatsapp_client
    if _whatsapp_client is None:
        _whatsapp_client = boto3.client("socialmessaging")
    return _whatsapp_client


def send_whatsapp_message(message: dict) -> str:
    """Sends one Meta message JSON from Pidge's WhatsApp number; returns Meta's message id. Failures raise."""
    response = get_whatsapp_client().send_whatsapp_message(
        originationPhoneNumberId=os.environ["WHATSAPP_PHONE_NUMBER_ID"],
        metaApiVersion=os.environ["WHATSAPP_META_API_VERSION"],
        message=json.dumps(message).encode(),
    )
    return response["messageId"]


def ask_on_whatsapp(request_row: dict, contact: graph.Contact, receiver_name: str, destination: str) -> Channel:
    """The approved template with Yes / No quick replies; the tap comes back on the WABA event topic."""
    request_id = graph.request_id_of(request_row)
    message = whatsapp.build_template_message(
        request_id, request_row["verdict"]["summary"], destination, os.environ["WHATSAPP_TEMPLATE_NAME"]
    )
    meta_message_id = send_whatsapp_message(message)
    graph.record_whatsapp_message_id(request_id, meta_message_id)
    logger.info("whatsapp ask %s sent as %s", request_id, meta_message_id)
    return Channel.WHATSAPP


def acknowledge_on_whatsapp(request_id: str, status: RequestState, destination_number: str, receiver_name: str) -> bool:
    """The free-form reply inside the 24-h window the contact's tap opened: "Noted" with the reverse button after a No, plain
    text after a reverse. False when the new state has no acknowledgement (a Yes)."""
    if status is RequestState.DENIED:
        message = whatsapp.build_no_acknowledgement_message(request_id, destination_number)
    elif status is RequestState.REVERSED_NO:
        message = whatsapp.build_reverse_acknowledgement_message(receiver_name, destination_number)
    else:
        return False
    meta_message_id = send_whatsapp_message(message)
    logger.info("whatsapp acknowledgement of %s on %s sent as %s", status, request_id, meta_message_id)
    return True


def get_ses_client():
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("sesv2")
    return _ses_client


def ask_on_email(request_row: dict, contact: graph.Contact, receiver_name: str, destination: str) -> Channel:
    """Amazon SES to the contact's own address, from pidge@ so a reply lands under asks/. Only a No can come back."""
    request_id = graph.request_id_of(request_row)
    app_url = os.environ["ASK_LINK_BASE_URL"]
    no_link = ask_link.build_no_link(app_url, request_id, int(time.time()))
    response = get_ses_client().send_email(
        FromEmailAddress=os.environ["RECEIPT_SENDER"],
        Destination={"ToAddresses": [destination]},
        Content={"Simple": {
            "Subject": {"Data": email_ask.ask_subject(request_id)},
            "Body": {"Text": {"Data": email_ask.ask_body(request_row["verdict"]["summary"], receiver_name, no_link, app_url)}},
        }},
    )
    logger.info("email ask %s sent as %s", request_id, response["MessageId"])
    return Channel.EMAIL


def announce_recorded_answer(result: verdict.ActionResult, receiver_user_id: str, receiver_name: str, receipt_sender: str,
                             ses_client) -> None:
    """Both PWAs learn the new status; a final state emails the contact the receipt (a failed receipt is logged, never blocking)."""
    message = payloads.request_state_message(result.request_row, result.status, result.contact_row["contactName"], False)
    push_to_user(receiver_user_id, message)
    push_to_user(result.contact_row["contactPwaUserId"], message)
    try:  # the verdict already reached the receiver
        verdict.send_receipt(ses_client, receipt_sender, result.contact_row, result.status, receiver_name)
    except Exception:
        logger.exception("receipt for %s failed", graph.request_id_of(result.request_row))


def reject_action(connection_id: str, request_id: str | None, outcome: ActionOutcome) -> None:
    """The action_rejected reply to one client action; every ActionOutcome but RECORDED goes back this way."""
    push_to_connection(connection_id, {"type": PushMessageType.ACTION_REJECTED, "requestId": request_id, "reason": outcome})


def push_to_connection(connection_id: str, message: dict) -> None:
    """Reply to the one socket that sent a client action."""
    get_management_client().post_to_connection(ConnectionId=connection_id, Data=encode_push_message(message))


async def ask_on_every_channel(request_row: dict, contact: graph.Contact, receiver_name: str) -> list[Channel]:
    """Asks in parallel: the app, and every address Pidge may ask on each channel that is on; returns the channels that
    sent. One failure never cancels the others."""
    sends = [(ask_in_app, contact.contact_pwa_user_id)]
    for channel, is_enabled, send in ((Channel.RCS, RCS_ENABLED, ask_on_rcs), (Channel.WHATSAPP, WHATSAPP_ENABLED, ask_on_whatsapp),
                                      (Channel.EMAIL, EMAIL_ASK_ENABLED, ask_on_email)):
        if is_enabled:
            sends.extend((send, destination) for destination in contact.addresses_to_ask(channel))
    outcomes = await asyncio.gather(
        *(asyncio.to_thread(send, request_row, contact, receiver_name, destination) for send, destination in sends),
        return_exceptions=True,
    )
    channels_sent: list[Channel] = []
    for (send, _), outcome in zip(sends, outcomes):
        if isinstance(outcome, BaseException):
            logger.error("channel %s failed for %s", send.__name__, graph.request_id_of(request_row), exc_info=outcome)
        else:
            channels_sent.append(outcome)
    return list(dict.fromkeys(channels_sent))  # two numbers on one channel still count as one channel sent
