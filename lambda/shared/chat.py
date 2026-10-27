"""Grandma's questions to Pidge: CHAT#<userId> rows, the hourly cap, the prompt-attack check and the reply.

pidge-verdict accepts a question (cap, then the grandma row); pidge-dispatch answers it from the stream (guardrail,
then the chat agent, then the pidge row). The user id always comes from the caller's connection row, never from text.
"""

import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import boto3
from boto3.dynamodb.conditions import Key

from shared import graph, hourly_counters
from shared.metrics import emit_count_metric
from shared.enums import ChatCardKind, ChatRole, ChatVia, GuardrailOutcome, PushMessageType

logger = logging.getLogger(__name__)

CHAT_PARTITION_PREFIX = "CHAT#"
MESSAGE_SORT_KEY_PREFIX = "MSG#"
QUESTIONS_PER_HOUR_LIMIT = 20
QUESTION_MAX_CHARACTERS = 1000
CHAT_RETENTION_SECONDS = 30 * 24 * 3600
PROMPT_ATTACK_METRIC_NAME = "PromptAttack"
GUARDRAIL_INTERVENED_ACTION = "GUARDRAIL_INTERVENED"

CHAT_OFF_REFUSAL = "Chat with Pidge is turned off right now."
EMPTY_QUESTION_REFUSAL = "Please type a shorter question."
RATE_LIMIT_REFUSAL = "You have asked a lot this hour. Please try again a little later."
PROMPT_ATTACK_REFUSAL = "I can only help with your own emails and cases."
UNAVAILABLE_REFUSAL = "I can't answer right now. Please try again in a few minutes."

_bedrock_runtime_client = None


@dataclass(frozen=True)
class AcceptedQuestion:
    """What pidge-verdict did with one question: the stored grandma row, or a refusal sentence and no row."""

    message_row: dict | None
    refusal_text: str | None


def is_chat_enabled() -> bool:
    return os.environ.get("CHAT_ENABLED") == "true"


def chat_partition(user_id: str) -> str:
    return f"{CHAT_PARTITION_PREFIX}{user_id}"


def take_question_slot(user_id: str) -> bool:
    """One conditional increment of this hour's counter. False when the cap is reached or the write fails (fail closed)."""
    return hourly_counters.take_hourly_slot(chat_partition(user_id), QUESTIONS_PER_HOUR_LIMIT)


def write_message(user_id: str, role: ChatRole, via: ChatVia, text: str, extra_fields: dict | None = None) -> dict:
    """MSG#<time>#<suffix>: sorts by time, and two questions in the same second never overwrite each other."""
    sent_at = graph.now_timestamp()
    message_row = {
        "PK": chat_partition(user_id), "SK": f"{MESSAGE_SORT_KEY_PREFIX}{sent_at}#{uuid.uuid4().hex[:8]}",
        "role": str(role), "via": str(via), "text": text, "sentAt": sent_at, **(extra_fields or {}),
        "ttlEpochSeconds": graph.retention_epoch(CHAT_RETENTION_SECONDS),
    }
    graph.get_table().put_item(Item=message_row, ConditionExpression="attribute_not_exists(SK)")
    return message_row


def accept_question(user_id: str, question: object, via: ChatVia, extra_fields: dict | None = None) -> AcceptedQuestion:
    """The flag, the length, the hourly cap, then the grandma row (plus extra_fields). Any refusal writes no row."""
    if not is_chat_enabled():
        return AcceptedQuestion(None, CHAT_OFF_REFUSAL)
    if not isinstance(question, str) or not question.strip() or len(question) > QUESTION_MAX_CHARACTERS:
        return AcceptedQuestion(None, EMPTY_QUESTION_REFUSAL)
    if not take_question_slot(user_id):
        return AcceptedQuestion(None, RATE_LIMIT_REFUSAL)
    return AcceptedQuestion(write_message(user_id, ChatRole.GRANDMA, via, question.strip(), extra_fields), None)


def question_key_from_stream_keys(keys: dict) -> tuple[str, str] | None:
    """(userId, message sort key) for a CHAT# MSG# row; None for any other row."""
    partition, sort_key = keys.get("PK", ""), keys.get("SK", "")
    if not partition.startswith(CHAT_PARTITION_PREFIX) or not sort_key.startswith(MESSAGE_SORT_KEY_PREFIX):
        return None
    return partition.removeprefix(CHAT_PARTITION_PREFIX), sort_key


def get_message(user_id: str, sort_key: str) -> dict | None:
    return graph.get_table().get_item(Key={"PK": chat_partition(user_id), "SK": sort_key}).get("Item")


