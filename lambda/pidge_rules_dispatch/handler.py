"""Run 4 queue: a new CASE# row with a rule gap, or an admin event naming rule keys, becomes at most
MAXIMUM_SESSIONS_PER_INVOCATION rules keeper sessions inside the day's BUDGET# line.

Keys come only from the stream row's parsed enums or the admin event's parsed keys, never from model output. With
RULES_KEEPER_ENABLED other than "true" the queue is built and logged and no session starts. The "rows unchecked 90 d"
queue is not built (RULE# rows cannot be listed without a scan).
"""

import logging
import os
from enum import StrEnum

from boto3.dynamodb.types import TypeDeserializer

from shared import graph, refund_rules, rules_keeper
from shared.budget import MODEL_CALL_COST_USD, BudgetLine, KeeperSessionOutcome, start_ledger
from shared.enums import Jurisdiction, PaymentRail, ReaderModelChoice, RuleState

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAXIMUM_SESSIONS_PER_INVOCATION = 3
STREAM_INSERT_EVENT = "INSERT"

deserializer = TypeDeserializer()


class RulesDispatchTrigger(StrEnum):
    ADMIN = "admin"


def deserialize(image: dict) -> dict:
    return {key: deserializer.deserialize(value) for key, value in image.items()}


def rule_key_from_case_record(record: dict) -> str | None:
    """A CASE#/META insert with ruleGap true → its RULE# key (party "any"); any other record → None."""
    if record["eventName"] != STREAM_INSERT_EVENT:
        return None
    keys = deserialize(record["dynamodb"]["Keys"])
    if not keys["PK"].startswith("CASE#") or keys["SK"] != graph.METADATA_SORT_KEY:
        return None
    new_image = deserialize(record["dynamodb"]["NewImage"])
    if new_image["ruleGap"] is not True:
        return None
    return refund_rules.rule_partition_key(Jurisdiction(new_image["jurisdiction"]), PaymentRail(new_image["paymentRail"]))


def build_rules_queue(rule_keys: list[str]) -> list[str]:
    """Order kept; a duplicate, a key whose newest version waits for review and a key with a live version are dropped."""
    queue: list[str] = []
    for rule_key in rule_keys:
        if rule_key in queue:
            continue
        versions = refund_rules.list_rule_versions(rule_key)
        if any(rule.status is RuleState.LIVE for rule in versions):
            continue
        if versions and versions[-1].status is RuleState.PROPOSED:
            continue
        queue.append(rule_key)
    return queue


def rule_keys_from_event(event: dict) -> list[str]:
    """Stream records → their gap keys; an admin event → its keys, each rebuilt from its parsed parts; else ValueError."""
    if "Records" in event:
        gap_keys = (rule_key_from_case_record(record) for record in event["Records"])
        return [rule_key for rule_key in gap_keys if rule_key is not None]
    if "trigger" in event and RulesDispatchTrigger(event["trigger"]) is RulesDispatchTrigger.ADMIN:
        return [refund_rules.rule_partition_key(*refund_rules.parse_rule_partition_key(rule_key)) for rule_key in event["ruleKeys"]]
    raise ValueError("a rules dispatch event holds stream Records or an admin trigger")


def handle(event, context):
    queue = build_rules_queue(rule_keys_from_event(event))
    if os.environ.get("RULES_KEEPER_ENABLED") != "true":
        logger.info("rules keeper disabled; queued %s, started none", queue)
        return {"queued": queue, "started": []}
    ledger = start_ledger(BudgetLine.RULES)
    model_choice = ReaderModelChoice(os.environ["RULES_KEEPER_MODEL"])
    started: list[str] = []
    for rule_key in queue[:MAXIMUM_SESSIONS_PER_INVOCATION]:
        if not ledger.has_room_for(MODEL_CALL_COST_USD):
            logger.info("rules budget for %s has no room; %d key(s) left queued", ledger.day, len(queue) - len(started))
            break
        result = rules_keeper.run_rules_keeper_for_key(rule_key, ledger, model_choice)
        started.append(rule_key)
        logger.info("keeper session for %s: %s", rule_key, result.outcome)
        if result.outcome is KeeperSessionOutcome.BUDGET_EXHAUSTED:
            break
    return {"queued": queue, "started": started}
