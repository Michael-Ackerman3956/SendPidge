import pytest

from shared.sanitize import SUMMARY_MAX_LENGTH, sanitize_summary


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
