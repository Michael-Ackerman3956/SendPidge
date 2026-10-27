"""DynamoDB Streams → the case agent. A final REQUEST# status (denied, confirmed, reversed_no, timeout), a new CASE# row
or a closed one becomes one case note pushed to Grandma;
a new question row under CHAT# becomes one Pidge reply (guardrail first), pushed to her or, for a question she
emailed to pidge@, mailed back to her real mailbox."""

import hashlib
import json
import logging
import os

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config

from shared import case_agent, channels, chat, chat_agent, chat_email, graph, payloads, recovery_drafts
from shared.enums import (
    FINAL_REQUEST_STATES, AgentInvocationKind, CaseAgentRuntime, CaseState, CaseTrigger, ChatVia, GuardrailOutcome,
    ReaderModelChoice,
    RequestState,
)
from shared.metrics import emit_count_metric

PACK_MAIL_FAILED_METRIC_NAME = "PackMailFailed"  # the recovery pack could not be mailed to her own mailbox

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RECEIVER_USER_ID = os.environ["RECEIVER_USER_ID"]
CASE_AGENT_RUNTIME = CaseAgentRuntime(os.environ["CASE_AGENT_RUNTIME"])
CASE_AGENT_MODEL = ReaderModelChoice(os.environ["CASE_AGENT_MODEL"])
CASE_AGENT_RUNTIME_ARN = os.environ.get("CASE_AGENT_RUNTIME_ARN", "")
AGENTCORE_QUALIFIER = "DEFAULT"
AGENTCORE_READ_TIMEOUT_SECONDS = 80  # under the Lambda's 90 s; no retries, a retry would run the agent twice
NOTE_WORTHY_STATES = (*FINAL_REQUEST_STATES, RequestState.TIMEOUT)
CHAT_GUARDRAIL_ENABLED = os.environ.get("CHAT_GUARDRAIL_ENABLED") == "true"
RECEIVER_MAILBOX = os.environ.get("RECEIVER_MAILBOX", "")  # her real mailbox; emailed questions are answered there only
RECEIPT_SENDER = os.environ.get("RECEIPT_SENDER", "")

deserializer = TypeDeserializer()
_agentcore_client = None
_ses_client = None


def get_agentcore_client():
    global _agentcore_client
    if _agentcore_client is None:
        _agentcore_client = boto3.client(
            "bedrock-agentcore", config=Config(read_timeout=AGENTCORE_READ_TIMEOUT_SECONDS, retries={"max_attempts": 0})
        )
    return _agentcore_client


def handle(event, context):
    for record in event["Records"]:
        question_key = chat.question_key_from_stream_keys(deserialize(record["dynamodb"].get("Keys")))
        if question_key is not None:
            answer_and_push_question(*question_key)
            continue
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
    """A REQUEST# META reaching a final state, a CASE# META insert or a CASE# META turning closed wakes the agent."""
    keys = deserialize(record["dynamodb"].get("Keys"))
    new_image = deserialize(record["dynamodb"].get("NewImage"))
    old_image = deserialize(record["dynamodb"].get("OldImage"))
    if keys.get("SK") != graph.METADATA_SORT_KEY:
        return None
    if keys["PK"].startswith("REQUEST#"):
        new_status, old_status = new_image.get("status"), old_image.get("status")
        if new_status not in NOTE_WORTHY_STATES or new_status == old_status:
            return None
        return case_agent.CaseScope(RECEIVER_USER_ID, graph.request_id_of(keys), CaseTrigger.VERDICT)
    if keys["PK"].startswith("CASE#") and record["eventName"] == "INSERT":
        return case_agent.CaseScope(RECEIVER_USER_ID, new_image["requestId"], CaseTrigger.CASE, graph.case_id_of(keys))
    if keys["PK"].startswith("CASE#") and new_image.get("status") == CaseState.CLOSED and old_image.get("status") != CaseState.CLOSED:
        return case_agent.CaseScope(RECEIVER_USER_ID, new_image["requestId"], CaseTrigger.CASE_CLOSED, graph.case_id_of(keys))
    return None


