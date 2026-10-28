"""Scheduler target: an unanswered ask is re-sent once with a fresh hold window, plus one keypad call to the contact's first
checked RCS number (shared/voice_ask.py) on that re-ask only; a second silence marks it timeout. The call's hang-up timer
targets this function too ({"hangUpRequestId": ...}). The resident comes from the REQUEST# row's receiverEmail, resolved
before any write; no profile raises (the invocation retries). When the office the request was held for is no longer reached
(removed from the circle, or a former home's after re-enrolment: orgs.read_request_contact_row), it is neither re-asked,
called nor told of the timeout; the last timer is still booked and the resident still hears the timeout, named after their
current home."""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from shared import channels, graph, orgs, payloads, profiles, schedules, voice_ask
from shared.enums import RequestState
from shared.profiles import ReceiverProfile

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RE_ASK_DELAY_SECONDS = int(os.environ["RE_ASK_DELAY_SECONDS"])
LAST_ATTEMPT = 2


def handle(event, context):
    if "hangUpRequestId" in event:
        voice_ask.hang_up_call(event["hangUpRequestId"])
        return
    request_id, attempt = event["requestId"], int(event["attempt"])
    request_row = graph.get_request(request_id)
    if request_row is None or request_row["status"] != RequestState.PENDING:
        logger.info("retry %s for %s: nothing to do (%s)", attempt, request_id, request_row and request_row["status"])
        return
    resident = profiles.require_receiver_profile(request_row["receiverEmail"])
    if attempt >= LAST_ATTEMPT:
        mark_timeout(request_row, resident)
        return
    re_ask(request_row, resident, attempt, context.invoked_function_arn)  # this function books its own next run


def re_ask(request_row: dict, resident: ReceiverProfile, attempt: int, retry_function_arn: str) -> None:
    """Same request, new expiresAt, every channel again (none for an office no longer reached), the last timer booked, then
    one call to the contact (voice_ask, which logs and counts its own failures, so it never costs the timer)."""
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
    contact_row = orgs.read_request_contact_row(refreshed_row, resident)  # None (logged): an office no longer reached
    contact = graph.contact_from_row(contact_row) if contact_row is not None else None
    if contact is not None:
        channels_sent = asyncio.run(channels.ask_on_every_channel(refreshed_row, contact, resident.resident_name))
        logger.info("re-asked %s on %s", request_id, [str(channel) for channel in channels_sent])
    schedules.schedule_one_time_invocation(
        f"retry-{request_id}-{attempt + 1}", now + timedelta(seconds=RE_ASK_DELAY_SECONDS), retry_function_arn,
        {"requestId": request_id, "attempt": attempt + 1},
    )
    if contact is not None:  # after the timer: no voice failure can lose it
        voice_ask.call_contact_on_re_ask(refreshed_row, contact, resident.resident_name, attempt, retry_function_arn)


def mark_timeout(request_row: dict, resident: ReceiverProfile) -> None:
    """The first writer of `timeout`: both PWAs hear it (the resident alone for an office no longer reached); the hold stays
    (the shield), and the case agent writes its note."""
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
    contact_row = orgs.read_request_contact_row(timed_out_row, resident)
    message = payloads.request_state_message(timed_out_row, RequestState.TIMEOUT, orgs.contact_name_for_request(contact_row, resident), False)
    channels.push_to_user(resident.receiver_user_id, message)
    if contact_row is not None:
        channels.push_to_user(contact_row["contactPwaUserId"], message)
