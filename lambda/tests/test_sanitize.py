import pytest

from shared.enums import UnsafeTextKind
from shared.sanitize import SUMMARY_MAX_LENGTH, find_unsafe_spans, sanitize_summary


@pytest.mark.parametrize(
    ("raw_summary", "expected"),
    [
        ("Jimin asks you to send money today.", "Jimin asks you to send money today."),
        ("Send 500,000 won to account 110-123-456789 now.", "Send won to account now."),
        ("Call +82-10-1234-5678 or (555) 123-4567.", "Call or."),
        ("Open https://jimin-help.top/pay and www.evil.example/x.", "Open and."),
        ("Go to jimin-help.top today.", "Go to today."),
        ("Reply to help@jimin-help.top for details.", "Reply to for details."),
        ("Code 482913 must be sent back.", "Code must be sent back."),
        ("급해요 1,000,000원 송금해 주세요.", "급해요 원 송금해 주세요."),
    ],
)
def test_links_addresses_and_numbers_are_stripped(raw_summary, expected):
    assert sanitize_summary(raw_summary) == expected


def test_summary_is_capped_at_140_characters():
    long_summary = "word " * 60
    assert len(sanitize_summary(long_summary)) <= SUMMARY_MAX_LENGTH


def test_nothing_digit_like_survives():
    assert not any(character.isdigit() for character in sanitize_summary("a1b22c333 +1 (800) 555-0199"))


@pytest.mark.parametrize(
    ("agent_text", "expected_kinds"),
    [
        ("Call 1-800-555-0199", [UnsafeTextKind.LONG_NUMBER]),
        ("010-1234-5678", [UnsafeTextKind.LONG_NUMBER]),
        ("code 482913", [UnsafeTextKind.LONG_NUMBER]),
        ("₩1,500,000", []),  # a comma-grouped amount with a currency symbol
        ("bank-help.example/refund", [UnsafeTextKind.LINK]),
        ("jimin.help@gmail.com", [UnsafeTextKind.EMAIL_ADDRESS]),
        ("on 2026-10-24", []),
        ("You have 2 held emails", []),
        ("PIN 4821", []),  # the accepted miss: no email body reaches these agents
        ("Call 010\u20131234\u20135678", [UnsafeTextKind.LONG_NUMBER]),
        ("010\u20111234\u20115678", [UnsafeTextKind.LONG_NUMBER]),
        ("010/1234/5678", [UnsafeTextKind.LONG_NUMBER]),
        ("010_1234_5678", [UnsafeTextKind.LONG_NUMBER]),
        ("\uff10\uff11\uff10-\uff11\uff12\uff13\uff14-\uff15\uff16\uff17\uff18", [UnsafeTextKind.LONG_NUMBER]),
        ("phone 2026-10-24-5678", [UnsafeTextKind.LONG_NUMBER]),
        ("Call 1234-56-78", [UnsafeTextKind.LONG_NUMBER]),
        ("on October 24 2026", []),
        ("on October 24, 2026", []),
        ("on 24.10.2026", []),
        ("on 2026. 10. 24.", []),
        ("on 10/24/2026", []),
        ("at 10:30", []),
        ('at "bank-help.example" now', [UnsafeTextKind.LINK]),
        ("[bank-help.example]", [UnsafeTextKind.LINK]),
        ("bank-help.example's page", [UnsafeTextKind.LINK]),
        ("bank-help\uff0eexample", [UnsafeTextKind.LINK]),
        ("bank\u200b-help.example", [UnsafeTextKind.LINK]),
        ("jimin\uff20gmail\uff0ecom", [UnsafeTextKind.EMAIL_ADDRESS]),
        ("visit xn--80ak6aa92e.xn--p1ai", [UnsafeTextKind.LINK]),
        ("Report it to the FTC.", []),
    ],
)
def test_unsafe_spans_golden_table(agent_text, expected_kinds):
    assert find_unsafe_spans(agent_text) == expected_kinds


