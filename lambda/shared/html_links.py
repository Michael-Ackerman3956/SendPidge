"""Read one HTML document the way a mail client shows it: its links, its images and its visible characters.

Parsing only: nothing is fetched, opened or rendered.
"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser

from shared.enums import LinkSource

LINK_HREF_MAX_CHARACTERS = 2048
MAXIMUM_OPEN_ELEMENTS = 256  # deeper start tags are not tracked: they inherit nothing new and their end tags cost nothing
SKIPPED_TEXT_TAGS = frozenset({"script", "style", "head", "title"})
VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})
CSS_COMMENT_PATTERN = re.compile(r"/\*.*?\*/", re.DOTALL)
CSS_ESCAPE_PATTERN = re.compile(r"\\(?:([0-9a-fA-F]{1,6})\s?|(.))", re.DOTALL)
CSS_NUMBER_PATTERN = re.compile(r"([+-]?[0-9]*\.?[0-9]+)([a-z%]*)")
CSS_IMPORTANT_SUFFIX = "!important"
UNICODE_REPLACEMENT_CHARACTER = "\ufffd"
LARGEST_CODE_POINT = 0x10FFFF
SURROGATE_CODE_POINTS = range(0xD800, 0xE000)
FORMAT_CHARACTER_CATEGORY = "Cf"  # zero-width space, soft hyphen, joiners, bidi marks: drawn as nothing
OPACITY_HIDING_THRESHOLD = 0.1
FONT_SIZE_HIDING_THRESHOLD_PIXELS = 1.0
PIXELS_PER_FONT_SIZE_UNIT = {"": 1.0, "px": 1.0, "pt": 4 / 3, "pc": 16.0, "in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4, "q": 96 / 101.6}
HIDING_KEYWORDS_BY_PROPERTY = {
    "display": frozenset({"none"}), "visibility": frozenset({"hidden", "collapse"}), "mso-hide": frozenset({"all"}),
}


def collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def visible_text_of(text_parts: list[str]) -> str:
    """Parts join with nothing between them (as a browser renders `zel<b></b>le`), format characters drop, whitespace collapses."""
    joined_text = "".join(text_parts)
    drawn_text = "".join(character for character in joined_text if unicodedata.category(character) != FORMAT_CHARACTER_CATEGORY)
    return collapse_whitespace(drawn_text)


def replace_css_escape(escape_match: re.Match[str]) -> str:
    hex_digits, escaped_character = escape_match.group(1), escape_match.group(2)
    if hex_digits is None:
        return escaped_character
    code_point = int(hex_digits, 16)
    is_valid_code_point = 0 < code_point <= LARGEST_CODE_POINT and code_point not in SURROGATE_CODE_POINTS
    return chr(code_point) if is_valid_code_point else UNICODE_REPLACEMENT_CHARACTER


def css_declarations_of(style: str) -> list[tuple[str, str]]:
    """(property, value) pairs with comments removed, escapes unescaped, whitespace removed, lower-cased, !important dropped."""
    unescaped_style = CSS_ESCAPE_PATTERN.sub(replace_css_escape, CSS_COMMENT_PATTERN.sub("", style))
    normalized_style = "".join(unescaped_style.split()).lower()
    declarations = []
    for declaration in normalized_style.split(";"):
        property_name, _, value = declaration.partition(":")
        declarations.append((property_name, value.removesuffix(CSS_IMPORTANT_SUFFIX)))
    return declarations


def is_opacity_hiding(value: str) -> bool:
    number_match = CSS_NUMBER_PATTERN.fullmatch(value)
    if number_match is None or number_match.group(2) not in ("", "%"):
        return False
    opacity = float(number_match.group(1)) / (100 if number_match.group(2) == "%" else 1)
    return opacity < OPACITY_HIDING_THRESHOLD  # a negative opacity clamps to 0


def is_font_size_hiding(value: str) -> bool:
    number_match = CSS_NUMBER_PATTERN.fullmatch(value)
    if number_match is None:
        return False
    size, unit = float(number_match.group(1)), number_match.group(2)
    if size < 0:
        return False  # an invalid declaration: the browser ignores it
    if unit not in PIXELS_PER_FONT_SIZE_UNIT:
        return size == 0  # a relative unit (em, %, ...): only zero is surely below 1px
    return size * PIXELS_PER_FONT_SIZE_UNIT[unit] < FONT_SIZE_HIDING_THRESHOLD_PIXELS


def is_declaration_hiding(property_name: str, value: str) -> bool:
    if property_name in HIDING_KEYWORDS_BY_PROPERTY:
        return value in HIDING_KEYWORDS_BY_PROPERTY[property_name]
    if property_name == "opacity":
        return is_opacity_hiding(value)
    return property_name == "font-size" and is_font_size_hiding(value)


def is_hidden_by_style(style: str) -> bool:
    """display:none, visibility:hidden/collapse, mso-hide:all, opacity below 0.1 or font-size below 1px."""
    return any(is_declaration_hiding(property_name, value) for property_name, value in css_declarations_of(style))


def is_element_hidden(attributes: dict[str, str]) -> bool:
    return "hidden" in attributes or ("style" in attributes and is_hidden_by_style(attributes["style"]))


def first_value_of_each_attribute(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    """A browser keeps the first of a repeated attribute; a valueless attribute reads as ''."""
    attributes: dict[str, str] = {}
    for name, value in attrs:
        attributes.setdefault(name, value or "")
    return attributes


def href_of_anchor(attributes: dict[str, str]) -> str | None:
    """href, else the SVG xlink:href; None when the anchor has neither."""
    if "href" in attributes:
        return attributes["href"]
    return attributes["xlink:href"] if "xlink:href" in attributes else None


@dataclass
class FoundLink:
    """A link as the parser met it, before its share of the body is known."""

    href: str
    source: LinkSource
    is_css_hidden: bool
    text_parts: list[str] = field(default_factory=list)  # visible data only
    image_count: int = 0

    @property
    def full_text(self) -> str:
        return visible_text_of(self.text_parts)


class LinkExtractor(HTMLParser):
    """Collects <a href> and <area href> with their visible text, images and hiding, up to a capacity, and the document's
    visible text: data outside script/style/head/title whose innermost open element is not hidden.

    An anchor left open at the end of the HTML is kept (close() flushes it); a link past the capacity sets is_truncated.
    """

    def __init__(self, anchor_source: LinkSource, capacity: int) -> None:
        super().__init__()
        self.anchor_source = anchor_source
        self.capacity = capacity
        self.found_links: list[FoundLink] = []
        self.is_truncated = False
        self.image_count = 0
        self.visible_parts: list[str] = []
        self.open_anchor: FoundLink | None = None
        self.element_stack: list[tuple[str, bool]] = []  # (tag, hidden by its own or an ancestor's style or hidden attribute)
        self.open_element_counts: Counter[str] = Counter()
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = first_value_of_each_attribute(attrs)
        is_hidden = self.is_inside_hidden_element() or is_element_hidden(attributes)
        if tag in SKIPPED_TEXT_TAGS:
            self.skip_depth += 1
        if tag == "a":
            self.finish_open_anchor()
            self.open_anchor_if_it_has_an_href(href_of_anchor(attributes), is_hidden)
        elif tag == "area":
            self.record_area(attributes, is_hidden)
        elif tag == "img":
            self.image_count += 1
            if self.open_anchor is not None:
                self.open_anchor.image_count += 1
        if tag not in VOID_TAGS:
            self.push_open_element(tag, is_hidden)

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TEXT_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1
        if tag == "a":
            self.finish_open_anchor()
        self.pop_to_innermost_open(tag)

    def handle_data(self, data: str) -> None:
        if self.skip_depth > 0 or self.is_inside_hidden_element():
            return
        self.visible_parts.append(data)
        if self.open_anchor is not None:
            self.open_anchor.text_parts.append(data)

    def close(self) -> None:
        super().close()
        self.finish_open_anchor()

    def is_inside_hidden_element(self) -> bool:
        return bool(self.element_stack) and self.element_stack[-1][1]

    def push_open_element(self, tag: str, is_hidden: bool) -> None:
        if len(self.element_stack) < MAXIMUM_OPEN_ELEMENTS:
            self.element_stack.append((tag, is_hidden))
            self.open_element_counts[tag] += 1

    def pop_to_innermost_open(self, tag: str) -> None:
        """Closes the innermost open `tag` and everything opened inside it; an end tag with none open returns at once."""
        if self.open_element_counts[tag] == 0:
            return
        while self.element_stack:
            popped_tag, _ = self.element_stack.pop()
            self.open_element_counts[popped_tag] -= 1
            if popped_tag == tag:
                return

    def has_capacity_or_mark_truncated(self) -> bool:
        open_anchor_count = 0 if self.open_anchor is None else 1
        if len(self.found_links) + open_anchor_count < self.capacity:
            return True
        self.is_truncated = True
        return False

    def open_anchor_if_it_has_an_href(self, href: str | None, is_css_hidden: bool) -> None:
        if href and self.has_capacity_or_mark_truncated():
            self.open_anchor = FoundLink(href.strip()[:LINK_HREF_MAX_CHARACTERS], self.anchor_source, is_css_hidden)

    def record_area(self, attributes: dict[str, str], is_css_hidden: bool) -> None:
        href = attributes["href"] if "href" in attributes else None
        if href and self.has_capacity_or_mark_truncated():
            area = FoundLink(href.strip()[:LINK_HREF_MAX_CHARACTERS], LinkSource.IMAGE_MAP, is_css_hidden)
            if "alt" in attributes and attributes["alt"]:  # no alt (or an empty one): the area shows no text
                area.text_parts.append(attributes["alt"])
            self.found_links.append(area)

    def finish_open_anchor(self) -> None:
        if self.open_anchor is not None:
            self.found_links.append(self.open_anchor)
            self.open_anchor = None


@dataclass(frozen=True)
class ExtractedHtmlDocument:
    found_links: tuple[FoundLink, ...]
    is_truncated: bool
    image_count: int
    visible_characters: int


def extract_links_from_html(html_text: str, anchor_source: LinkSource, capacity: int) -> ExtractedHtmlDocument:
    extractor = LinkExtractor(anchor_source, capacity)
    extractor.feed(html_text)
    extractor.close()
    visible_characters = len(visible_text_of(extractor.visible_parts))
    return ExtractedHtmlDocument(tuple(extractor.found_links), extractor.is_truncated, extractor.image_count, visible_characters)
