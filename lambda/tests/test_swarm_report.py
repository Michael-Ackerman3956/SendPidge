"""Scoring and the cost gate. The by-axis counts are checked against a hand-built fixture, the gate refuses before the first
S3 drop, holding every control is counted as a false hold, and a planted camouflage that no reason named is a recorded miss,
not a silent pass."""

import sys
from pathlib import Path
from types import SimpleNamespace

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))

from shared.enums import PolicyDecision, ReasonCode  # noqa: E402

import swarm  # noqa: E402
from swarm_cases import (  # noqa: E402
    ATTACK_EXPECTED,
    BENIGN_PASS_OR_WARN,
    SWARM_TEMPLATES,
    BodyEncoding,
    InjectionPlacement,
    Language,
    LinkCamouflage,
    SenderTrick,
    SwarmCase,
    SwarmTemplate,
    generate_cases,
)
from swarm_report import (  # noqa: E402
    EXIT_COST_REFUSED,
    EXIT_FAILURES,
    EXPECTED_REASON_BY_CAMOUFLAGE,
    SwarmResult,
    by_axis,
    by_axis_range,
    judge,
    print_headline,
    totals,
)

ATTACK_TEMPLATE = next(template for template in SWARM_TEMPLATES if template.is_attack)
CONTROL_TEMPLATE = next(template for template in SWARM_TEMPLATES if not template.is_attack)


def _case(template: SwarmTemplate, language: Language, camouflage: LinkCamouflage, expected) -> SwarmCase:
    return SwarmCase(f"{template.key}-{language.value}-{camouflage.value}", template, language, camouflage,
                     BodyEncoding.PLAIN, InjectionPlacement.NONE, SenderTrick.UNKNOWN_FREE_MAIL, frozenset(expected))


def _result(is_attack, language, camouflage, decision, reasons=()) -> SwarmResult:
    template = ATTACK_TEMPLATE if is_attack else CONTROL_TEMPLATE
    expected = ATTACK_EXPECTED if is_attack else BENIGN_PASS_OR_WARN
    return SwarmResult(_case(template, language, camouflage, expected), decision, tuple(reasons))


def test_by_axis_counts_match_a_fixed_12_case_fixture():
    # 6 attacks (held, caught), 6 controls (passed); the first attack of each language carries a flagged mismatch link.
    results = []
    for index in range(6):
        language = Language.ENGLISH if index % 2 == 0 else Language.KOREAN
        camouflage = LinkCamouflage.TEXT_HREF_MISMATCH if index < 2 else LinkCamouflage.NONE
        reasons = (ReasonCode.LINK_TEXT_HREF_MISMATCH,) if index < 2 else ()
        results.append(_result(True, language, camouflage, PolicyDecision.HOLD, reasons))
    for index in range(6):
        language = Language.ENGLISH if index % 2 == 0 else Language.KOREAN
        results.append(_result(False, language, LinkCamouflage.NONE, PolicyDecision.PASS))

    table = by_axis(results)
    assert table["language"]["en"] == {"attacks": 3, "caught": 3, "ask_capped": 0, "controls": 3, "false_holds": 0,
                                       "camouflage_planted": 1, "camouflage_flagged": 1}
    assert table["language"]["ko"] == {"attacks": 3, "caught": 3, "ask_capped": 0, "controls": 3, "false_holds": 0,
                                       "camouflage_planted": 1, "camouflage_flagged": 1}
    assert table["link_camouflage"]["text_href_mismatch"] == {"attacks": 2, "caught": 2, "ask_capped": 0, "controls": 0,
                                                              "false_holds": 0, "camouflage_planted": 2, "camouflage_flagged": 2}
    assert table["link_camouflage"]["none"] == {"attacks": 4, "caught": 4, "ask_capped": 0, "controls": 6,
                                                "false_holds": 0, "camouflage_planted": 0, "camouflage_flagged": 0}


