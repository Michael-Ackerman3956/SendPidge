"""Makes the Reader's summary safe to put in front of a real person on any channel, and gates agent text (case notes,
chat replies) before it is written to DynamoDB: agent text with a link, an address or a long number is refused, never
stripped. Both run on a normalised copy (NFKC except Hangul compatibility jamo, every default-ignorable character dropped,
enclosed and other whole-number symbols read as digits, ideographic full stops read as "."), so fullwidth, enclosed and
invisibly split spans are caught. Bare domains end at an ASCII boundary and include internationalised TLDs."""

import datetime
import logging
import re
import unicodedata

from shared.enums import UnsafeTextKind
from shared.idn_tlds import INTERNATIONALISED_TOP_LEVEL_DOMAINS
from shared.metrics import emit_count_metric

logger = logging.getLogger(__name__)

SUMMARY_MAX_LENGTH = 140
AGENT_OUTPUT_REFUSED_METRIC_NAME = "AgentOutputRefused"
UNSAFE_KIND_DIMENSION_NAME = "kind"
FORMAT_CHARACTER_CATEGORY = "Cf"  # zero-width space and joiners, soft hyphen, direction marks
# Default_Ignorable_Code_Point characters outside category Cf: grapheme joiner, Hangul fillers, Khmer inherent vowels,
# variation selectors (Mongolian, standard, supplement) and the reserved ignorable ranges.
DEFAULT_IGNORABLE_NON_FORMAT_PATTERN = re.compile(
    r"[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u3164\ufe00-\ufe0f\uffa0\ufff0-\ufff8\U000e0000-\U000e0fff]"
)
# Everything except Hangul compatibility jamo ("ㅋㅋ"), which NFKC would turn into conjoining jamo.
NON_COMPATIBILITY_JAMO_RUN_PATTERN = re.compile(r"[^\u3131-\u318e]+")
OTHER_NUMBER_CATEGORY = "No"  # enclosed and dingbat digits such as "❶" and "⓿"
IDEOGRAPHIC_FULL_STOP = "\u3002"  # NFKC already folds the halfwidth "\uff61" into this one

# Each pattern stops before trailing punctuation so the sentence keeps its full stop.
TRAILING_PUNCTUATION_LOOKAHEAD = r"(?=[.,;:!?)]*(?:\s|$))"
INTERNATIONALISED_TOP_LEVEL_DOMAIN_ALTERNATION = "|".join(
    re.escape(domain) for domain in sorted(INTERNATIONALISED_TOP_LEVEL_DOMAINS, key=lambda domain: (-len(domain), domain))
)
# A bare domain's TLD is ASCII letters, an xn-- label or an internationalised TLD ("рф", "한국"), never any
# Unicode word. It ends where the next character is not an ASCII letter or digit, so quotes, brackets, "'s", dashes,
# "_" and non-ASCII letters after it ("evil.com에서") do not hide it; the path is optional.
URL_PATTERN = re.compile(
    r"(?:https?://|www\.)\S*?" + TRAILING_PUNCTUATION_LOOKAHEAD
    + r"|\b[\w-]+(?:\.[\w-]+)*\.(?:" + INTERNATIONALISED_TOP_LEVEL_DOMAIN_ALTERNATION + r"|xn--[a-z0-9-]+|[a-z]{2,})"
    + r"(?![A-Za-z0-9])(?:/\S*?" + TRAILING_PUNCTUATION_LOOKAHEAD + ")?",
    re.IGNORECASE,
)
EMAIL_ADDRESS_PATTERN = re.compile(r"[^\s@]+@\S*?" + TRAILING_PUNCTUATION_LOOKAHEAD)
NUMBER_PATTERN = re.compile(r"[+(]?\d(?:[\s,.\-()/]*\d)*\)?")  # phone numbers, amounts, codes, any digit run
# 6+ digits joined by whitespace, . ( ) + / _ * ~, a middle dot or bullet (\u00b7 \u2022 \u2219 \u22c5 \u30fb \u2027) or any
# dash, or by a comma with a digit on both sides ("1,500,000"; "October 24, 2026" has a space after the comma). ":" never
# joins ("at 10:30"). Dates and amounts are blanked first.
LONG_NUMBER_PATTERN = re.compile(
    r"(?<!\d)\d(?:(?:,|[\s.()+/_*~\u00b7\u2022\u2219\u22c5\u30fb\u2027\-\u2010-\u2015\u2212]*)\d){5,}"
)
WHITESPACE_PATTERN = re.compile(r"\s+")
SPACE_BEFORE_PUNCTUATION_PATTERN = re.compile(r"\s+([,.;:!?])")

