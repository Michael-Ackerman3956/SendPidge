"""Link reasons: text versus href, hidden anchors, shorteners, lookalikes of the family's domains; clean links say nothing."""

from shared.email_parse import EmailLink
from shared.enums import ReasonCode
from shared.links import assess_link, assess_links, link_reasons
from shared.lists import confusable_skeleton

FAMILY_DOMAINS = frozenset({"example.com", "kakaobank.com"})


def link(text: str, href: str, domain: str, is_hidden: bool = False) -> EmailLink:
    return EmailLink(text, href, domain, is_hidden)


def test_text_href_mismatch():
    assessment = assess_link(link("zelle.com", "https://zelle-pay-help.example/renew", "zelle-pay-help.example"), frozenset())
    assert assessment.reasons == (ReasonCode.LINK_TEXT_HREF_MISMATCH,)
    assert assessment.as_row() == {"text": "zelle.com", "domain": "zelle-pay-help.example", "href": "https://zelle-pay-help.example/renew", "flagged": True}


def test_plain_words_over_a_link_are_not_a_mismatch():
    assert assess_link(link("Click here", "https://anything.example/", "anything.example"), frozenset()).reasons == ()
    assert assess_link(link("www.zelle.com/login", "https://pay.zelle.com/x", "zelle.com"), frozenset()).reasons == ()


def test_hidden_anchor():
    assert assess_link(link("", "https://img.example/x", "img.example", is_hidden=True), frozenset()).reasons == (ReasonCode.HIDDEN_LINK,)


def test_known_shortener():
    assert assess_link(link("see this", "https://bit.ly/3abc", "bit.ly"), frozenset()).reasons == (ReasonCode.KNOWN_SHORTENER,)


def test_lookalike_of_a_family_domain():
    assert assess_link(link("pay", "https://kakaobank-secure-pay.example/", "kakaobank-secure-pay.example"), FAMILY_DOMAINS).reasons == (ReasonCode.LOOKALIKE_LINK_DOMAIN,)
    assert assess_link(link("pay", "https://examp1e.com/", "examp1e.com"), FAMILY_DOMAINS).reasons == (ReasonCode.LOOKALIKE_LINK_DOMAIN,)
    assert assess_link(link("pay", "https://kakaobank.com/", "kakaobank.com"), FAMILY_DOMAINS).reasons == ()


def test_clean_links_no_reasons():
    clean = [link("Garden Weekly", "https://gardenweekly.example/october", "gardenweekly.example"), link("unsubscribe", "mailto:x@y.example", "")]
    assessments = assess_links(tuple(clean), FAMILY_DOMAINS)
    assert all(not assessment.is_flagged for assessment in assessments)
    assert link_reasons(assessments, links_unreadable=False) == []


def test_reasons_are_deduplicated_and_unreadable_html_is_a_reason():
    two_mismatches = assess_links((link("a.com", "https://b.example/", "b.example"), link("c.com", "https://d.example/", "d.example")), frozenset())
    assert link_reasons(two_mismatches, links_unreadable=True) == [ReasonCode.LINKS_UNREADABLE, ReasonCode.LINK_TEXT_HREF_MISMATCH]


def test_confusable_skeleton():
    assert confusable_skeleton("PayPa1.com") == confusable_skeleton("paypal.com")
    assert confusable_skeleton("rnicrosoft.com") == confusable_skeleton("microsoft.com")