def test_cost_gate_refuses_before_the_first_drop():
    class RecordingS3:
        def __init__(self):
            self.put_count = 0

        def put_object(self, **_kwargs):
            self.put_count += 1

    cases = generate_cases(7)[:5]
    one_run_estimate = swarm.estimate_cost_usd(len(cases))
    # A zero budget refuses; so does a budget that fits one run but not the three that --repeat 3 would drop.
    for max_cost_usd, repeat in ((0.0, 1), (one_run_estimate * 2.5, 3)):
        recording_s3 = RecordingS3()
        arguments = SimpleNamespace(max_cost_usd=max_cost_usd, repeat=repeat, bucket="bucket", wait_seconds=0, asks_per_hour=3,
                                    holds_already_this_hour=0, delay_seconds=0, out_dir="/tmp/swarm-unused", seed=7)
        exit_code = swarm.run_live(cases, recording_s3, table=None, arguments=arguments)
        assert exit_code == EXIT_COST_REFUSED == 2
        assert recording_s3.put_count == 0


def test_hold_everything_fails_the_control_half():
    # 64 attacks and 64 controls; force every decision to HOLD. Each control's expected is PASS/WARN, so all 64 are false holds.
    results = [_result(True, Language.ENGLISH, LinkCamouflage.NONE, PolicyDecision.HOLD) for _ in range(64)]
    results += [_result(False, Language.ENGLISH, LinkCamouflage.NONE, PolicyDecision.HOLD) for _ in range(64)]
    for result in results:
        judge(result)
    assert totals(results)["false_holds"] == 64
    assert print_headline(results) == EXIT_FAILURES


def test_unflagged_planted_camouflage_is_a_miss_not_a_pass():
    planted_but_unflagged = _result(True, Language.ENGLISH, LinkCamouflage.QR_IN_IMAGE, PolicyDecision.HOLD, reasons=())
    assert planted_but_unflagged.camouflage_planted and not planted_but_unflagged.camouflage_flagged
    flagged = _result(True, Language.ENGLISH, LinkCamouflage.TEXT_HREF_MISMATCH, PolicyDecision.WARN,
                      reasons=(ReasonCode.LINK_TEXT_HREF_MISMATCH,))
    cell = by_axis([planted_but_unflagged, flagged])["link_camouflage"]
    assert cell["qr_in_image"]["camouflage_planted"] == 1 and cell["qr_in_image"]["camouflage_flagged"] == 0
    assert cell["text_href_mismatch"]["camouflage_planted"] == 1 and cell["text_href_mismatch"]["camouflage_flagged"] == 1


def test_repeated_runs_report_the_per_axis_min_and_max():
    # The same attack and control twice: run 1 holds the attack and passes the control, run 2 misses the attack and holds the control.
    first_run = [_result(True, Language.ENGLISH, LinkCamouflage.NONE, PolicyDecision.HOLD),
                 _result(False, Language.ENGLISH, LinkCamouflage.NONE, PolicyDecision.PASS)]
    second_run = [_result(True, Language.ENGLISH, LinkCamouflage.NONE, PolicyDecision.PASS),
                  _result(False, Language.ENGLISH, LinkCamouflage.NONE, PolicyDecision.HOLD)]
    english = by_axis_range([first_run, second_run])["language"]["en"]
    assert (english["attacks"], english["caught"], english["false_holds"]) == (
        {"min": 1, "max": 1}, {"min": 0, "max": 1}, {"min": 0, "max": 1})


def test_a_planted_kind_counts_only_its_own_reason():
    qr_in_pdf_with_image_only_body = _result(True, Language.ENGLISH, LinkCamouflage.QR_IN_PDF, PolicyDecision.HOLD,
                                             reasons=(ReasonCode.IMAGE_ONLY_BODY,))
    assert qr_in_pdf_with_image_only_body.camouflage_planted and not qr_in_pdf_with_image_only_body.camouflage_flagged
    nothing_planted_with_a_reason = _result(True, Language.ENGLISH, LinkCamouflage.NONE, PolicyDecision.HOLD,
                                            reasons=(ReasonCode.WHOLE_BODY_LINK,))
    assert not nothing_planted_with_a_reason.camouflage_flagged
    whole_body = _result(True, Language.ENGLISH, LinkCamouflage.WHOLE_BODY_ANCHOR, PolicyDecision.HOLD,
                         reasons=(ReasonCode.WHOLE_BODY_LINK,))
    assert whole_body.camouflage_flagged
    published_misses = set(LinkCamouflage) - {LinkCamouflage.NONE} - set(EXPECTED_REASON_BY_CAMOUFLAGE)
    assert published_misses == {LinkCamouflage.TRUSTED_SERVICE_REDIRECT, LinkCamouflage.QR_IN_PDF,
                                LinkCamouflage.CALLBACK_PHONE_ONLY, LinkCamouflage.CALENDAR_ICS}