# Dates are replaced by a word, never a space: a space is a joiner and would glue the digits on either side together.
DATE_TOKEN = " date "
YEAR = r"(?P<year>(?:19|20)\d\d)"
MONTH = r"(?P<month>\d{1,2})"
DAY = r"(?P<day>\d{1,2})"
FOUR_OR_TWO_DIGIT_YEAR = r"(?P<year>(?:19|20)\d\d|\d\d)"  # a two-digit year reads as 20yy
ISO_DATE_PATTERN = re.compile(r"(?<![\d-])" + YEAR + r"-(?P<month>0[1-9]|1[0-2])-(?P<day>0[1-9]|[12]\d|3[01])(?![\d-])")
# 2026.10.24, "2026. 10. 24." (Korean style) and 2026/10/24; the trailing dot is left in place.
YEAR_FIRST_DATE_PATTERN = re.compile(
    r"(?<!\d)(?<!\d[./])(?<!\d\.\s)" + YEAR + r"(?P<separator>\.\s?|/)" + MONTH + r"(?P=separator)" + DAY
    + r"(?!\d)(?![./]\s?\d)"
)
# 24.10.2026 and 24.10.26 are day first; 10/24/2026, 10/24/26 and 24/10/2026 pass when either order is a real date.
YEAR_LAST_DATE_PATTERN = re.compile(
    r"(?<!\d)(?<!\d[./])(?P<first>\d{1,2})(?P<separator>[./])(?P<second>\d{1,2})(?P=separator)" + FOUR_OR_TWO_DIGIT_YEAR
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

# Amounts are replaced by a word, like dates. Only a comma-grouped number counts, with a currency symbol before it or a
# currency word after it; "$555,123,4567" and "$5551234567" are not amounts and stay long numbers.
AMOUNT_TOKEN = " amount "
GROUPED_NUMBER = r"(?<![\d,.])\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?(?![,.]?\d)"
CURRENCY_WORD = r"(?:원|won|dollars?|usd|krw|eur|euros?|gbp|pounds?|yen|jpy)(?![A-Za-z0-9])"
AMOUNT_PATTERN = re.compile(
    r"(?<![\d,.])[$€£¥₩]\s?" + GROUPED_NUMBER + "|" + GROUPED_NUMBER + r"\s?" + CURRENCY_WORD,
    re.IGNORECASE,
)


def fold_compatibility_run(match: re.Match) -> str:
    return unicodedata.normalize("NFKC", match.group(0))


def spell_whole_number_symbol(character: str) -> str:
    """The decimal digits of a category "No" character whose numeric value is a whole number ("❿" -> "10"), else the
    character unchanged."""
    if unicodedata.category(character) != OTHER_NUMBER_CATEGORY:
        return character
    try:
        numeric_value = unicodedata.numeric(character)
    except ValueError:  # a "No" character without a numeric value is not a number
        return character
    return str(int(numeric_value)) if numeric_value.is_integer() and numeric_value >= 0 else character


def normalize_for_matching(text: str) -> str:
    """NFKC everywhere but Hangul compatibility jamo (fullwidth letters, digits, "@" and "." become ASCII), every
    default-ignorable character dropped, whole-number "No" symbols spelled as digits, "\u3002" read as "."."""
    folded_text = NON_COMPATIBILITY_JAMO_RUN_PATTERN.sub(fold_compatibility_run, text)
    visible_text = DEFAULT_IGNORABLE_NON_FORMAT_PATTERN.sub("", "".join(
        character for character in folded_text if unicodedata.category(character) != FORMAT_CHARACTER_CATEGORY))
    digit_text = "".join(spell_whole_number_symbol(character) for character in visible_text)
    return digit_text.replace(IDEOGRAPHIC_FULL_STOP, ".")


def is_real_date(year: str, month: str, day: str) -> bool:
    try:
        datetime.date(int(year), int(month), int(day))
    except ValueError:
        return False
    return True


def expand_two_digit_year(year: str) -> str:
    return str(2000 + int(year)) if len(year) == 2 else year


def blank_numeric_date(match: re.Match) -> str:
    """The date token when the match is a real calendar date, else the match unchanged."""
    groups = match.groupdict()
    year = expand_two_digit_year(groups["year"])
    if "first" not in groups:
        is_date = is_real_date(year, groups["month"], groups["day"])
    elif groups["separator"] == ".":
        is_date = is_real_date(year, groups["second"], groups["first"])
    else:
        is_date = (is_real_date(year, groups["first"], groups["second"])
                   or is_real_date(year, groups["second"], groups["first"]))
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


def blank_amounts(text: str) -> str:
    """Replaces every comma-grouped currency amount with the amount token so its digits never count toward a long number."""
    return AMOUNT_PATTERN.sub(AMOUNT_TOKEN, text)


def strip_links_and_addresses(text: str) -> str:
    return EMAIL_ADDRESS_PATTERN.sub(" ", URL_PATTERN.sub(" ", text))


def sanitize_summary(summary: str) -> str:
    """Strips links, addresses and every number from the normalised summary, then links and addresses again (a stripped
    digit can free a link: "evil.com1"), then caps the length."""
    text = strip_links_and_addresses(normalize_for_matching(summary))
    text = NUMBER_PATTERN.sub(" ", text)
    text = strip_links_and_addresses(text)
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
    address holds a domain), links before numbers and dates and amounts before the long-number check, so one span is one
    kind."""
    found_kinds: set[UnsafeTextKind] = set()
    remaining_text = normalize_for_matching(text)
    for kind, pattern in ((UnsafeTextKind.EMAIL_ADDRESS, EMAIL_ADDRESS_PATTERN), (UnsafeTextKind.LINK, URL_PATTERN)):
        if pattern.search(remaining_text):
            found_kinds.add(kind)
            remaining_text = pattern.sub(" ", remaining_text)
    if LONG_NUMBER_PATTERN.search(blank_amounts(blank_dates(remaining_text))):
        found_kinds.add(UnsafeTextKind.LONG_NUMBER)
    return [kind for kind in UnsafeTextKind if kind in found_kinds]


def refuse_unsafe_agent_text(text: str) -> None:
    """Returns when the text is clean. Otherwise emits one AgentOutputRefused count per kind found, logs the kinds (never
    the text) and raises AgentOutputRefused; the caller stores its own fixed text in its place, never the refused text."""
    unsafe_kinds = find_unsafe_spans(text)
    if not unsafe_kinds:
        return
    for kind in unsafe_kinds:
        emit_count_metric(AGENT_OUTPUT_REFUSED_METRIC_NAME, UNSAFE_KIND_DIMENSION_NAME, str(kind))
    logger.warning("agent text refused: kinds=%s", [str(kind) for kind in unsafe_kinds])
    raise AgentOutputRefused(unsafe_kinds)
