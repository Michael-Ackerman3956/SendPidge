"""Sends: WebSocket pushes to a user's open PWAs, and the 'Was this you?' ask on every channel."""

import asyncio
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

from shared import graph
from shared.enums import Channel, PushMessageType

logger = logging.getLogger(__name__)

_management_client = None


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


def ask_in_app(request: graph.VerificationRequest) -> Channel:
    """The request row is already stored, so the contact's PWA shows it on connect; the live push is a bonus."""
    card = {
        "type": PushMessageType.VERIFICATION_CARD,
        "request": {"requestId": request.request_id, "summary": request.summary, "expiresAt": request.expires_at},
    }
    delivered_count = push_to_user(request.contact.contact_pwa_user_id, card)
    logger.info("in-app ask %s pushed to %d open connection(s)", request.request_id, delivered_count)
    return Channel.INAPP


async def ask_on_every_channel(request: graph.VerificationRequest) -> list[Channel]:
    """Asks in parallel; returns the channels that sent. One failure never cancels the others."""
    channel_senders = [ask_in_app]  # RCS joins in slice 5, WhatsApp in slice 6
    outcomes = await asyncio.gather(
        *(asyncio.to_thread(send, request) for send in channel_senders), return_exceptions=True
    )
    channels_sent: list[Channel] = []
    for send, outcome in zip(channel_senders, outcomes):
        if isinstance(outcome, BaseException):
            logger.error("channel %s failed for %s", send.__name__, request.request_id, exc_info=outcome)
        else:
            channels_sent.append(outcome)
    return channels_sent
