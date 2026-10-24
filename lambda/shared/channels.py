"""Sends: WebSocket pushes to a user's open PWAs, and the 'Was this you?' ask on every channel."""

import asyncio
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

from shared import graph, payloads, rcs, whatsapp
from shared.enums import Channel, RequestState

logger = logging.getLogger(__name__)

_management_client = None
_rcs_client = None
_whatsapp_client = None

RCS_ENABLED = os.environ.get("RCS_ENABLED") == "true"
WHATSAPP_ENABLED = os.environ.get("WHATSAPP_ENABLED") == "true"


def get_management_client():
    """API Gateway management client for the WebSocket stage's callback URL."""
    global _management_client
    if _management_client is None:
        _management_client = boto3.client("apigatewaymanagementapi", endpoint_url=os.environ["WEBSOCKET_ENDPOINT"])
    return _management_client


def push_to_user(user_id: str, message: dict) -> int:
    """Pushes to every open connection of the user; returns how many received it. Stale connections are removed."""
    client = get_management_client()
    message_bytes = json.dumps(message).encode()
    delivered_count = 0
    for connection_id in graph.list_connection_ids(user_id):
        try:
            client.post_to_connection(ConnectionId=connection_id, Data=message_bytes)
            delivered_count += 1
        except client.exceptions.GoneException:
            graph.remove_connection(connection_id)
        except ClientError:
            logger.exception("push to connection %s of %s failed", connection_id, user_id)
    return delivered_count


def ask_in_app(request_row: dict, contact: graph.Contact, receiver_name: str) -> Channel:
    """The request row is already stored, so the contact's PWA shows it on sync; the live push is a bonus."""
    email_row = graph.get_email(request_row["emailId"])
    card = payloads.verification_card_message(request_row, email_row, receiver_name, RequestState(request_row["status"]))
    delivered_count = push_to_user(contact.contact_pwa_user_id, card)
    logger.info("in-app ask %s pushed to %d open connection(s)", graph.request_id_of(request_row), delivered_count)
    return Channel.INAPP


def get_rcs_client():
    global _rcs_client
    if _rcs_client is None:
        _rcs_client = boto3.client("pinpoint-sms-voice-v2")
    return _rcs_client


def ask_on_rcs(request_row: dict, contact: graph.Contact, receiver_name: str) -> Channel:
    """Rich card with Yes / No chips from the RCS agent; the tap comes back on the two-way SNS topic."""
    request_id = graph.request_id_of(request_row)
    email_row = graph.get_email(request_row["emailId"]) or {}
    card = rcs.build_rich_card(request_id, email_row, contact.contact_name, receiver_name, os.environ["PIDGE_IMAGE_URL"])
    response = get_rcs_client().send_rcs_message(
        DestinationPhoneNumber=contact.contact_rcs,
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


def ask_on_whatsapp(request_row: dict, contact: graph.Contact, receiver_name: str) -> Channel:
    """The approved template with Yes / No quick replies; the tap comes back on the WABA event topic."""
    request_id = graph.request_id_of(request_row)
    message = whatsapp.build_template_message(
        request_id, request_row["verdict"]["summary"], contact.contact_whatsapp, os.environ["WHATSAPP_TEMPLATE_NAME"]
    )
    response = get_whatsapp_client().send_whatsapp_message(
        originationPhoneNumberId=os.environ["WHATSAPP_PHONE_NUMBER_ID"],
        metaApiVersion=os.environ["WHATSAPP_META_API_VERSION"],
        message=json.dumps(message).encode(),
    )
    graph.record_whatsapp_message_id(request_id, response["messageId"])
    logger.info("whatsapp ask %s sent as %s", request_id, response["messageId"])
    return Channel.WHATSAPP


def push_to_connection(connection_id: str, message: dict) -> None:
    """Reply to the one socket that sent a client action."""
    get_management_client().post_to_connection(ConnectionId=connection_id, Data=json.dumps(message).encode())


async def ask_on_every_channel(request_row: dict, contact: graph.Contact, receiver_name: str) -> list[Channel]:
    """Asks in parallel; returns the channels that sent. One failure never cancels the others."""
    channel_senders = [ask_in_app]
    if RCS_ENABLED and contact.contact_rcs:
        channel_senders.append(ask_on_rcs)
    if WHATSAPP_ENABLED and contact.contact_whatsapp:
        channel_senders.append(ask_on_whatsapp)
    outcomes = await asyncio.gather(
        *(asyncio.to_thread(send, request_row, contact, receiver_name) for send in channel_senders),
        return_exceptions=True,
    )
    channels_sent: list[Channel] = []
    for send, outcome in zip(channel_senders, outcomes):
        if isinstance(outcome, BaseException):
            logger.error("channel %s failed for %s", send.__name__, graph.request_id_of(request_row), exc_info=outcome)
        else:
            channels_sent.append(outcome)
    return channels_sent
