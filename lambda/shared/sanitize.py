"""An allow-list first: Pidge's own text may hold only characters a person needs to read it (ASCII letters and digits,
Latin letters, Hangul syllables, space, newline and the punctuation in ALLOWED_PUNCTUATION_AND_SYMBOLS). Agent text bound
for a person (case notes, chat replies) is refused, never stripped, before it is written to DynamoDB when it holds any
other character, a link, an address, a messenger handle or a long number; the Reader's summary has all of those stripped
instead. Both run on a normalised copy (NFKC except Hangul compatibility jamo, combining marks after ASCII dropped, every
default-ignorable character dropped, enclosed and other whole-number symbols read as digits, ideographic full stops read
as ".", whitespace collapsed and a space before punctuation removed), so fullwidth, enclosed, underlined and invisibly or
space-split spans are caught. A bare domain's TLD is an internationalised TLD, an xn-- label or any letter run but
Hangul. A summary whose own output still holds a span is replaced by SUMMARY_FALLBACK_TEXT."""

import datetime
import logging
import re
import unicodedata

from shared.agent_enums import UnsafeTextKind
from shared.idn_tlds import INTERNATIONALISED_TOP_LEVEL_DOMAINS
from shared.metrics import emit_count_metric

logger = logging.getLogger(__name__)

SUMMARY_MAX_LENGTH = 140
SUMMARY_FALLBACK_TEXT = "An email asked for something. Pidge is checking it."  # no link, address or number
SUMMARY_FALLBACK_METRIC_NAME = "SummaryFallback"
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
COMBINING_MARK_CATEGORIES = frozenset({"Mn", "Me"})  # underline, strike-through, accents, the keycap "\u20e3"
MARK_ANCHOR_PUNCTUATION = frozenset("./:@-")  # with ASCII letters and digits: a mark after one of these is dropped

# The allow-list, checked on normalize_for_gate output (so "…" has already become "..." and fullwidth forms are ASCII).
ALLOWED_PUNCTUATION_AND_SYMBOLS = frozenset(" \n.,!?'\"()-;:’‘“”–—…%&$€£₩¥")
LATIN_LETTER_CODE_POINT_RANGES = (range(0x00C0, 0x0250), range(0x1E00, 0x1F00))  # letters only, so "×" and "÷" are out
HANGUL_SYLLABLE_CODE_POINTS = range(0xAC00, 0xD7A4)  # syllables only: compatibility ("ㅡ") and conjoining jamo are out
LETTER_CATEGORY_PREFIX = "L"

# Each pattern stops before trailing punctuation so the sentence keeps its full stop.
TRAILING_PUNCTUATION_LOOKAHEAD = r"(?=[.,;:!?)]*(?:\s|$))"
INTERNATIONALISED_TOP_LEVEL_DOMAIN_ALTERNATION = "|".join(
    re.escape(domain) for domain in sorted(INTERNATIONALISED_TOP_LEVEL_DOMAINS, key=lambda domain: (-len(domain), domain))
)
# A bare domain's TLD is an internationalised TLD ("рф", "한국"), an xn-- label or a run of letters in any script but
# Hangul syllables ("evil.ćom", "evil.соm"); a Hangul run would make "확인했어요.감사합니다" a link. It ends where the
# next character is not an ASCII letter or digit, so quotes, brackets, "'s", dashes, "_" and Hangul after it
# ("evil.com에서") do not hide it; the path is optional.
URL_PATTERN = re.compile(
    r"(?:https?://|www\.)\S*?" + TRAILING_PUNCTUATION_LOOKAHEAD
    + r"|\b[\w-]+(?:\.[\w-]+)*\.(?:" + INTERNATIONALISED_TOP_LEVEL_DOMAIN_ALTERNATION
    + r"|xn--[a-z0-9-]+|[^\W\d_가-힣]{2,})"
    + r"(?![A-Za-z0-9])(?:/\S*?" + TRAILING_PUNCTUATION_LOOKAHEAD + ")?",
    re.IGNORECASE,
)
EMAIL_ADDRESS_PATTERN = re.compile(r"[^\s@]+@\S*?" + TRAILING_PUNCTUATION_LOOKAHEAD)
MESSENGER_HANDLE_PATTERN = re.compile(r"(?<![\w.@])@[A-Za-z0-9_][A-Za-z0-9_.]{2,}")  # "@refund_desk"
NUMBER_PATTERN = re.compile(r"[+(]?\d(?:[\s,.\-()/]*\d)*\)?")  # phone numbers, amounts, codes, any digit run
# 6+ digits joined by any run of non-word characters (":" and "_" join too), or by one "x"/"X" between groups of 3+
# digits ("010x1234x5678"). A letter breaks the run. Dates, amounts and lone clock times are blanked first. Joiners
# never match a digit, so the run is linear.
LONG_NUMBER_PATTERN = re.compile(r"(?<!\d)\d(?:(?:[\W_]*|(?<=\d{3})[xX](?=\d{3}))\d){5,}")
# A digit cluster (digits joined by non-word characters, linear for the same reason) is blanked only when the whole
# cluster is one real clock time ("10:30": hours 0-23, minutes 00-59) or a dash-joined range of two ("9:00-16:00"), so
# ":" still joins every other digit group ("010:1234:5678", "01:02 03:04 05:06").
CLOCK_TIME = r"(?:[01]?\d|2[0-3]):[0-5]\d"
DIGIT_CLUSTER_PATTERN = re.compile(r"\d(?:[\W_]*\d)*")
LONE_CLOCK_TIME_PATTERN = re.compile(CLOCK_TIME + r"(?:\s?[-–—]\s?" + CLOCK_TIME + ")?")
# "@" followed by more "@" or spaces before a handle ("@@refund_desk", "@ refund_desk") reads as one "@" in a summary.
SPACED_HANDLE_SIGN_PATTERN = re.compile(r"@[@\s]*(?=[A-Za-z0-9_])")
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


