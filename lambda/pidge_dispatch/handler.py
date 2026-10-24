"""DynamoDB Streams → the case agent. A final REQUEST# status or a new CASE# row becomes one case note pushed to Grandma."""

import hashlib
import json
import logging
import os

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config

from shared import case_agent, channels, graph, payloads
from shared.enums import FINAL_REQUEST_STATES, CaseAgentRuntime, CaseTrigger, ReaderModelChoice, RequestState

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RECEIVER_USER_ID = os.environ["RECEIVER_USER_ID"]
CASE_AGENT_RUNTIME = CaseAgentRuntime(os.environ["CASE_AGENT_RUNTIME"])
CASE_AGENT_MODEL = ReaderModelChoice(os.environ["CASE_AGENT_MODEL"])
CASE_AGENT_RUNTIME_ARN = os.environ.get("CASE_AGENT_RUNTIME_ARN", "")
AGENTCORE_QUALIFIER = "DEFAULT"
AGENTCORE_READ_TIMEOUT_SECONDS = 80  # under the Lambda's 90 s; no retries, a retry would run the agent twice
NOTE_WORTHY_STATES = (*FINAL_REQUEST_STATES, RequestState.TIMEOUT)

deserializer = TypeDeserializer()
_agentcore_client = None


def get_agentcore_client():
    global _agentcore_client
    if _agentcore_client is None:
        _agentcore_client = boto3.client(
            "bedrock-agentcore", config=Config(read_timeout=AGENTCORE_READ_TIMEOUT_SECONDS, retries={"max_attempts": 0})
        )
    return _agentcore_client


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
        note = invoke_case_agent_on_agentcore(scope)
    note_row = case_agent.store_case_note(scope, note)
    delivered_count = channels.push_to_user(scope.receiver_user_id, payloads.case_note_message(note_row))
    logger.info("case note for %s (%s) stored; pushed to %d connection(s)", scope.request_id, scope.trigger, delivered_count)


def agentcore_session_id(scope: case_agent.CaseScope) -> str:
    """Deterministic and at least 33 characters: the same request always lands in the same runtime session."""
    return f"case-{hashlib.sha256(scope.request_id.encode()).hexdigest()}"


def invoke_case_agent_on_agentcore(scope: case_agent.CaseScope) -> case_agent.CaseNote:
    """One InvokeAgentRuntime call; anything but a well-formed note raises (logged by the stream retry, no row written)."""
    payload = {"receiverUserId": scope.receiver_user_id, "requestId": scope.request_id, "trigger": str(scope.trigger), "caseId": scope.case_id}
    response = get_agentcore_client().invoke_agent_runtime(
        agentRuntimeArn=CASE_AGENT_RUNTIME_ARN,
        runtimeSessionId=agentcore_session_id(scope),
        qualifier=AGENTCORE_QUALIFIER,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps(payload).encode(),
    )
    body = response["response"].read()
    if response.get("statusCode", 200) >= 300:
        raise RuntimeError(f"case agent runtime answered {response.get('statusCode')}: {body[:200]!r}")
    answer = json.loads(body)
    if "note" not in answer:
        raise RuntimeError(f"case agent runtime returned no note: {answer}")
    return case_agent.CaseNote.model_validate(answer["note"])
