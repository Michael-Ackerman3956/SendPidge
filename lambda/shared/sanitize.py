"""Makes the Reader's summary safe to put in front of a real person on any channel."""

import re

SUMMARY_MAX_LENGTH = 140

# Each pattern stops before trailing punctuation so the sentence keeps its full stop.
TRAILING_PUNCTUATION_LOOKAHEAD = r"(?=[.,;:!?)]*(?:\s|$))"
URL_PATTERN = re.compile(
    r"(?:https?://|www\.)\S*?" + TRAILING_PUNCTUATION_LOOKAHEAD
    + r"|\b[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?:/\S*?)?" + TRAILING_PUNCTUATION_LOOKAHEAD,
    re.IGNORECASE,
)
EMAIL_ADDRESS_PATTERN = re.compile(r"[^\s@]+@\S*?" + TRAILING_PUNCTUATION_LOOKAHEAD)
NUMBER_PATTERN = re.compile(r"[+(]?\d(?:[\s,.\-()/]*\d)*\)?")  # phone numbers, amounts, codes, any digit run
WHITESPACE_PATTERN = re.compile(r"\s+")
SPACE_BEFORE_PUNCTUATION_PATTERN = re.compile(r"\s+([,.;:!?])")


def sanitize_summary(summary: str) -> str:
    """Strips links, addresses and every number, then caps the length."""
    text = URL_PATTERN.sub(" ", summary)
    text = EMAIL_ADDRESS_PATTERN.sub(" ", text)
    text = NUMBER_PATTERN.sub(" ", text)
    text = WHITESPACE_PATTERN.sub(" ", text).strip()
    text = SPACE_BEFORE_PUNCTUATION_PATTERN.sub(r"\1", text)
    return text[:SUMMARY_MAX_LENGTH].rstrip()
