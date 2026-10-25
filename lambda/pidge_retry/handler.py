"""Scheduler target: an unanswered ask is re-sent once with a fresh hold window; a second silence marks it timeout."""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from shared import channels, graph, payloads, schedules
from shared.enums import RequestState

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RECEIVER_USER_ID = os.environ["RECEIVER_USER_ID"]
RECEIVER_NAME = os.environ["RECEIVER_NAME"]
RE_ASK_DELAY_SECONDS = int(os.environ["RE_ASK_DELAY_SECONDS"])
LAST_ATTEMPT = 2


def handle(event, context):
    request_id, attempt = event["requestId"], int(event["attempt"])
    request_row = graph.get_request(request_id)
    if request_row is None or request_row["status"] != RequestState.PENDING:
        logger.info("retry %s for %s: nothing to do (%s)", attempt, request_id, request_row and request_row["status"])
        return
    if attempt >= LAST_ATTEMPT:
        mark_timeout(request_row)
        return
    re_ask(request_row, attempt, context.invoked_function_arn)  # this function books its own next run


def re_ask(request_row: dict, attempt: int, retry_function_arn: str) -> None:
    """Same request, new expiresAt, every channel again, and the last timer booked."""
    request_id = graph.request_id_of(request_row)
    now = datetime.now(timezone.utc)
    new_expiry = graph.iso_timestamp(now + timedelta(seconds=graph.HOLD_DURATION_SECONDS))
    applied = graph.write_answer(request_id, {
        "UpdateExpression": "SET expiresAt = :expiry, reAskedAt = :now",
        "ConditionExpression": "#status = :pending",
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":expiry": new_expiry, ":now": graph.iso_timestamp(now), ":pending": str(RequestState.PENDING)},
    })
    if not applied:
        return
    refreshed_row = graph.get_request(request_id)
    contact_row = graph.get_contact_row(refreshed_row["receiverEmail"], refreshed_row["contactSK"])
    contact = graph.contact_from_row(contact_row)
    channels_sent = asyncio.run(channels.ask_on_every_channel(refreshed_row, contact, RECEIVER_NAME))
    logger.info("re-asked %s on %s", request_id, [str(channel) for channel in channels_sent])
    schedules.schedule_one_time_invocation(
        f"retry-{request_id}-{attempt + 1}", now + timedelta(seconds=RE_ASK_DELAY_SECONDS), retry_function_arn,
        {"requestId": request_id, "attempt": attempt + 1},
    )


def mark_timeout(request_row: dict) -> None:
    """The first writer of `timeout`: both PWAs hear it; the hold stays (the shield), and the case agent writes its note."""
    request_id = graph.request_id_of(request_row)
    applied = graph.write_answer(request_id, {
        "UpdateExpression": "SET #status = :timeout, resolvedAt = :now",
        "ConditionExpression": "#status = :pending",
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":timeout": str(RequestState.TIMEOUT), ":pending": str(RequestState.PENDING),
                                      ":now": graph.now_timestamp()},
    })
    if not applied:
        return
    timed_out_row = graph.get_request(request_id)
    contact_row = graph.get_contact_row(timed_out_row["receiverEmail"], timed_out_row["contactSK"])
    message = payloads.request_state_message(timed_out_row, RequestState.TIMEOUT, contact_row["contactName"], False)
    channels.push_to_user(RECEIVER_USER_ID, message)
    channels.push_to_user(contact_row["contactPwaUserId"], message)
