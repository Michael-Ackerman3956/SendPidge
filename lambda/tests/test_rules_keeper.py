"""Run 4 stub: the real rules keeper loop, its read-only tools and the BUDGET# ledger on the moto table, with a scripted
model in place of Bedrock and the mocked bank page in infra/seed/mock_pages."""

import importlib
import inspect
import json
from decimal import Decimal

import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from shared import budget, graph, refund_rules, rules_keeper
from shared.budget import BudgetExhausted, BudgetLedger, BudgetLine, KeeperSessionOutcome, KeeperSessionResult
from shared.enums import Jurisdiction, PaymentRail, ReaderModelChoice
from tests.helpers.scripted_model import ScriptedModel, tool_call_step, verdict_step
from tests.test_refund_rules import SEED_PATH, seed_rules
from tests.test_verdict import aws  # noqa: F401  (the moto table fixture)

RULE_KEY = "RULE#US#card#any"
GAP_RULE_KEY = "RULE#KR#card#any"
BUDGET_DAY = "2026-10-27"
BUDGET_KEY = {"PK": "BUDGET#2026-10-27", "SK": "META"}
PAGE_URL = "https://www.examplebank.example/card-dispute"
REJECT_REASON = "Copies the FTC page, not the bank's terms"
PROPOSAL = {
    "has_rule": True,
    "steps": [
        {"order": 1, "action": "Call your card issuer and ask for a chargeback.", "contact": "the number on the back of your card",
         "organisation": "Example Bank", "source_url": PAGE_URL, "deadline_days": 60},
        {"order": 2, "action": "Report the scam to the FTC.", "contact": "the FTC",
         "organisation": "Federal Trade Commission (FTC)", "source_url": PAGE_URL},
        {"order": 3, "action": "File a complaint with the FBI's Internet Crime Complaint Center.", "contact": "the FBI",
         "organisation": "FBI Internet Crime Complaint Center", "source_url": PAGE_URL},
    ],
    "evidence": ["call the number on the back of your card within 60 days and ask for a chargeback"],
}
FULL_SESSION_STEPS = [tool_call_step("read_rule", {}), tool_call_step("search_sources", {}),
                      tool_call_step("fetch_page", {"url": PAGE_URL}), verdict_step(PROPOSAL)]


