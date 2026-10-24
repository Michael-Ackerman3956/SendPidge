"""Link reasons from parsing alone: what a link says versus where it goes. Reasons add a mark, never a hold."""

import re
from dataclasses import dataclass

from shared.email_parse import EmailLink, registrable_domain_of_host
from shared.enums import ReasonCode

KNOWN_SHORTENER_HOSTS = frozenset({
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "cutt.ly", "rb.gy", "tiny.cc", "t.ly", "shorturl.at",
})
# A visible text that reads like an address: one token, at least one dot, letters and digits only around it.
HOST_LIKE_TEXT = re.compile(r"^(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)(?:/\S*)?$", re.IGNORECASE)
CONFUSABLE_CHARACTERS = str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s", "7": "t", "8": "b", "9": "g", "|": "l"})
CONFUSABLE_PAIRS = (("rn", "m"), ("vv", "w"), ("cl", "d"))


@dataclass(frozen=True)
class LinkAssessment:
    link: EmailLink
    reasons: tuple[ReasonCode, ...]

    @property
    def is_flagged(self) -> bool:
        return bool(self.reasons)

    def as_row(self) -> dict:
        """The shape stored on EMAIL# and sent to the PWA; the PWA renders it as text only."""
        return {"text": self.link.visible_text, "domain": self.link.registrable_domain, "href": self.link.href, "flagged": self.is_flagged}


def confusable_skeleton(text: str) -> str:
    """'PayPa1.com' and 'paypal.com' share a skeleton; lists.py (lookalike senders) uses the same one later."""
    skeleton = text.casefold().translate(CONFUSABLE_CHARACTERS)
    for pair, replacement in CONFUSABLE_PAIRS:
        skeleton = skeleton.replace(pair, replacement)
    return re.sub(r"[^a-z0-9.]", "", skeleton)


def domain_named_in_text(visible_text: str) -> str:
    match = HOST_LIKE_TEXT.match(visible_text.strip())
    return registrable_domain_of_host(match.group(1)) if match else ""


def is_lookalike_of(link_domain: str, family_domain: str) -> bool:
    """Same skeleton but a different domain, or the family label hyphenated into a different domain (zelle-pay-help)."""
    if not link_domain or link_domain == family_domain:
        return False
    if confusable_skeleton(link_domain) == confusable_skeleton(family_domain):
        return True
    family_label = family_domain.split(".")[0]
    link_label = link_domain.split(".")[0]
    return len(family_label) >= 4 and family_label in link_label.split("-") and link_label != family_label


def assess_link(link: EmailLink, family_domains: frozenset[str]) -> LinkAssessment:
    reasons: list[ReasonCode] = []
    if link.is_hidden:
        reasons.append(ReasonCode.HIDDEN_LINK)
    named_domain = domain_named_in_text(link.visible_text)
    if named_domain and link.registrable_domain and named_domain != link.registrable_domain:
        reasons.append(ReasonCode.LINK_TEXT_HREF_MISMATCH)
    if link.registrable_domain in KNOWN_SHORTENER_HOSTS:
        reasons.append(ReasonCode.KNOWN_SHORTENER)
    if any(is_lookalike_of(link.registrable_domain, family_domain) for family_domain in family_domains):
        reasons.append(ReasonCode.LOOKALIKE_LINK_DOMAIN)
    return LinkAssessment(link, tuple(reasons))


def assess_links(links: tuple[EmailLink, ...], family_domains: frozenset[str]) -> list[LinkAssessment]:
    return [assess_link(link, family_domains) for link in links]


def link_reasons(assessments: list[LinkAssessment], links_unreadable: bool) -> list[ReasonCode]:
    """Each reason once, in first-seen order; an unreadable HTML part is itself a reason."""
    reasons: list[ReasonCode] = [ReasonCode.LINKS_UNREADABLE] if links_unreadable else []
    for assessment in assessments:
        for reason in assessment.reasons:
            if reason not in reasons:
                reasons.append(reason)
    return reasons
