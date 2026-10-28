"""WebSocket route send_message (pidge-send, its own function and role, writes limited to MESSAGE#*/SENDRATE#*): a resident
sends one message to someone in her circle. The resident comes from the connection's user -> profiles.require_resident_caller,
never the body; the body gives only channel, to and text (shared/messenger_send.py). The sent or failed row is pushed to her
own sockets as message_state, keeping gone connections (this role cannot delete CONN# rows); a refusal and a failed send are
answered action_rejected. Logs ids only."""

import json
import logging
from datetime import datetime, timezone

from shared import channels, graph, messenger_send, payloads, profiles
from shared.enums import ActionOutcome, ClientAction
from shared.profiles import ReceiverProfile

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HANDLED = {"statusCode": 200}
FORBIDDEN = {"statusCode": 403}


def handle(event, context):
    """Authorization first: an unregistered connection is FORBIDDEN and a caller who is not a resident is NOT_ALLOWED before
    the body is read; an unparseable body or an action without a send handler is BAD_MESSAGE."""
    connection_id = event["requestContext"]["connectionId"]
    user_id = graph.get_connection_user_id(connection_id)
    if user_id is None:
        logger.warning("send action from unregistered connection %s", connection_id)
        return FORBIDDEN
    resident = profiles.require_resident_caller(user_id)
    if resident is None:
        channels.reject_action(connection_id, None, ActionOutcome.NOT_ALLOWED)
        return HANDLED
    try:
        body = json.loads(event["body"])
        action = ClientAction(body["action"])
    except (ValueError, TypeError, KeyError):
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return HANDLED
    if action not in SEND_ACTION_HANDLERS:
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return HANDLED
    SEND_ACTION_HANDLERS[action](connection_id, resident, body)
    return HANDLED


def handle_send_message(connection_id: str, resident: ReceiverProfile, body: dict) -> None:
    """BAD_MESSAGE for a malformed body; otherwise messenger_send.send_message. A stored row (sent or failed) is pushed to
    her; any outcome but RECORDED is answered on this socket. An exception inside the send (a table write, say) is answered
    SEND_FAILED so the composer stops waiting, then raised for the Lambda Errors metric."""
    try:
        send_request = messenger_send.parse_send_request(body)
    except ValueError:
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    try:
        result = messenger_send.send_message(resident, send_request, datetime.now(timezone.utc))
    except Exception:
        logger.error("send for %s raised; answered send_failed", resident.receiver_user_id)
        channels.reject_action(connection_id, None, ActionOutcome.SEND_FAILED)
        raise
    if result.message_row is not None:
        channels.push_to_user(resident.receiver_user_id, payloads.message_state_message(result.message_row, None, None, None),
                              should_remove_gone_connections=False)
    if result.outcome is not ActionOutcome.RECORDED:
        channels.reject_action(connection_id, None, result.outcome)


SEND_ACTION_HANDLERS = {  # each acts for the caller's own resident profile, resolved first by handle
    ClientAction.SEND_MESSAGE: handle_send_message,
}
