"""Makes the Reader's summary safe to put in front of a real person on any channel, and gates agent text (case notes,
chat replies) before it is written to DynamoDB: agent text with a link, an address or a long number is refused, never
stripped. Both run on a normalised copy (NFKC, format characters dropped, ideographic full stops read as "."), so
fullwidth and zero-width-split spans are caught."""

import datetime
import logging
import re
import unicodedata

from shared.enums import UnsafeTextKind
from shared.metrics import emit_count_metric

logger = logging.getLogger(__name__)

SUMMARY_MAX_LENGTH = 140
AGENT_OUTPUT_REFUSED_METRIC_NAME = "AgentOutputRefused"
UNSAFE_KIND_DIMENSION_NAME = "kind"
FORMAT_CHARACTER_CATEGORY = "Cf"  # zero-width space and joiners, soft hyphen, direction marks
IDEOGRAPHIC_FULL_STOP = "\u3002"  # NFKC already folds the halfwidth "\uff61" into this one

# Each pattern stops before trailing punctuation so the sentence keeps its full stop.
TRAILING_PUNCTUATION_LOOKAHEAD = r"(?=[.,;:!?)]*(?:\s|$))"
# A bare domain ends where its TLD ends (quotes, brackets, "'s" and dashes after it do not hide it); the path is optional.
URL_PATTERN = re.compile(
    r"(?:https?://|www\.)\S*?" + TRAILING_PUNCTUATION_LOOKAHEAD
    + r"|\b[\w-]+(?:\.[\w-]+)*\.(?:[a-z]{2,}|xn--[a-z0-9-]+)(?![\w-])(?:/\S*?" + TRAILING_PUNCTUATION_LOOKAHEAD + ")?",
    re.IGNORECASE,
)
EMAIL_ADDRESS_PATTERN = re.compile(r"[^\s@]+@\S*?" + TRAILING_PUNCTUATION_LOOKAHEAD)
NUMBER_PATTERN = re.compile(r"[+(]?\d(?:[\s,.\-()/]*\d)*\)?")  # phone numbers, amounts, codes, any digit run
# 6+ digits joined by whitespace, . ( ) + / _ * ~ \u00b7 or any dash, or by a comma with a digit on both sides
# ("1,500,000"; "October 24, 2026" has a space after the comma). ":" never joins ("at 10:30"). Dates are blanked first.
LONG_NUMBER_PATTERN = re.compile(r"(?<!\d)\d(?:(?:,|[\s.()+/_*~\u00b7\-\u2010-\u2015\u2212]*)\d){5,}")
WHITESPACE_PATTERN = re.compile(r"\s+")
SPACE_BEFORE_PUNCTUATION_PATTERN = re.compile(r"\s+([,.;:!?])")

# Dates are replaced by a word, never a space: a space is a joiner and would glue the digits on either side together.
DATE_TOKEN = " date "
YEAR = r"(?P<year>(?:19|20)\d\d)"
MONTH = r"(?P<month>\d{1,2})"
DAY = r"(?P<day>\d{1,2})"
ISO_DATE_PATTERN = re.compile(r"(?<![\d-])" + YEAR + r"-(?P<month>0[1-9]|1[0-2])-(?P<day>0[1-9]|[12]\d|3[01])(?![\d-])")
# 2026.10.24, "2026. 10. 24." (Korean style) and 2026/10/24; the trailing dot is left in place.
YEAR_FIRST_DATE_PATTERN = re.compile(
    r"(?<!\d)(?<!\d[./])(?<!\d\.\s)" + YEAR + r"(?P<separator>\.\s?|/)" + MONTH + r"(?P=separator)" + DAY
    + r"(?!\d)(?![./]\s?\d)"
)
# 24.10.2026 is day first; 10/24/2026 and 24/10/2026 pass when either order is a real date.
YEAR_LAST_DATE_PATTERN = re.compile(
    r"(?<!\d)(?<!\d[./])(?P<first>\d{1,2})(?P<separator>[./])(?P<second>\d{1,2})(?P=separator)" + YEAR
    + r"(?!\d)(?![./]\d)"
)
MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
               "november", "december")
MONTH_ABBREVIATIONS = tuple(name[:3] for name in MONTH_NAMES)
MONTH_NAME_ALTERNATION = "|".join(f"{name}|{abbreviation}" for name, abbreviation in zip(MONTH_NAMES, MONTH_ABBREVIATIONS))
MONTH_NAME_DATE_PATTERN = re.compile(
    r"\b(?P<month_name>" + MONTH_NAME_ALTERNATION + r")\.?\s+" + DAY + r"(?:st|nd|rd|th)?,?\s+" + YEAR + r"(?!\d)",
    re.IGNORECASE,
)


