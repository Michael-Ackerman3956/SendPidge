"""Pidge's own call: Amazon Connect's flow invokes pidge-verdict at two steps (Parameters.voiceStep). load_facts writes the
four session facts the AI agent may speak (voice_facts) before the bot is entered; record_answer records the key the callee
pressed, or the 2 the flow sets for the agent's No (the agent's Yes reaches it only as a keypad 1 the callee pressed).

Either step counts only on our instance, and only when the REQUEST# row names this ContactId as the call Pidge placed
(voiceContactId); requestId is a lookup key only. record_answer then has verdict.record_answer check the number as a checked
RCS point; load_facts records no answer.
"""

import hmac
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from pidge_verdict import voice_facts
from shared import graph, profiles, verdict
from shared.channel_enums import VoiceFlowStep
from shared.enums import ActionOutcome, Channel, VerdictAnswer
from shared.metrics import emit_count_metric
from shared.profiles import ReceiverProfile

logger = logging.getLogger(__name__)

CONNECT_FLOW_EVENT_NAME = "ContactFlowEvent"
VOICE_ANSWER_FAILED_METRIC_NAME = "VoiceAnswerFailed"
KEYPAD_ANSWERS = {"1": VerdictAnswer.YES, "2": VerdictAnswer.NO}  # any other key records nothing
# Only our own flow can invoke this (the invoke permission), so these outcomes mean a broken flow or event contract, never a
# callee's choice: each is counted as VoiceAnswerFailed (alarmed) as well as returned to the flow.
CONTRACT_REFUSAL_OUTCOMES = frozenset({
    ActionOutcome.NOT_ALLOWED, ActionOutcome.BAD_MESSAGE, ActionOutcome.UNKNOWN_REQUEST, ActionOutcome.WRONG_SENDER})


class VoiceAnswerError(Exception):
    """Raised in place of any failure, carrying only its class name: the runtime logs a raised error's message, and an AWS
    message can carry the number or an address."""


@dataclass(frozen=True)
class FlowContext:
    """The fields every step carries."""

    step: VoiceFlowStep
    contact_id: str
    request_id: str


@dataclass(frozen=True)
class FlowAnswer:
    contact_id: str
    request_id: str
    customer_number: str
    answer: VerdictAnswer


class FlowEventRefused(Exception):
    """A flow event that records nothing; its outcome goes back to the flow (record_answer speaks the matching sentence,
    load_facts goes to the keypad)."""

    def __init__(self, outcome: ActionOutcome):
        super().__init__(str(outcome))
        self.outcome = outcome


def is_connect_flow_event(event) -> bool:
    return isinstance(event, dict) and event.get("Name") == CONNECT_FLOW_EVENT_NAME


def require_text(value) -> str:
    """A non-empty ASCII string from the event (hmac.compare_digest refuses non-ASCII str); anything else raises TypeError."""
    if not isinstance(value, str) or not value or not value.isascii():
        raise TypeError("expected a non-empty ASCII string")
    return value


def parse_flow_context(event: dict, expected_instance_arn: str) -> FlowContext:
    """Every step's fields. Another instance, an unknown step, or a missing or malformed field raises
    FlowEventRefused(BAD_MESSAGE)."""
    try:
        contact_data = event["Details"]["ContactData"]
        if contact_data["InstanceARN"] != expected_instance_arn:
            raise ValueError("another Connect instance")
        return FlowContext(VoiceFlowStep(event["Details"]["Parameters"]["voiceStep"]),
                           require_text(contact_data["ContactId"]), require_text(contact_data["Attributes"]["requestId"]))
    except (KeyError, TypeError, ValueError) as error:
        raise FlowEventRefused(ActionOutcome.BAD_MESSAGE) from error


def parse_flow_answer(event: dict, flow_context: FlowContext) -> FlowAnswer:
    """The record_answer step's own fields. A key other than 1 or 2, or a missing or malformed field raises
    FlowEventRefused(BAD_MESSAGE)."""
    try:
        contact_data = event["Details"]["ContactData"]
        keypad_digit = require_text(contact_data["Attributes"]["keypadDigit"])
        if keypad_digit not in KEYPAD_ANSWERS:
            raise ValueError("a key that is not an answer")
        return FlowAnswer(flow_context.contact_id, flow_context.request_id,
                          require_text(contact_data["CustomerEndpoint"]["Address"]), KEYPAD_ANSWERS[keypad_digit])
    except (KeyError, TypeError, ValueError) as error:
        raise FlowEventRefused(ActionOutcome.BAD_MESSAGE) from error


