"""RULE# rows: the recovery pack's facts. Only live versions are read; the envelope is strict, `extra` is free.

The rules keeper writes "proposed" versions only (write_proposed_rule); an admin makes one live or rejects it."""

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import graph
from shared.enums import Jurisdiction, PaymentRail, ProposedBy, RuleState

ANY_PARTY = "any"
RULE_VERSION_PREFIX = "v"
CARD_ISSUER_CONTACT = "the number on the back of your card"
SEED_RULE_FILE_PATTERN = re.compile(r"^refund_rules(?:\.([A-Z]{2}))?\.json$")
RULE_KEY_PATTERN = re.compile(r"^RULE#([A-Z]{2})#([a-z_]+)#([a-z0-9][a-z0-9-]{0,39})$")
REJECT_REASON_MAX_LENGTH = 300
CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailedException"


class RuleNotProposed(ValueError):
    """The version is missing or no longer proposed, so it cannot be approved or rejected."""


class ProposalMatchesRejected(ValueError):
    """The proposed steps equal a version an admin already rejected."""

    def __init__(self, version: int, reason: str | None) -> None:
        super().__init__(f"the proposal repeats rejected version {version}")
        self.version = version
        self.reason = reason


@dataclass(frozen=True)
class RefundRuleStep:
    order: int
    action: str
    contact: str
    source_url: str
    deadline_hours: int | None = None
    deadline_days: int | None = None
    organisation: str | None = None  # the body the step speaks to, in words; never shown through as_pack_step

    def as_pack_step(self) -> dict:
        return {"order": self.order, "action": self.action, "contact": self.contact, "sourceUrl": self.source_url,
                "deadlineHours": self.deadline_hours, "deadlineDays": self.deadline_days}


@dataclass(frozen=True)
class RefundRule:
    rule_key: str  # the RULE# partition key
    version: int
    jurisdiction: Jurisdiction
    payment_rail: PaymentRail
    party: str
    status: RuleState
    proposed_by: ProposedBy
    steps: tuple[RefundRuleStep, ...]
    extra: dict
    checked_at: str
    reject_reason: str | None


def rule_partition_key(jurisdiction: Jurisdiction, payment_rail: PaymentRail, party: str = ANY_PARTY) -> str:
    return f"RULE#{jurisdiction}#{payment_rail}#{party}"


def optional_int(value) -> int | None:
    return None if value is None else int(value)


def parse_rule_row(item: dict) -> RefundRule:
    """Raises (KeyError / ValueError) on a broken envelope: a half-written rule must never become a pack."""
    steps = tuple(
        RefundRuleStep(int(step["order"]), str(step["action"]), str(step["contact"]), str(step["sourceUrl"]),
                       optional_int(step.get("deadlineHours")), optional_int(step.get("deadlineDays")), step.get("organisation"))
        for step in item["steps"]
    )
    if not steps or any(not step.source_url.startswith("https://") for step in steps):
        raise ValueError(f"rule {item['PK']} {item['SK']} has no steps or a step without an https source")
    return RefundRule(
        item["PK"], int(item["SK"].removeprefix(RULE_VERSION_PREFIX)), Jurisdiction(item["jurisdiction"]),
        PaymentRail(item["paymentRail"]), str(item["party"]), RuleState(item["status"]), ProposedBy(item["proposedBy"]),
        steps, dict(item.get("extra", {})),
        str(item["checkedAt"]), item["rejectReason"],
    )


def find_live_rule(jurisdiction: Jurisdiction, payment_rail: PaymentRail, party: str = ANY_PARTY) -> RefundRule | None:
    """The newest live version for the key, or None (a rule gap). Proposed and rejected versions are never returned."""
    response = graph.get_table().query(KeyConditionExpression=Key("PK").eq(rule_partition_key(jurisdiction, payment_rail, party)))
    live_rules = [parse_rule_row(item) for item in response["Items"] if item.get("status") == RuleState.LIVE]
    return max(live_rules, key=lambda rule: rule.version, default=None)


def rule_row_from_seed(seed_rule: dict) -> dict:
    """One infra/seed/refund_rules.json entry → the DynamoDB item (Decimals for numbers)."""
    jurisdiction, rail = Jurisdiction(seed_rule["jurisdiction"]), PaymentRail(seed_rule["paymentRail"])
    return {
        "PK": rule_partition_key(jurisdiction, rail, seed_rule["party"]),
        "SK": f"{RULE_VERSION_PREFIX}{int(seed_rule['version'])}",
        "jurisdiction": str(jurisdiction), "paymentRail": str(rail), "party": seed_rule["party"],
        "status": str(RuleState(seed_rule["status"])), "proposedBy": str(ProposedBy(seed_rule["proposedBy"])),
        "checkedAt": seed_rule["checkedAt"], "rejectReason": seed_rule.get("rejectReason"),
        "steps": [{key: (Decimal(value) if isinstance(value, int) and not isinstance(value, bool) else value)
                   for key, value in step.items()} for step in seed_rule["steps"]],
        "extra": seed_rule.get("extra", {}),
    }


