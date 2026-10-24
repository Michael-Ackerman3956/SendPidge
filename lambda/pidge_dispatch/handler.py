"""DynamoDB Streams → the case agent. A final REQUEST# status or a new CASE# row becomes one case note pushed to Grandma."""

import logging
import os

from boto3.dynamodb.types import TypeDeserializer

from shared import case_agent, channels, graph, payloads
from shared.enums import FINAL_REQUEST_STATES, CaseAgentRuntime, CaseTrigger, ReaderModelChoice, RequestState

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RECEIVER_USER_ID = os.environ["RECEIVER_USER_ID"]
CASE_AGENT_RUNTIME = CaseAgentRuntime(os.environ["CASE_AGENT_RUNTIME"])
CASE_AGENT_MODEL = ReaderModelChoice(os.environ["CASE_AGENT_MODEL"])
NOTE_WORTHY_STATES = (*FINAL_REQUEST_STATES, RequestState.TIMEOUT)

deserializer = TypeDeserializer()


def handle(event, context):
    for record in event["Records"]:
        scope = scope_for_record(record)
        if scope is None:
            continue
        if case_agent.has_note_for(scope):
            logger.info("note for %s %s already written", scope.request_id, scope.trigger)
            continue
        write_and_push_note(scope)


def deserialize(image: dict | None) -> dict:
    return {key: deserializer.deserialize(value) for key, value in (image or {}).items()}


def scope_for_record(record: dict) -> case_agent.CaseScope | None:
    """Only a REQUEST# META reaching a final state, or a CASE# META insert, wakes the agent."""
    keys = deserialize(record["dynamodb"].get("Keys"))
    new_image = deserialize(record["dynamodb"].get("NewImage"))
    old_image = deserialize(record["dynamodb"].get("OldImage"))
    if keys.get("SK") != graph.METADATA_SORT_KEY:
        return None
    if keys["PK"].startswith("REQUEST#"):
        new_status, old_status = new_image.get("status"), old_image.get("status")
        if new_status not in NOTE_WORTHY_STATES or new_status == old_status:
            return None
        return case_agent.CaseScope(RECEIVER_USER_ID, keys["PK"].removeprefix("REQUEST#"), CaseTrigger.VERDICT)
    if keys["PK"].startswith("CASE#") and record["eventName"] == "INSERT":
        return case_agent.CaseScope(RECEIVER_USER_ID, new_image["requestId"], CaseTrigger.CASE, keys["PK"].removeprefix("CASE#"))
    return None


def write_and_push_note(scope: case_agent.CaseScope) -> None:
    """Runs the agent, stores the note, tells Grandma's PWA. A failed run writes nothing and is logged for the retry."""
    if CASE_AGENT_RUNTIME is CaseAgentRuntime.LAMBDA:
        agent = case_agent.build_case_agent(CASE_AGENT_MODEL, scope)
        note = case_agent.run_case_agent(agent, scope)
    else:
        raise NotImplementedError("CASE_AGENT_RUNTIME=agentcore is wired in the next build step")
    note_row = case_agent.store_case_note(scope, note)
    delivered_count = channels.push_to_user(scope.receiver_user_id, payloads.case_note_message(note_row))
    logger.info("case note for %s (%s) stored; pushed to %d connection(s)", scope.request_id, scope.trigger, delivered_count)