def is_mark_anchor(character: str) -> bool:
    return character.isascii() and (character.isalnum() or character in MARK_ANCHOR_PUNCTUATION)


def drop_marks_after_ascii(text: str) -> str:
    """Drops every Mn or Me character that follows an ASCII letter, digit or one of ". / : @ -", directly or after other
    dropped marks (an underlined or struck-through "evil.com" -> "evil.com"). Marks after any other character stay here
    (Thai, Devanagari); the allow-list then refuses them."""
    kept_characters = []
    is_after_anchor = False
    for character in text:
        if unicodedata.category(character) in COMBINING_MARK_CATEGORIES:
            if is_after_anchor:
                continue
        else:
            is_after_anchor = is_mark_anchor(character)
        kept_characters.append(character)
    return "".join(kept_characters)


def normalize_for_gate(text: str) -> str:
    """normalize_for_matching with combining marks after ASCII dropped (before NFKC, which would compose "c" and an acute
    into one letter, and again after it), then whitespace collapsed and a space before punctuation removed ("evil .com"
    -> "evil.com"). Only the gate and the summary use it; hosts keep normalize_for_matching, so a marked host stays IDN."""
    unmarked_text = drop_marks_after_ascii(normalize_for_matching(drop_marks_after_ascii(text)))
    return SPACE_BEFORE_PUNCTUATION_PATTERN.sub(r"\1", WHITESPACE_PATTERN.sub(" ", unmarked_text))


def is_allowed_character(character: str) -> bool:
    """True for ASCII letters and digits, ALLOWED_PUNCTUATION_AND_SYMBOLS, Hangul syllables and the letters of the two
    Latin ranges; every other character is outside the allow-list."""
    if (character.isascii() and character.isalnum()) or character in ALLOWED_PUNCTUATION_AND_SYMBOLS:
        return True
    code_point = ord(character)
    if code_point in HANGUL_SYLLABLE_CODE_POINTS:
        return True
    is_in_latin_range = any(code_point in code_point_range for code_point_range in LATIN_LETTER_CODE_POINT_RANGES)
    return is_in_latin_range and unicodedata.category(character).startswith(LETTER_CATEGORY_PREFIX)


def has_disallowed_character(text: str) -> bool:
    return not all(is_allowed_character(character) for character in text)


def replace_disallowed_characters(text: str) -> str:
    """Every character outside the allow-list becomes a space ("evil·com" -> "evil com")."""
    return "".join(character if is_allowed_character(character) else " " for character in text)


def blank_lone_clock_time(match: re.Match) -> str:
    return " " if LONE_CLOCK_TIME_PATTERN.fullmatch(match.group(0)) else match.group(0)


def blank_clock_times(text: str) -> str:
    """Blanks every digit cluster that is one clock time or one range of two; run after dates and amounts, whose tokens
    break clusters."""
    return DIGIT_CLUSTER_PATTERN.sub(blank_lone_clock_time, text)


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
    """Replaces every link, email address and messenger handle with a space."""
    return MESSENGER_HANDLE_PATTERN.sub(" ", EMAIL_ADDRESS_PATTERN.sub(" ", URL_PATTERN.sub(" ", text)))


