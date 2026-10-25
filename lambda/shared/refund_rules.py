"""RULE# rows: the recovery pack's facts. Only live versions are read; the envelope is strict, `extra` is free."""

from dataclasses import dataclass
from decimal import Decimal

from boto3.dynamodb.conditions import Key

from shared import graph
from shared.enums import Jurisdiction, PaymentRail, ProposedBy, RuleState

ANY_PARTY = "any"
RULE_VERSION_PREFIX = "v"
CARD_ISSUER_CONTACT = "the number on the back of your card"


@dataclass(frozen=True)
class RefundRuleStep:
    order: int
    action: str
    contact: str
    source_url: str
    deadline_hours: int | None = None
    deadline_days: int | None = None

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


def rule_partition_key(jurisdiction: Jurisdiction, payment_rail: PaymentRail, party: str = ANY_PARTY) -> str:
    return f"RULE#{jurisdiction}#{payment_rail}#{party}"


def optional_int(value) -> int | None:
    return None if value is None else int(value)


def parse_rule_row(item: dict) -> RefundRule:
    """Raises (KeyError / ValueError) on a broken envelope: a half-written rule must never become a pack."""
    steps = tuple(
        RefundRuleStep(int(step["order"]), str(step["action"]), str(step["contact"]), str(step["sourceUrl"]),
                       optional_int(step.get("deadlineHours")), optional_int(step.get("deadlineDays")))
        for step in item["steps"]
    )
    if not steps or any(not step.source_url.startswith("https://") for step in steps):
        raise ValueError(f"rule {item['PK']} {item['SK']} has no steps or a step without an https source")
    return RefundRule(
        item["PK"], int(item["SK"].removeprefix(RULE_VERSION_PREFIX)), Jurisdiction(item["jurisdiction"]),
        PaymentRail(item["paymentRail"]), str(item["party"]), RuleState(item["status"]), ProposedBy(item["proposedBy"]),
        steps, dict(item.get("extra", {})),
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
    """Facts for Grandma's recovery pack: the case's rail and the rule's steps. No model output goes in here."""
    return {
        "caseId": graph.case_id_of(case_row),
        "paymentRail": case_row["paymentRail"],
        "jurisdiction": case_row["jurisdiction"],
        "ruleKey": rule.rule_key if rule else None,
        "ruleGap": rule is None,
        "steps": [step.as_pack_step() for step in rule.steps] if rule else [],
    }