def test_summary_strips_a_quoted_and_a_fullwidth_domain():
    assert "bank-help" not in sanitize_summary('Asks you to verify at "bank-help.example" now')
    fullwidth_summary = sanitize_summary("Asks you to verify at \uff42\uff41\uff4e\uff4b\uff0d\uff48\uff45\uff4c\uff50\uff0e\uff45\uff58\uff41\uff4d\uff50\uff4c\uff45 now")
    assert "bank-help" not in fullwidth_summary and "\uff42\uff41\uff4e\uff4b" not in fullwidth_summary


@pytest.mark.parametrize(
    "agent_text",
    ["Pay at zelle-help.com에서 today", "zelle-help.com으로 송금하라고 했어요", "evil.com- now", "evil.com_ now", "evil.comへ now"],
)
def test_a_domain_glued_to_a_word_is_still_a_link(agent_text):
    assert find_unsafe_spans(agent_text) == [UnsafeTextKind.LINK]
    summary = sanitize_summary(agent_text)
    assert "zelle-help.com" not in summary and "evil.com" not in summary


def test_stripping_a_number_never_leaves_a_link():
    assert "evil.com" not in sanitize_summary("Go to evil.com1 now")


@pytest.mark.parametrize(
    ("agent_text", "expected_kinds"),
    [
        ("Visit evil.рф now", [UnsafeTextKind.LINK]),
        ("Visit kb은행.한국 now", [UnsafeTextKind.LINK]),
        ("확인했어요.감사합니다", []),  # the over-refusal guard: a Korean word is not a TLD
    ],
)
def test_internationalised_top_level_domains_are_links(agent_text, expected_kinds):
    assert find_unsafe_spans(agent_text) == expected_kinds


@pytest.mark.parametrize(
    ("agent_text", "expected_kinds"),
    [
        ("Visit evil\ufe0f.com now", [UnsafeTextKind.LINK]),
        ("Call 555\ufe0f123\ufe0f4567", [UnsafeTextKind.LONG_NUMBER]),
        ("555\u3164123\u31644567", [UnsafeTextKind.LONG_NUMBER]),
        ("555\u034f123\u034f4567", [UnsafeTextKind.LONG_NUMBER]),
        ("555\u115f123\u115f4567", [UnsafeTextKind.LONG_NUMBER]),
    ],
)
def test_invisible_characters_never_split_a_span(agent_text, expected_kinds):
    assert find_unsafe_spans(agent_text) == expected_kinds


@pytest.mark.parametrize("agent_text", ["evil\u3002com", "evil\uff61com"])
def test_full_width_full_stop_reads_as_a_dot(agent_text):
    assert find_unsafe_spans(agent_text) == [UnsafeTextKind.LINK]


@pytest.mark.parametrize(
    "agent_text",
    ["Call \u2776\u2777\u2778-\u2779\u277a\u277b-\u277c\u277d\u277e\u24ff", "Call 555\u2022123\u20224567"],
)
def test_enclosed_and_bulleted_digits_are_numbers(agent_text):
    assert find_unsafe_spans(agent_text) == [UnsafeTextKind.LONG_NUMBER]


def test_compatibility_jamo_survive_the_summary():
    assert sanitize_summary("ㅋㅋ 확인했어요") == "ㅋㅋ 확인했어요"


@pytest.mark.parametrize(
    ("agent_text", "expected_kinds"),
    [
        ("Sent on 10/24/26", []),
        ("Sent on 24.10.26", []),
        ("It asked for $2,500.00", []),
        ("500,000원을 보내라고 했어요", []),
        ("₩500,000 was asked", []),
        ("Call $555,123,4567", [UnsafeTextKind.LONG_NUMBER]),
        ("Call $5551234567", [UnsafeTextKind.LONG_NUMBER]),
    ],
)
def test_amounts_and_short_dates_pass_the_gate(agent_text, expected_kinds):
    assert find_unsafe_spans(agent_text) == expected_kinds