def list_recent_messages(user_id: str, limit: int) -> list[dict]:
    """The caller's last `limit` MSG# rows, oldest first (the HOUR# counter rows in the same partition are skipped)."""
    response = graph.get_table().query(
        KeyConditionExpression=Key("PK").eq(chat_partition(user_id)) & Key("SK").begins_with(MESSAGE_SORT_KEY_PREFIX),
        ScanIndexForward=False,
        Limit=limit,
    )
    return list(reversed(response["Items"]))


def chat_message_for_sync(message_row: dict) -> dict:
    """One thread bubble in SYNC_STATE.chat: a question row has no card; a reply row's card is null or {kind}."""
    card = message_row.get("card")
    return {
        "messageId": message_row["SK"],
        "role": str(ChatRole(message_row["role"])),
        "text": message_row["text"],
        "sentAt": message_row["sentAt"],
        "card": {"kind": ChatCardKind(card["kind"])} if card else None,
    }


def mark_question(user_id: str, sort_key: str, field_name: str, value: object) -> None:
    graph.get_table().update_item(
        Key={"PK": chat_partition(user_id), "SK": sort_key},
        UpdateExpression="SET #field = :value", ExpressionAttributeNames={"#field": field_name},
        ExpressionAttributeValues={":value": value},
    )


def get_bedrock_runtime_client():
    global _bedrock_runtime_client
    if _bedrock_runtime_client is None:
        _bedrock_runtime_client = boto3.client("bedrock-runtime")
    return _bedrock_runtime_client


def check_prompt_attack(question: str) -> GuardrailOutcome:
    """ApplyGuardrail on the question alone (source INPUT). Raises when the guardrail cannot be reached."""
    response = get_bedrock_runtime_client().apply_guardrail(
        guardrailIdentifier=os.environ["CHAT_GUARDRAIL_ID"],
        guardrailVersion=os.environ["CHAT_GUARDRAIL_VERSION"],
        source="INPUT",
        content=[{"text": {"text": question}}],
    )
    return GuardrailOutcome.INTERVENED if response["action"] == GUARDRAIL_INTERVENED_ACTION else GuardrailOutcome.PASSED


def emit_prompt_attack_metric() -> None:
    emit_count_metric(PROMPT_ATTACK_METRIC_NAME)


def write_reply(user_id: str, question_row: dict, text: str, card_kind: ChatCardKind | None) -> dict:
    card = {"kind": str(card_kind)} if card_kind else None
    reply_row = write_message(user_id, ChatRole.PIDGE, ChatVia(question_row["via"]), text,
                              {"inReplyTo": question_row["SK"], "card": card})
    mark_question(user_id, question_row["SK"], "answeredAt", reply_row["sentAt"])
    return reply_row


def answer_question(
    user_id: str,
    sort_key: str,
    check_question: Callable[[str], GuardrailOutcome],
    ask_agent: Callable[[str, str], str],
) -> dict | None:
    """Guardrail first, then the agent; returns the stored pidge row, or None when this question was already handled.

    A blocked question never reaches the agent; a guardrail or agent failure becomes a refusal reply, never a skip.
    """
    question_row = get_message(user_id, sort_key)
    if question_row is None or question_row.get("answeredAt") or question_row.get("guardrailHit"):
        return None
    try:
        guardrail_outcome = check_question(question_row["text"])
    except Exception:
        logger.exception("guardrail unavailable for %s %s; refusing", user_id, sort_key)
        return write_reply(user_id, question_row, UNAVAILABLE_REFUSAL, ChatCardKind.REFUSAL)
    if guardrail_outcome is GuardrailOutcome.INTERVENED:
        mark_question(user_id, sort_key, "guardrailHit", True)
        emit_prompt_attack_metric()
        logger.warning("prompt attack blocked for %s %s", user_id, sort_key)
        return write_reply(user_id, question_row, PROMPT_ATTACK_REFUSAL, ChatCardKind.REFUSAL)
    try:
        reply_text = ask_agent(user_id, question_row["text"])
    except Exception:
        logger.exception("chat agent failed for %s %s; refusing", user_id, sort_key)
        return write_reply(user_id, question_row, UNAVAILABLE_REFUSAL, ChatCardKind.REFUSAL)
    return write_reply(user_id, question_row, reply_text, None)


def pidge_reply_message(message_id: str | None, text: str, card_kind: ChatCardKind | None) -> dict:
    """PIDGE_REPLY: messageId is the reply row's sort key, or None for a refusal that wrote no row."""
    return {"type": PushMessageType.PIDGE_REPLY, "messageId": message_id, "text": text,
            "card": {"kind": card_kind} if card_kind else None}


def pidge_reply_message_for_row(reply_row: dict) -> dict:
    card = reply_row.get("card")
    return pidge_reply_message(reply_row["SK"], reply_row["text"], ChatCardKind(card["kind"]) if card else None)