def write_and_push_note(scope: case_agent.CaseScope) -> None:
    """Runs the agent, stores the note, tells Grandma's PWA. A failed run writes nothing and is logged for the retry."""
    if CASE_AGENT_RUNTIME is CaseAgentRuntime.LAMBDA:
        agent = case_agent.build_case_agent(CASE_AGENT_MODEL, scope)
        note = case_agent.run_case_agent(agent, scope)
    else:
        note = invoke_case_agent_on_agentcore(scope)
    note_row = case_agent.store_case_note(scope, note)
    if note_row.get("recoveryPack"):
        note_row = mail_recovery_pack(note_row)
    delivered_count = channels.push_to_user(scope.receiver_user_id, payloads.case_note_message(note_row))
    logger.info("case note for %s (%s) stored; pushed to %d connection(s)", scope.request_id, scope.trigger, delivered_count)


def mail_recovery_pack(note_row: dict) -> dict:
    """The pack goes to her own mailbox (the only SES recipient this role may use); a refused send is logged, the note
    still reaches the app, and the row stays unstamped so the screen does not claim a copy was sent."""
    if not RECEIVER_MAILBOX:
        logger.warning("RECEIVER_MAILBOX is not set; recovery pack for %s not mailed", note_row["PK"])
        return note_row
    try:
        recovery_drafts.send_pack_email(get_ses_client(), RECEIPT_SENDER, RECEIVER_MAILBOX, note_row["recoveryPack"])
    except Exception:
        logger.exception("recovery pack email for %s failed", note_row["PK"])
        emit_count_metric(PACK_MAIL_FAILED_METRIC_NAME)
        return note_row
    mailed_at = graph.now_timestamp()
    try:
        graph.record_pack_mailed(note_row, mailed_at)
    except Exception:  # the email went out; only the stamp is missing, so the screen does not claim a copy was sent
        logger.exception("recovery pack for %s was mailed but the packMailedAt stamp failed", note_row["PK"])
        return note_row
    return {**note_row, "packMailedAt": mailed_at}


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


def check_question_with_guardrail(question: str) -> GuardrailOutcome:
    if not CHAT_GUARDRAIL_ENABLED:
        logger.warning("CHAT_GUARDRAIL_ENABLED is off; the question goes to the agent unchecked")
        return GuardrailOutcome.PASSED
    return chat.check_prompt_attack(question)


def ask_chat_agent(user_id: str, question: str) -> str:
    if CASE_AGENT_RUNTIME is CaseAgentRuntime.LAMBDA:
        return chat_agent.run_chat_agent(chat_agent.build_chat_agent(CASE_AGENT_MODEL, user_id), question).reply_text
    return invoke_chat_agent_on_agentcore(user_id, question)


def get_ses_client():
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("sesv2")
    return _ses_client


def answer_and_push_question(user_id: str, sort_key: str) -> None:
    """One question → one stored reply → mailed back (emailed question) or pushed to the asker's open sockets.

    A redelivered question is skipped.
    """
    reply_row = chat.answer_question(user_id, sort_key, check_question_with_guardrail, ask_chat_agent)
    if reply_row is None:
        logger.info("question %s for %s already answered", sort_key, user_id)
        return
    if ChatVia(reply_row["via"]) is ChatVia.EMAIL:
        question_row = chat.get_message(user_id, sort_key)
        chat_email.send_reply_email(get_ses_client(), RECEIPT_SENDER, RECEIVER_MAILBOX, question_row, reply_row)
        logger.info("reply to emailed %s for %s stored and mailed", sort_key, user_id)
        return
    delivered_count = channels.push_to_user(user_id, chat.pidge_reply_message_for_row(reply_row))
    logger.info("reply to %s for %s stored; pushed to %d connection(s)", sort_key, user_id, delivered_count)


def chat_session_id(user_id: str) -> str:
    """Deterministic and at least 33 characters: one chat session per user, so follow-up questions keep context."""
    return f"chat-{hashlib.sha256(user_id.encode()).hexdigest()}"


def invoke_chat_agent_on_agentcore(user_id: str, question: str) -> str:
    payload = {"kind": str(AgentInvocationKind.CHAT), "userId": user_id, "question": question}
    response = get_agentcore_client().invoke_agent_runtime(
        agentRuntimeArn=CASE_AGENT_RUNTIME_ARN,
        runtimeSessionId=chat_session_id(user_id),
        qualifier=AGENTCORE_QUALIFIER,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps(payload).encode(),
    )
    body = response["response"].read()
    if response.get("statusCode", 200) >= 300:
        raise RuntimeError(f"case agent runtime answered {response.get('statusCode')}: {body[:200]!r}")
    answer = json.loads(body)
    if "reply" not in answer:
        raise RuntimeError(f"case agent runtime returned no reply: {answer}")
    return chat_agent.ChatReply.model_validate(answer["reply"]).reply_text
