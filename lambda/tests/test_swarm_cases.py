"""The generated swarm is deterministic, balanced across every axis, and renders mail the shipped parser reads the way the
catalogue claims. The pinned count and SHA below are computed from the generator; changing the generator, the templates or
the axis values changes them, and this test is where you re-pin."""

import hashlib
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))

from shared.email_parse import collapse_whitespace, parse_eml  # noqa: E402

from swarm_cases import (  # noqa: E402
    BODY_ENCODINGS,
    INJECTION_PLACEMENTS,
    LANGUAGES,
    LINK_CAMOUFLAGES,
    SENDER_TRICKS,
    SWARM_TEMPLATES,
    BodyEncoding,
    LinkCamouflage,
    generate_cases,
)
from swarm_render import (  # noqa: E402
    CAMOUFLAGE_PAST_THE_LINK_CAP,
    CAMOUFLAGE_PLANTS_NO_PARSEABLE_ANCHOR,
    LURE_HOST,
    PLANTED_HREF_TOKEN,
    build_case_eml,
)

SEED = 7
# Pinned from generate_cases(SEED). Changing the generator changes these; recompute and re-pin here when you do.
PINNED_CASE_COUNT = 792
PINNED_CASE_ID_SHA256 = "a226e00a26555a3cffe1e588c555d6c5a45ccb02efde5b57a822273b23a7b016"

# Lure survives the parse into text_body for every encoding except the two that keep it out of the part the Reader reads.
ENCODINGS_THAT_KEEP_THE_LURE = frozenset({
    BodyEncoding.PLAIN, BodyEncoding.HTML_ONLY, BodyEncoding.BASE64_HTML, BodyEncoding.QUOTED_PRINTABLE,
    BodyEncoding.INVALID_HTML, BodyEncoding.CAPITALISED_TAGS,
})
ENCODINGS_THAT_HIDE_THE_LURE = frozenset({BodyEncoding.MULTIPART_DIVERGENT, BodyEncoding.TEXT_IN_IMAGE})


def test_generator_is_deterministic_for_a_seed():
    cases = generate_cases(SEED)
    assert len(cases) == PINNED_CASE_COUNT
    case_ids = [case.case_id for case in cases]
    assert len(set(case_ids)) == len(case_ids)
    assert hashlib.sha256("\n".join(case_ids).encode()).hexdigest() == PINNED_CASE_ID_SHA256
    assert [case.case_id for case in generate_cases(SEED)] == case_ids  # same seed, same ids
    controls = [case for case in cases if not case.is_attack]
    assert len(controls) * 3 >= len(cases)  # a third are controls


def test_every_axis_value_appears_with_every_template():
    cases = generate_cases(SEED)
    for template in SWARM_TEMPLATES:
        template_cases = [case for case in cases if case.template.key == template.key]
        assert {case.language for case in template_cases} == set(LANGUAGES)
        assert {case.camouflage for case in template_cases} == set(LINK_CAMOUFLAGES)
        assert {case.encoding for case in template_cases} == set(BODY_ENCODINGS)
        assert {case.injection for case in template_cases} == set(INJECTION_PLACEMENTS)
        assert {case.sender_trick for case in template_cases} == set(SENDER_TRICKS)


def test_at_least_a_third_of_cases_are_controls():
    cases = generate_cases(SEED)
    controls = [case for case in cases if not case.is_attack]
    assert len(controls) * 3 >= len(cases)
    assert {template.key for template in SWARM_TEMPLATES if not template.is_attack} == {
        "control-gift", "control-family-chat", "control-store-receipt", "control-newsletter"}


def test_every_case_parses_and_the_lure_survives_its_encoding():
    one_case_per_encoding_and_language = {}
    for case in generate_cases(SEED):
        one_case_per_encoding_and_language.setdefault((case.encoding, case.language), case)
    for (encoding, language), case in one_case_per_encoding_and_language.items():
        parsed = parse_eml(build_case_eml(case))
        assert (parsed.sender_name, parsed.sender_address) != ("", "")  # it parses as a sender
        lure = collapse_whitespace(case.template.lure(language))
        lure_in_body = lure in collapse_whitespace(parsed.text_body)
        if encoding in ENCODINGS_THAT_KEEP_THE_LURE:
            assert lure_in_body, f"{encoding} dropped the lure"
        elif encoding in ENCODINGS_THAT_HIDE_THE_LURE:
            assert not lure_in_body, f"{encoding} should keep the lure out of the read part"


def test_planted_camouflage_is_in_parsed_links_or_marks_the_link_cap():
    one_case_per_camouflage = {}
    for case in generate_cases(SEED):
        one_case_per_camouflage.setdefault(case.camouflage, case)
    for camouflage, case in one_case_per_camouflage.items():
        if camouflage is LinkCamouflage.NONE:
            continue  # plants nothing
        parsed = parse_eml(build_case_eml(case))
        hrefs = " ".join(link.href for link in parsed.links)
        if camouflage in CAMOUFLAGE_PAST_THE_LINK_CAP:
            assert parsed.links_truncated and LURE_HOST not in hrefs, f"{camouflage} should be cut by the cap, and say so"
        elif camouflage in CAMOUFLAGE_PLANTS_NO_PARSEABLE_ANCHOR:
            assert LURE_HOST not in hrefs, f"{camouflage} should plant no parseable link"
        else:
            assert PLANTED_HREF_TOKEN[camouflage] in hrefs, f"{camouflage} link was not parsed"


def test_every_indicator_is_in_the_parsed_body():
    templates_with_indicators = {template.key for template in SWARM_TEMPLATES if template.indicators}
    assert templates_with_indicators  # the leak check has body facts to look for
    checked_case_count = 0
    for case in generate_cases(SEED):
        if not case.template.indicators or case.encoding not in ENCODINGS_THAT_KEEP_THE_LURE:
            continue
        text_body = parse_eml(build_case_eml(case)).text_body
        for indicator in case.template.indicators:
            assert indicator in text_body, f"{case.case_id} lost {indicator}"
        checked_case_count += 1
    assert checked_case_count > 0