@pytest.fixture
def keeper(aws, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RULES_KEEPER_MOCK", "true")
    return seed_rules()


def rules_ledger() -> BudgetLedger:
    return BudgetLedger(BudgetLine.RULES, BUDGET_DAY)


def keeper_scope() -> rules_keeper.RulesKeeperScope:
    return rules_keeper.RulesKeeperScope(RULE_KEY, rules_keeper.mock_page_hosts())


def keeper_with_scripted_model(scope: rules_keeper.RulesKeeperScope, ledger: BudgetLedger, steps: list) -> tuple:
    agent = rules_keeper.build_rules_keeper_agent(ReaderModelChoice.HAIKU_4_5, scope, ledger)
    scripted_model = ScriptedModel(rules_keeper.RuleProposal, {rules_keeper.run_prompt(scope): steps})
    agent.model = scripted_model
    return agent, scripted_model


def run_session(ledger: BudgetLedger, steps: list) -> tuple[KeeperSessionResult, ScriptedModel]:
    scope = keeper_scope()
    agent, scripted_model = keeper_with_scripted_model(scope, ledger, steps)
    return rules_keeper.run_rules_keeper_session(agent, scope, ledger), scripted_model


def proposal_with_step(position: int, **changed_fields) -> dict:
    steps = [dict(step) for step in PROPOSAL["steps"]]
    steps[position] = {**steps[position], **changed_fields}
    return {**PROPOSAL, "steps": steps}


def budget_item(key: dict = BUDGET_KEY) -> dict | None:
    return graph.get_table().get_item(Key=key).get("Item")


def rule_item(version: int, rule_key: str = RULE_KEY) -> dict | None:
    return graph.get_table().get_item(Key={"PK": rule_key, "SK": f"v{version}"}).get("Item")


def seed_spent(spent_usd: Decimal) -> None:
    graph.get_table().put_item(Item={**BUDGET_KEY, "spentUsd": spent_usd, "limitUsd": Decimal("20")})


def budget_exhausted_lines(captured_output: str) -> list[str]:
    return [line for line in captured_output.splitlines() if budget.BUDGET_EXHAUSTED_METRIC_NAME in line]


def test_a_session_proposes_v2_and_never_live(keeper):
    us_card_seed_row = next(row for row in keeper if row["PK"] == RULE_KEY)
    result, scripted_model = run_session(rules_ledger(), FULL_SESSION_STEPS)
    assert result == KeeperSessionResult(KeeperSessionOutcome.PROPOSED, 2)
    proposed = rule_item(2)
    assert (proposed["status"], proposed["proposedBy"], proposed["extra"]) == ("proposed", "keeper", {"evidence": PROPOSAL["evidence"]})
    assert [(step["order"], step["sourceUrl"], step["organisation"]) for step in proposed["steps"]] == [
        (1, PAGE_URL, "Example Bank"), (2, PAGE_URL, "Federal Trade Commission (FTC)"), (3, PAGE_URL, "FBI Internet Crime Complaint Center")]
    assert rule_item(1) == us_card_seed_row
    assert refund_rules.find_live_rule(Jurisdiction.US, PaymentRail.CARD).version == 1
    ledger_row = budget_item()
    assert (ledger_row["spentUsd"], ledger_row["sessions"], ledger_row["proposals"], ledger_row["limitUsd"]) == (Decimal("0.22"), 1, 1, 20)
    assert len(scripted_model.recorded_calls) == 4  # 4 model debits of 0.05 and one page fetch of 0.02
    assert "status" not in rules_keeper.RuleProposal.model_fields
    assert "status" not in inspect.signature(refund_rules.write_proposed_rule).parameters
    assert scripted_model.raised_errors == []


def test_a_debit_past_the_limit_ends_the_session_with_no_row(keeper, capsys):
    seed_spent(Decimal("19.96"))
    result, scripted_model = run_session(rules_ledger(), FULL_SESSION_STEPS)
    assert result == KeeperSessionResult(KeeperSessionOutcome.BUDGET_EXHAUSTED, None)
    assert scripted_model.recorded_calls == []
    assert budget_item()["spentUsd"] == Decimal("19.96")
    assert rule_item(2) is None
    assert len(budget_exhausted_lines(capsys.readouterr().out)) == 1

    seed_spent(Decimal("19.88"))
    steps = [tool_call_step("read_rule", {}), tool_call_step("fetch_page", {"url": PAGE_URL}), verdict_step(PROPOSAL)]
    result, scripted_model = run_session(rules_ledger(), steps)
    assert result == KeeperSessionResult(KeeperSessionOutcome.BUDGET_EXHAUSTED, None)
    assert len(scripted_model.recorded_calls) == 2
    assert budget_item()["spentUsd"] == Decimal("20.00")  # 0.05 + 0.05 + 0.02; the third model debit was refused
    assert rule_item(2) is None
    assert len(budget_exhausted_lines(capsys.readouterr().out)) == 1
    assert scripted_model.raised_errors == []


def test_the_ledger_debit_is_conditional_and_fails_closed(aws, monkeypatch):  # noqa: F811
    seed_spent(Decimal("19.90"))
    ledger = rules_ledger()
    assert ledger.debit(budget.MODEL_CALL_COST_USD) == Decimal("19.95")
    assert ledger.debit(budget.MODEL_CALL_COST_USD) == Decimal("20.00")
    with pytest.raises(BudgetExhausted):
        ledger.debit(budget.MODEL_CALL_COST_USD)
    assert ledger.is_exhausted and budget_item()["spentUsd"] == Decimal("20.00")

    def raise_throttled(**update_arguments):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}}, "UpdateItem")

    monkeypatch.setattr(graph.get_table(), "update_item", raise_throttled)
    fresh_ledger = rules_ledger()
    with pytest.raises(ClientError):
        fresh_ledger.debit(budget.MODEL_CALL_COST_USD)
    assert fresh_ledger.is_exhausted is False


def test_approve_makes_v2_live_and_keeps_v1(keeper):
    result, _ = run_session(rules_ledger(), FULL_SESSION_STEPS)
    assert result.proposed_version == 2
    refund_rules.approve_rule(RULE_KEY, 2)
    assert refund_rules.find_live_rule(Jurisdiction.US, PaymentRail.CARD).version == 2
    assert rule_item(1)["status"] == "live"
    with pytest.raises(refund_rules.RuleNotProposed):
        refund_rules.approve_rule(RULE_KEY, 2)


def test_a_rejected_proposal_keeps_its_reason_and_is_not_proposed_again(keeper):
    run_session(rules_ledger(), FULL_SESSION_STEPS)
    refund_rules.reject_rule(RULE_KEY, 2, REJECT_REASON)
    assert (rule_item(2)["status"], rule_item(2)["rejectReason"]) == ("rejected", REJECT_REASON)
    result, scripted_model = run_session(rules_ledger(), FULL_SESSION_STEPS)
    assert result == KeeperSessionResult(KeeperSessionOutcome.REFUSED, None)
    assert rule_item(3) is None and scripted_model.raised_errors == []
    read_rule = rules_keeper.build_rules_keeper_tools(keeper_scope(), rules_ledger())[0]
    assert [(version["version"], version["status"], version["rejectReason"]) for version in read_rule()] == [
        (1, "live", None), (2, "rejected", REJECT_REASON)]


