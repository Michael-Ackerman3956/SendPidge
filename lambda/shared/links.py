"""Link reasons from parsing, plus LINK_LISTED from the scam-list check: what a link says versus where it goes, and
whether that place is listed. Reasons add a mark, never a hold."""

import re
from dataclasses import dataclass, replace

from shared.email_parse import EmailLink, ParsedEmail, registrable_domain_of_host
from shared.enums import HiddenBy, IndicatorKind, LinkKind, LinkSource, ReasonCode
from shared.lists import CheckResult, Indicator, indicator_key, is_lookalike_of

KNOWN_SHORTENER_HOSTS = frozenset({
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "cutt.ly", "rb.gy", "tiny.cc", "t.ly", "shorturl.at",
})
WHOLE_BODY_LINK_SHARE = 0.6  # an anchor whose text is this share of the HTML body or more makes most of the body one click
IMAGE_ONLY_BODY_MAXIMUM_VISIBLE_CHARACTERS = 40  # under this, an HTML body with images reads as a picture (QR or screenshot mail)
REASON_BY_LINK_KIND = {LinkKind.SCRIPT: ReasonCode.SCRIPT_HREF_LINK, LinkKind.DATA: ReasonCode.SCRIPT_HREF_LINK}
REASON_BY_LINK_SOURCE = {LinkSource.IMAGE_MAP: ReasonCode.IMAGE_MAP_LINK, LinkSource.HTML_ATTACHMENT: ReasonCode.HTML_ATTACHMENT_LINK}
# A visible text that reads like an address: one token, at least one dot, letters and digits only around it.
HOST_LIKE_TEXT = re.compile(r"^(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)(?:/\S*)?$", re.IGNORECASE)


@dataclass(frozen=True)
class LinkAssessment:
    link: EmailLink
    reasons: tuple[ReasonCode, ...]
    listed_by: str | None = None  # the attribution of the list entry behind LINK_LISTED; None when the link is not listed

    @property
    def is_flagged(self) -> bool:
        return bool(self.reasons)

    def as_row(self) -> dict:
        """The shape stored on EMAIL# and sent to the PWA; the PWA renders it as text only."""
        return {
            "text": self.link.visible_text, "domain": describe_domain_for_display(self.link), "href": self.link.href,
            "flagged": self.is_flagged, "kind": str(self.link.kind), "hiddenBy": str(self.link.hidden_by),
            "reasons": [str(reason) for reason in self.reasons], "listedBy": self.listed_by,
        }


def describe_domain_for_display(link: EmailLink) -> str:
    """An internationalised domain in its ASCII (xn--) form, as a browser's address bar shows a mixed-script lookalike, so
    'аррӏе.com' never reads as 'apple.com' on screen. The decoded form stays in registrable_domain for the lookalike check."""
    if not link.has_internationalised_host:
        return link.registrable_domain
    try:
        return link.registrable_domain.encode("idna").decode("ascii")
    except UnicodeError:
        return link.registrable_domain.encode("ascii", "backslashreplace").decode("ascii")  # unreadable, but never a lookalike


def domain_named_in_text(visible_text: str) -> str:
    match = HOST_LIKE_TEXT.match(visible_text.strip())
    return registrable_domain_of_host(match.group(1)) if match else ""


def assess_link(link: EmailLink, family_domains: frozenset[str]) -> LinkAssessment:
    reasons: list[ReasonCode] = list(camouflage_reasons(link))
    named_domain = domain_named_in_text(link.visible_text)
    if named_domain and link.registrable_domain and named_domain != link.registrable_domain:
        reasons.append(ReasonCode.LINK_TEXT_HREF_MISMATCH)
    if link.registrable_domain in KNOWN_SHORTENER_HOSTS:
        reasons.append(ReasonCode.KNOWN_SHORTENER)
    if any(is_lookalike_of(link.registrable_domain, family_domain) for family_domain in family_domains):
        reasons.append(ReasonCode.LOOKALIKE_LINK_DOMAIN)
    if link.has_internationalised_host:
        reasons.append(ReasonCode.IDN_LINK_DOMAIN)
    return LinkAssessment(link, tuple(reasons))


