"""RULE# rows: only live versions resolve, the four seeded keys exist, a missing key is a rule gap, a broken envelope raises."""

import json
from pathlib import Path

import pytest

from shared import graph, refund_rules
from shared.enums import Jurisdiction, PaymentRail
from tests.test_verdict import aws  # noqa: F401  (the moto table fixture)

SEED_PATH = Path(__file__).resolve().parents[2] / "infra" / "seed" / "refund_rules.json"


def seed_rules() -> list[dict]:
    rows = [refund_rules.rule_row_from_seed(seed_rule) for seed_rule in json.loads(SEED_PATH.read_text())]
    for row in rows:
        graph.get_table().put_item(Item=row)
    return rows


def test_the_four_seeded_us_rules_resolve_with_an_https_source_on_every_step(aws):
    seed_rules()
    for rail in PaymentRail:
        rule = refund_rules.find_live_rule(Jurisdiction.US, rail)
        assert rule is not None and rule.payment_rail is rail and rule.version == 1
        assert all(step.source_url.startswith("https://") for step in rule.steps)
    assert refund_rules.find_live_rule(Jurisdiction.US, PaymentRail.CARD).steps[0].contact == refund_rules.CARD_ISSUER_CONTACT


def test_a_missing_key_is_a_rule_gap_and_a_proposed_version_is_never_read(aws):
    seed_rules()
    assert refund_rules.find_live_rule(Jurisdiction.KR, PaymentRail.CARD) is None
    proposed = refund_rules.rule_row_from_seed({**json.loads(SEED_PATH.read_text())[0], "version": 2, "status": "proposed", "proposedBy": "keeper",
                                                "steps": [{"order": 1, "action": "Do nothing", "contact": "nobody", "sourceUrl": "https://example.com"}]})
    graph.get_table().put_item(Item=proposed)
    assert refund_rules.find_live_rule(Jurisdiction.US, PaymentRail.CARD).version == 1


def test_a_broken_envelope_raises_instead_of_becoming_a_pack():
    with pytest.raises(ValueError):
        refund_rules.parse_rule_row({"PK": "RULE#US#card#any", "SK": "v1", "jurisdiction": "US", "paymentRail": "card", "party": "any",
                                     "status": "live", "proposedBy": "seed", "steps": [{"order": 1, "action": "x", "contact": "y", "sourceUrl": "http://plain"}]})
    with pytest.raises(KeyError):
        refund_rules.parse_rule_row({"PK": "RULE#US#card#any", "SK": "v1", "jurisdiction": "US", "paymentRail": "card", "party": "any",
                                     "status": "live", "proposedBy": "seed", "steps": [{"order": 1}]})


def test_the_pack_carries_only_case_and_rule_facts(aws):
    seed_rules()
    case_row = {"PK": "CASE#case-1", "paymentRail": "card", "jurisdiction": "US", "bank": "Chase", "amount": 500, "currency": "USD",
                "paidAt": "2026-10-20T09:00:00Z"}
    pack = refund_rules.build_recovery_pack(case_row, refund_rules.find_live_rule(Jurisdiction.US, PaymentRail.CARD))
    assert (pack["caseId"], pack["ruleKey"], pack["ruleGap"], len(pack["steps"])) == ("case-1", "RULE#US#card#any", False, 3)
    assert "bank" not in pack and "amount" not in pack
    assert set(pack["drafts"]) == {"callScript", "disputeLetter", "policeReport"}
    gap_pack = refund_rules.build_recovery_pack({**case_row, "jurisdiction": "KR"}, None)
    assert (gap_pack["ruleGap"], gap_pack["steps"]) == (True, [])