def normalize_for_matching(text: str) -> str:
    """NFKC (fullwidth letters, digits, "@" and "." become ASCII), every format character dropped, "\u3002" read as "."."""
    folded_text = unicodedata.normalize("NFKC", text)
    visible_text = "".join(character for character in folded_text
                           if unicodedata.category(character) != FORMAT_CHARACTER_CATEGORY)
    return visible_text.replace(IDEOGRAPHIC_FULL_STOP, ".")


def is_real_date(year: str, month: str, day: str) -> bool:
    try:
        datetime.date(int(year), int(month), int(day))
    except ValueError:
        return False
    return True


def blank_numeric_date(match: re.Match) -> str:
    """The date token when the match is a real calendar date, else the match unchanged."""
    groups = match.groupdict()
    if "first" not in groups:
        is_date = is_real_date(groups["year"], groups["month"], groups["day"])
    elif groups["separator"] == ".":
        is_date = is_real_date(groups["year"], groups["second"], groups["first"])
    else:
        is_date = (is_real_date(groups["year"], groups["first"], groups["second"])
                   or is_real_date(groups["year"], groups["second"], groups["first"]))
    return DATE_TOKEN if is_date else match.group(0)


def blank_month_name_date(match: re.Match) -> str:
    """The date token when "Month D YYYY" is a real calendar date, else the match unchanged."""
    month_number = MONTH_ABBREVIATIONS.index(match["month_name"].lower()[:3]) + 1
    return DATE_TOKEN if is_real_date(match["year"], str(month_number), match["day"]) else match.group(0)


def blank_dates(text: str) -> str:
    """Replaces every real calendar date with the date token so its digits never count toward a long number."""
    for pattern in (ISO_DATE_PATTERN, YEAR_FIRST_DATE_PATTERN, YEAR_LAST_DATE_PATTERN):
        text = pattern.sub(blank_numeric_date, text)
    return MONTH_NAME_DATE_PATTERN.sub(blank_month_name_date, text)


def sanitize_summary(summary: str) -> str:
    """Strips links, addresses and every number from the normalised summary, then caps the length."""
    text = URL_PATTERN.sub(" ", normalize_for_matching(summary))
    text = EMAIL_ADDRESS_PATTERN.sub(" ", text)
    text = NUMBER_PATTERN.sub(" ", text)
    text = WHITESPACE_PATTERN.sub(" ", text).strip()
    text = SPACE_BEFORE_PUNCTUATION_PATTERN.sub(r"\1", text)
    return text[:SUMMARY_MAX_LENGTH].rstrip()


class AgentOutputRefused(Exception):
    """Agent text held an unsafe span. Carries the kinds only, never the text, so logging it leaks nothing."""

    def __init__(self, kinds: list[UnsafeTextKind]) -> None:
        super().__init__(f"agent text refused: {', '.join(str(kind) for kind in kinds)}")
        self.kinds = kinds


def find_unsafe_spans(text: str) -> list[UnsafeTextKind]:
    """Each kind found, once, in enum order, matched on the normalised text. Addresses are blanked before links (an
    address holds a domain), links before numbers and dates before the long-number check, so one span is one kind."""
    found_kinds: set[UnsafeTextKind] = set()
    remaining_text = normalize_for_matching(text)
    for kind, pattern in ((UnsafeTextKind.EMAIL_ADDRESS, EMAIL_ADDRESS_PATTERN), (UnsafeTextKind.LINK, URL_PATTERN)):
        if pattern.search(remaining_text):
            found_kinds.add(kind)
            remaining_text = pattern.sub(" ", remaining_text)
    if LONG_NUMBER_PATTERN.search(blank_dates(remaining_text)):
        found_kinds.add(UnsafeTextKind.LONG_NUMBER)
    return [kind for kind in UnsafeTextKind if kind in found_kinds]


def refuse_unsafe_agent_text(text: str) -> None:
    """Returns when the text is clean. Otherwise emits one AgentOutputRefused count per kind found, logs the kinds (never
    the text) and raises AgentOutputRefused; the caller writes nothing to DynamoDB."""
    unsafe_kinds = find_unsafe_spans(text)
    if not unsafe_kinds:
        return
    for kind in unsafe_kinds:
        emit_count_metric(AGENT_OUTPUT_REFUSED_METRIC_NAME, UNSAFE_KIND_DIMENSION_NAME, str(kind))
    logger.warning("agent text refused: kinds=%s", [str(kind) for kind in unsafe_kinds])
    raise AgentOutputRefused(unsafe_kinds)
