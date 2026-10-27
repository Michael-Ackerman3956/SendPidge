"""The swarm's offline half and its moved judge: templates parse as the sender the trick claims, a scripted attack is caught
and a scripted control is not held, the leak check catches a number in any formatting, and the judge calls an unreadable mark
and an over-cap send failures while a capped hold passes."""

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))

from shared.email_parse import parse_eml  # noqa: E402
from shared.enums import PolicyDecision, WarnReason  # noqa: E402
from shared.graph import domains_of  # noqa: E402

from swarm import DEFAULT_LISTS, build_contacts, holds_counted_for_hour, load_list_store, offline_result  # noqa: E402
from swarm_cases import SWARM_TEMPLATES, Language, SenderTrick, generate_cases  # noqa: E402
from swarm_render import build_case_eml  # noqa: E402
from swarm_report import SwarmResult, judge, leaked_indicators  # noqa: E402

CASES = generate_cases(7)
FIRST_ATTACK = next(case for case in CASES if case.is_attack)
FIRST_CONTROL = next(case for case in CASES if not case.is_attack)


def test_fourteen_lure_templates_and_four_controls():
    assert [template.is_attack for template in SWARM_TEMPLATES].count(True) == 14
    assert [template.is_attack for template in SWARM_TEMPLATES].count(False) == 4
    injection_pack = next(template for template in SWARM_TEMPLATES if template.key == "injection-pack")
    assert injection_pack.scripted_verdict().intent.value == "money"


def test_an_unknown_free_mail_case_parses_as_the_contact_name_it_claims():
    case = next(case for case in CASES if case.sender_trick is SenderTrick.UNKNOWN_FREE_MAIL and case.is_attack)
    parsed = parse_eml(build_case_eml(case))
    assert parsed.sender_name == case.template.claimed_name(case.language)
    assert parsed.pidge_auth_header_values == ("spf=fail;dkim=fail",)


def test_a_scripted_attack_is_caught_and_a_scripted_control_is_not_a_false_hold():
    contacts = build_contacts()
    family_domains = domains_of(contacts)
    list_store = load_list_store(DEFAULT_LISTS)
    attack_result = offline_result(FIRST_ATTACK, contacts, family_domains, list_store)
    assert attack_result.is_caught and not attack_result.failures
    control_result = offline_result(FIRST_CONTROL, contacts, family_domains, list_store)
    assert not control_result.is_false_hold and not control_result.failures


def test_leak_check_finds_digits_in_any_formatting_and_passes_a_clean_summary():
    indicators = ("3333-01-1234567",)
    assert leaked_indicators("Send to 3333 01 1234567 now", indicators) == ["3333-01-1234567"]
    assert leaked_indicators("Asks for money for a hospital deposit.", indicators) == []


def test_judge_calls_unreadable_a_failure_and_the_fourth_hold_must_not_send():
    unreadable = SwarmResult(FIRST_ATTACK, PolicyDecision.WARN, warn_reason=WarnReason.UNREADABLE)
    judge(unreadable)
    assert any("run failure" in failure for failure in unreadable.failures)

    over_cap = SwarmResult(FIRST_ATTACK, PolicyDecision.HOLD, channels_sent=["inapp"], ask_capped=False)
    judge(over_cap, hold_count_before=3, asks_per_hour=3)
    assert any("still sent" in failure for failure in over_cap.failures)

    capped = SwarmResult(FIRST_ATTACK, PolicyDecision.WARN, warn_reason=WarnReason.ASK_CAPPED,
                         channels_sent=[], ask_capped=True)
    judge(capped, hold_count_before=3, asks_per_hour=3)
    assert capped.scored_decision is PolicyDecision.HOLD and capped.failures == []


def test_the_hold_count_restarts_when_the_utc_hour_changes():
    assert holds_counted_for_hour(3, "2026-10-27T09", "2026-10-27T09") == 3
    assert holds_counted_for_hour(3, "2026-10-27T09", "2026-10-27T10") == 0