def test_unsafe_or_off_list_proposals_are_refused(keeper):
    unsafe_proposal = proposal_with_step(0, action="Call 1-800-555-0199 and ask for a chargeback.")
    off_list_proposal = proposal_with_step(1, source_url="https://evil.example/")
    for proposal in (unsafe_proposal, off_list_proposal):
        steps = [*FULL_SESSION_STEPS[:-1], verdict_step(proposal)]
        result, scripted_model = run_session(rules_ledger(), steps)
        assert result == KeeperSessionResult(KeeperSessionOutcome.REFUSED, None)
        assert rule_item(2) is None and scripted_model.raised_errors == []
    unused_day_ledger = BudgetLedger(BudgetLine.RULES, "2026-10-26")
    fetch_page = rules_keeper.build_rules_keeper_tools(keeper_scope(), unused_day_ledger)[2]
    for url in ("https://www.examplebank.example/../x", "http://www.examplebank.example/", "https://user@www.examplebank.example/"):
        with pytest.raises(ValueError):
            fetch_page(url=url)
    assert budget_item({"PK": "BUDGET#2026-10-26", "SK": "META"}) is None


def case_record(event_name: str, case_id: str, rule_gap: bool, jurisdiction: str = "KR") -> dict:
    serializer = TypeSerializer()
    image = {"PK": f"CASE#{case_id}", "SK": "META", "ruleGap": rule_gap, "jurisdiction": jurisdiction, "paymentRail": "card"}
    return {"eventName": event_name, "dynamodb": {
        "Keys": {key: serializer.serialize(image[key]) for key in ("PK", "SK")},
        "NewImage": {key: serializer.serialize(value) for key, value in image.items()},
    }}


def test_a_rule_gap_case_enqueues_exactly_one_session(aws, monkeypatch):  # noqa: F811
    seed_rules()  # US rules only: the peer's KR card rule (seeded proposed) is not loaded here
    dispatch = importlib.import_module("pidge_rules_dispatch.handler")
    recorded_sessions = []

    def record_session(rule_key, ledger, model_choice):
        recorded_sessions.append((rule_key, model_choice))
        return KeeperSessionResult(KeeperSessionOutcome.PROPOSED, 1)

    monkeypatch.setattr(rules_keeper, "run_rules_keeper_for_key", record_session)
    monkeypatch.setenv("RULES_KEEPER_ENABLED", "true")
    monkeypatch.setenv("RULES_KEEPER_MODEL", "haiku-4.5")
    event = {"Records": [case_record("INSERT", "c1", True), case_record("INSERT", "c1", True),
                         case_record("INSERT", "c2", False, "US"), case_record("INSERT", "c3", True),
                         case_record("MODIFY", "c1", True)]}
    assert dispatch.handle(event, None) == {"queued": [GAP_RULE_KEY], "started": [GAP_RULE_KEY]}
    assert recorded_sessions == [(GAP_RULE_KEY, ReaderModelChoice.HAIKU_4_5)]

    monkeypatch.delenv("RULES_KEEPER_ENABLED")
    assert dispatch.handle(event, None) == {"queued": [GAP_RULE_KEY], "started": []}
    assert len(recorded_sessions) == 1
    us_card_seed_rule = json.loads(SEED_PATH.read_text())[0]
    graph.get_table().put_item(Item=refund_rules.rule_row_from_seed(
        {**us_card_seed_rule, "jurisdiction": "KR", "status": "proposed", "proposedBy": "keeper"}))
    assert rule_item(1, GAP_RULE_KEY)["status"] == "proposed"
    assert dispatch.handle(event, None)["queued"] == []


def test_live_fetch_is_refused(aws, monkeypatch):  # noqa: F811
    seed_rules()
    monkeypatch.delenv("RULES_KEEPER_MOCK", raising=False)
    ledger, scope = rules_ledger(), keeper_scope()
    agent, scripted_model = keeper_with_scripted_model(scope, ledger, FULL_SESSION_STEPS)
    with pytest.raises(rules_keeper.LivePageFetchNotBuilt):
        rules_keeper.run_rules_keeper_session(agent, scope, ledger)
    assert scripted_model.recorded_calls == []
    assert budget_item() is None