def camouflage_reasons(link: EmailLink) -> list[ReasonCode]:
    """Reasons from how the link is dressed: no text, hidden by style, script href, image map, attachment, whole body."""
    reasons: list[ReasonCode] = []
    if link.is_hidden:
        reasons.append(ReasonCode.HIDDEN_LINK)
    if link.hidden_by is HiddenBy.CSS:
        reasons.append(ReasonCode.CSS_HIDDEN_LINK)
    if link.kind in REASON_BY_LINK_KIND:
        reasons.append(REASON_BY_LINK_KIND[link.kind])
    if link.source in REASON_BY_LINK_SOURCE:
        reasons.append(REASON_BY_LINK_SOURCE[link.source])
    if link.body_share >= WHOLE_BODY_LINK_SHARE:
        reasons.append(ReasonCode.WHOLE_BODY_LINK)
    return reasons


def assess_links(links: tuple[EmailLink, ...], family_domains: frozenset[str]) -> list[LinkAssessment]:
    return [assess_link(link, family_domains) for link in links]


def link_reasons(assessments: list[LinkAssessment], parsed_email: ParsedEmail) -> list[ReasonCode]:
    """Every link reason once, in first-seen order: the email-level ones first, then each link's."""
    reasons: list[ReasonCode] = _email_level_link_reasons(parsed_email)
    for assessment in assessments:
        for reason in assessment.reasons:
            if reason not in reasons:
                reasons.append(reason)
    return reasons


def _email_level_link_reasons(parsed_email: ParsedEmail) -> list[ReasonCode]:
    """An HTML document that would not decode, links past the cap, or a body that is only a picture."""
    reasons: list[ReasonCode] = []
    if parsed_email.links_unreadable:
        reasons.append(ReasonCode.LINKS_UNREADABLE)
    if parsed_email.links_truncated:
        reasons.append(ReasonCode.LINKS_TRUNCATED)
    if parsed_email.image_count > 0 and parsed_email.visible_html_characters < IMAGE_ONLY_BODY_MAXIMUM_VISIBLE_CHARACTERS:
        reasons.append(ReasonCode.IMAGE_ONLY_BODY)
    return reasons


def indicators_of_web_link(link: EmailLink) -> list[Indicator]:
    """A web link's domain (the ASCII form, as feeds list it) and its URL; none for mailto, tel, script, data or other links."""
    if link.kind is not LinkKind.WEB:
        return []
    return [Indicator(IndicatorKind.DOMAIN, describe_domain_for_display(link)), Indicator(IndicatorKind.URL, link.href)]


def sender_domain_indicators(sender_domain: str) -> list[Indicator]:
    """The sender's domain in its ASCII (xn--) form, so an IDN sender keys like the feeds' rows; none when it will not encode."""
    try:
        return [Indicator(IndicatorKind.DOMAIN, sender_domain.encode("idna").decode("ascii"))]
    except UnicodeError:  # not a domain name, so no LIST# row can be keyed on it
        return []


def is_keyable_indicator(indicator: Indicator) -> bool:
    """Whether lists.indicator_key accepts the value; a value it refuses keys no LIST# row, so skipping it misses nothing."""
    try:
        indicator_key(indicator.kind, indicator.value)
    except ValueError:  # an odd host, or a URL that is not a plain web URL
        return False
    return True


def list_indicators_of(assessments: list[LinkAssessment], sender_domain: str) -> list[Indicator]:
    """What the Reader asks lists.check about, once each and in order: the sender's domain, then each web link's domain and
    URL. Never raises (the Reader calls this before the claim): a value indicator_key refuses is skipped."""
    candidates = sender_domain_indicators(sender_domain)
    for assessment in assessments:
        candidates.extend(indicators_of_web_link(assessment.link))
    indicators: list[Indicator] = []
    for candidate in candidates:
        if candidate not in indicators and is_keyable_indicator(candidate):
            indicators.append(candidate)
    return indicators


def mark_listed_links(assessments: list[LinkAssessment], check_result: CheckResult) -> list[LinkAssessment]:
    """LINK_LISTED on every web link whose domain or URL is a hit (any strength), with the first such hit's attribution."""
    return [mark_listed_link(assessment, check_result) for assessment in assessments]


def mark_listed_link(assessment: LinkAssessment, check_result: CheckResult) -> LinkAssessment:
    link_indicators = indicators_of_web_link(assessment.link)
    first_hit = next((hit for hit in check_result.hits if hit.indicator in link_indicators), None)
    if first_hit is None:
        return assessment
    return replace(assessment, reasons=(*assessment.reasons, ReasonCode.LINK_LISTED), listed_by=first_hit.attribution)