def build_recovery_pack(case_row: dict, rule: RefundRule | None) -> dict:
    """Facts for Grandma's recovery pack: the case's rail and the rule's steps. No model output goes in here, and the drafts are
    added by case_agent.store_case_note after the agent ran, so the model never reads the payee hint through them."""
    return {
        "caseId": graph.case_id_of(case_row),
        "paymentRail": case_row["paymentRail"],
        "jurisdiction": case_row["jurisdiction"],
        "ruleKey": rule.rule_key if rule else None,
        "ruleGap": rule is None,
        "steps": [step.as_pack_step() for step in rule.steps] if rule else [],
    }


def parse_rule_partition_key(rule_key: str) -> tuple[Jurisdiction, PaymentRail, str]:
    """RULE#<jurisdiction>#<paymentRail>#<party> → its parsed parts; ValueError on any other shape or an unknown value."""
    matched = RULE_KEY_PATTERN.match(rule_key)
    if matched is None:
        raise ValueError("not a RULE# partition key")
    return Jurisdiction(matched.group(1)), PaymentRail(matched.group(2)), matched.group(3)


def list_rule_versions(rule_key: str) -> list[RefundRule]:
    """Every version of the key, oldest first, whatever its status. The key is rebuilt from its parsed parts."""
    partition_key = rule_partition_key(*parse_rule_partition_key(rule_key))
    response = graph.get_table().query(KeyConditionExpression=Key("PK").eq(partition_key))
    return sorted((parse_rule_row(item) for item in response["Items"]), key=lambda rule: rule.version)


def read_rule_seed_rows(seed_directory: Path) -> list[dict]:
    """Every refund_rules[.<CC>].json under the directory, as parsed RULE# items. A rule filed under the wrong country or a
    (PK, SK) seen twice raises ValueError. Nothing is written here."""
    rows: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for seed_file in sorted(seed_directory.iterdir()):
        matched = SEED_RULE_FILE_PATTERN.match(seed_file.name)
        if matched is None:
            continue
        file_jurisdiction = matched.group(1)
        for seed_rule in json.loads(seed_file.read_text(), parse_float=Decimal):
            row = rule_row_from_seed(seed_rule)
            parse_rule_row(row)
            if file_jurisdiction is not None and row["jurisdiction"] != file_jurisdiction:
                raise ValueError(f"{seed_file.name} holds a {row['jurisdiction']} rule")
            if (row["PK"], row["SK"]) in seen_keys:
                raise ValueError(f"{row['PK']} {row['SK']} is seeded twice")
            seen_keys.add((row["PK"], row["SK"]))
            rows.append(row)
    return rows


def write_proposed_rule(rule_key: str, steps: list[dict], evidence: list[str]) -> int:
    """Writes the next version as "proposed" by the keeper and returns its number. There is no status parameter, and the
    put never overwrites a row. Raises ProposalMatchesRejected when a rejected version holds the same steps."""
    jurisdiction, payment_rail, party = parse_rule_partition_key(rule_key)
    versions = list_rule_versions(rule_key)
    version = versions[-1].version + 1 if versions else 1
    item = rule_row_from_seed({
        "jurisdiction": str(jurisdiction), "paymentRail": str(payment_rail), "party": party, "version": version,
        "status": str(RuleState.PROPOSED), "proposedBy": str(ProposedBy.KEEPER), "checkedAt": graph.now_timestamp(),
        "rejectReason": None, "steps": steps, "extra": {"evidence": list(evidence)},
    })
    proposed_rule = parse_rule_row(item)
    for rejected in (rule for rule in versions if rule.status is RuleState.REJECTED):
        if rejected.steps == proposed_rule.steps:
            raise ProposalMatchesRejected(rejected.version, rejected.reject_reason)
    graph.get_table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    return version


def set_proposed_rule_status(rule_key: str, version: int, update_expression: str, values: dict) -> None:
    """One conditional update on a proposed version; any other state (or no row) raises RuleNotProposed."""
    if version < 1:
        raise ValueError("a rule version starts at 1")
    partition_key = rule_partition_key(*parse_rule_partition_key(rule_key))
    try:
        graph.get_table().update_item(
            Key={"PK": partition_key, "SK": f"{RULE_VERSION_PREFIX}{version}"},
            UpdateExpression=update_expression,
            ConditionExpression="#status = :proposed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":proposed": str(RuleState.PROPOSED), **values},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != CONDITIONAL_CHECK_FAILED:
            raise
        raise RuleNotProposed(f"{partition_key} v{version} is not a proposed version") from None


def approve_rule(rule_key: str, version: int) -> None:
    """The proposed version goes live; older live versions stay as they are (the newest live one is read)."""
    set_proposed_rule_status(rule_key, version, "SET #status = :live, checkedAt = :now",
                             {":live": str(RuleState.LIVE), ":now": graph.now_timestamp()})


def reject_rule(rule_key: str, version: int, reason: str) -> None:
    """The proposed version is rejected with its reason, so the keeper never proposes the same steps again."""
    if not 1 <= len(reason.strip()) <= REJECT_REASON_MAX_LENGTH:
        raise ValueError(f"a reject reason is 1 to {REJECT_REASON_MAX_LENGTH} characters")
    set_proposed_rule_status(rule_key, version, "SET #status = :rejected, rejectReason = :reason",
                             {":rejected": str(RuleState.REJECTED), ":reason": reason.strip()})