def handle_flow_event(event: dict, announce_recorded_answer: Callable[[str, verdict.ActionResult, ReceiverProfile], None]) -> dict:
    """Connect's Invoke-Lambda block at either step; the flow branches on the returned outcome. A CONTRACT_REFUSAL_OUTCOMES
    outcome is also logged and counted as VoiceAnswerFailed. Any exception is logged by class name, counted, and raised as
    VoiceAnswerError: Connect retries, then takes its Error branch."""
    request_id, step = None, None
    try:
        if os.environ["VOICE_ENABLED"] != "true":
            outcome = ActionOutcome.NOT_ALLOWED
        else:
            flow_context = parse_flow_context(event, os.environ["VOICE_INSTANCE_ARN"])
            request_id, step = flow_context.request_id, flow_context.step
            outcome = run_flow_step(event, flow_context, announce_recorded_answer)
    except FlowEventRefused as refusal:
        outcome = refusal.outcome
    except Exception as error:
        logger.error("voice step %s on %s failed: %s", step, request_id, type(error).__name__)
        emit_count_metric(VOICE_ANSWER_FAILED_METRIC_NAME)
        raise VoiceAnswerError(type(error).__name__) from None
    if outcome in CONTRACT_REFUSAL_OUTCOMES:
        logger.warning("voice step %s on %s refused: %s", step, request_id, outcome)
        emit_count_metric(VOICE_ANSWER_FAILED_METRIC_NAME)
    return build_flow_response(outcome)


def run_flow_step(
    event: dict, flow_context: FlowContext, announce_recorded_answer: Callable[[str, verdict.ActionResult, ReceiverProfile], None]
) -> ActionOutcome:
    if flow_context.step is VoiceFlowStep.RECORD_ANSWER:
        return record_flow_answer(parse_flow_answer(event, flow_context), announce_recorded_answer)
    if flow_context.step is VoiceFlowStep.LOAD_FACTS:
        return load_flow_facts(flow_context)
    raise ValueError(f"no handler for the flow step {flow_context.step}")


def build_flow_response(outcome: ActionOutcome) -> dict:
    """Connect reads a flat dict of strings."""
    return {"voiceOutcome": str(outcome)}


def is_pidges_own_call(request_row: dict, contact_id: str) -> bool:
    """The REQUEST# row names this ContactId as the call Pidge placed (graph.record_voice_contact_id)."""
    return "voiceContactId" in request_row and hmac.compare_digest(request_row["voiceContactId"], contact_id)


def find_pidges_call_request(request_id: str, contact_id: str) -> dict:
    """The REQUEST# row of the call Pidge placed on this contact. No row raises FlowEventRefused(UNKNOWN_REQUEST); a row that
    names another ContactId, or none, raises FlowEventRefused(WRONG_SENDER)."""
    request_row = graph.get_request(request_id)
    if request_row is None:
        raise FlowEventRefused(ActionOutcome.UNKNOWN_REQUEST)
    if not is_pidges_own_call(request_row, contact_id):
        logger.warning("voice step on %s: the contact is not Pidge's call", request_id)
        raise FlowEventRefused(ActionOutcome.WRONG_SENDER)
    return request_row


def load_flow_facts(flow_context: FlowContext) -> ActionOutcome:
    """Pidge's own call on an existing request; then the AI agent's session facts. Records no answer."""
    request_row = find_pidges_call_request(flow_context.request_id, flow_context.contact_id)
    resident = profiles.require_receiver_profile(request_row["receiverEmail"])  # no profile raises before any Connect call
    return voice_facts.load_session_facts(
        request_row, resident.resident_name, flow_context.contact_id, datetime.now(timezone.utc))


def record_flow_answer(
    flow_answer: FlowAnswer, announce_recorded_answer: Callable[[str, verdict.ActionResult, ReceiverProfile], None]
) -> ActionOutcome:
    """Pidge's own call on an existing request; then record_answer checks the number. A Connect retry after the write landed
    (its connect:<ContactId> is already on the row) reads as recorded and announces nothing again."""
    request_id = flow_answer.request_id
    request_row = find_pidges_call_request(request_id, flow_answer.contact_id)
    resident = profiles.require_receiver_profile(request_row["receiverEmail"])  # no profile raises before any write
    inbound_message_id = f"connect:{flow_answer.contact_id}"
    sender = verdict.SenderIdentity(Channel.VOICE, flow_answer.customer_number)
    result = verdict.record_answer(request_id, flow_answer.answer, sender, inbound_message_id, graph.now_timestamp())
    if result.outcome is ActionOutcome.RECORDED:
        announce_recorded_answer(request_id, result, resident)
        return ActionOutcome.RECORDED
    if result.outcome is ActionOutcome.NOT_APPLIED and inbound_message_id in result.request_row["inboundMessageIds"]:
        logger.info("voice answer on %s already recorded: a Connect retry", request_id)
        return ActionOutcome.RECORDED
    logger.info("voice answer on %s not recorded: %s", request_id, result.outcome)
    return result.outcome