def sanitize_summary(summary: str) -> str:
    """Strips links, addresses and handles from the gate-normalised summary (whitespace is collapsed, "evil .com" joined
    and "@@x" or "@ x" read as "@x" before any strip), replaces every character outside the allow-list with a space,
    strips every number, then links and addresses again (a stripped digit can free a link: "evil.com1"), collapses
    whitespace and caps the length. If the gate still finds a span in that output (a braille blank before ".com" leaves
    "evil .com"), or a non-blank summary was stripped to nothing (all Thai, Cyrillic or emoji), logs why, emits one
    SummaryFallback count and returns SUMMARY_FALLBACK_TEXT."""
    text = SPACED_HANDLE_SIGN_PATTERN.sub("@", normalize_for_gate(summary))
    text = strip_links_and_addresses(text)
    text = replace_disallowed_characters(text)
    text = NUMBER_PATTERN.sub(" ", text)
    text = strip_links_and_addresses(text)
    capped_summary = WHITESPACE_PATTERN.sub(" ", text).strip()[:SUMMARY_MAX_LENGTH].rstrip()
    if capped_summary == "" and summary.strip() != "":
        emit_count_metric(SUMMARY_FALLBACK_METRIC_NAME)
        logger.warning("summary fell back: nothing readable left")
        return SUMMARY_FALLBACK_TEXT
    unsafe_kinds = find_unsafe_spans(capped_summary)
    if not unsafe_kinds:
        return capped_summary
    emit_count_metric(SUMMARY_FALLBACK_METRIC_NAME)
    logger.warning("summary fell back: kinds=%s", [str(kind) for kind in unsafe_kinds])
    return SUMMARY_FALLBACK_TEXT


class AgentOutputRefused(Exception):
    """Agent text held an unsafe span. Carries the kinds only, never the text, so logging it leaks nothing."""

    def __init__(self, kinds: list[UnsafeTextKind]) -> None:
        super().__init__(f"agent text refused: {', '.join(str(kind) for kind in kinds)}")
        self.kinds = kinds


def find_unsafe_spans(text: str) -> list[UnsafeTextKind]:
    """Each kind found, once, in enum order, matched on the gate-normalised text. Addresses are blanked before handles
    and handles before links (both can hold a domain), so one span is one kind. On what is left (dates still in it), any
    character outside the allow-list is DISALLOWED_CHARACTER ("a@b.co" is an address only, "@@refund_desk" is a
    disallowed character); dates, amounts and lone clock times are then blanked before the long-number check."""
    found_kinds: set[UnsafeTextKind] = set()
    remaining_text = normalize_for_gate(text)
    for kind, pattern in ((UnsafeTextKind.EMAIL_ADDRESS, EMAIL_ADDRESS_PATTERN),
                          (UnsafeTextKind.MESSENGER_HANDLE, MESSENGER_HANDLE_PATTERN), (UnsafeTextKind.LINK, URL_PATTERN)):
        if pattern.search(remaining_text):
            found_kinds.add(kind)
            remaining_text = pattern.sub(" ", remaining_text)
    if has_disallowed_character(remaining_text):
        found_kinds.add(UnsafeTextKind.DISALLOWED_CHARACTER)
    if LONG_NUMBER_PATTERN.search(blank_clock_times(blank_amounts(blank_dates(remaining_text)))):
        found_kinds.add(UnsafeTextKind.LONG_NUMBER)
    return [kind for kind in UnsafeTextKind if kind in found_kinds]


def refuse_unsafe_agent_text(text: str) -> None:
    """Returns when the text is clean. Otherwise emits one dimensionless AgentOutputRefused count for the refusal and one
    per kind found, logs the kinds (never the text) and raises AgentOutputRefused; the caller stores its own fixed text
    in its place, never the refused text."""
    unsafe_kinds = find_unsafe_spans(text)
    if not unsafe_kinds:
        return
    emit_count_metric(AGENT_OUTPUT_REFUSED_METRIC_NAME)
    for kind in unsafe_kinds:
        emit_count_metric(AGENT_OUTPUT_REFUSED_METRIC_NAME, UNSAFE_KIND_DIMENSION_NAME, str(kind))
    logger.warning("agent text refused: kinds=%s", [str(kind) for kind in unsafe_kinds])
    raise AgentOutputRefused(unsafe_kinds)
