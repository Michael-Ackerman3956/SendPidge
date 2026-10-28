"""Link reasons: text versus href, hidden anchors, shorteners, lookalikes of the family's domains; clean links say nothing."""

from dataclasses import replace

from shared.email_parse import EmailLink, ParsedEmail, link_kind_of_href, registrable_domain_of_host
from shared.enums import LINK_REASON_CODES, HiddenBy, LinkSource, ReasonCode
from shared.links import WHOLE_BODY_LINK_SHARE, assess_link, assess_links, link_reasons
from shared.lists import confusable_skeleton

FAMILY_DOMAINS = frozenset({"example.com", "kakaobank.com"})


PLAIN_EMAIL = ParsedEmail("Garden Weekly", "hello@gardenweekly.example", "gardenweekly.example", "Hello", "Hello.")


def link(text: str, href: str, domain: str, is_hidden: bool = False, body_share: float = 0.1,
         has_internationalised_host: bool = False) -> EmailLink:
    hidden_by = HiddenBy.NO_TEXT if is_hidden else HiddenBy.NONE
    return EmailLink(text, href, domain, is_hidden, link_kind_of_href(href), hidden_by, LinkSource.HTML_PART, body_share,
                     has_internationalised_host)


def test_text_href_mismatch():
    assessment = assess_link(link("zelle.com", "https://zelle-pay-help.example/renew", "zelle-pay-help.example"), frozenset())
    assert assessment.reasons == (ReasonCode.LINK_TEXT_HREF_MISMATCH,)
    assert assessment.as_row() == {"text": "zelle.com", "domain": "zelle-pay-help.example", "href": "https://zelle-pay-help.example/renew",
                                   "flagged": True, "kind": "web", "hiddenBy": "none", "reasons": ["link_text_href_mismatch"],
                                   "listedBy": None}


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
    assert link_reasons(assessments, PLAIN_EMAIL) == []


def test_reasons_are_deduplicated_and_unreadable_html_is_a_reason():
    two_mismatches = assess_links((link("a.com", "https://b.example/", "b.example"), link("c.com", "https://d.example/", "d.example")), frozenset())
    assert link_reasons(two_mismatches, replace(PLAIN_EMAIL, links_unreadable=True)) == [ReasonCode.LINKS_UNREADABLE, ReasonCode.LINK_TEXT_HREF_MISMATCH]


def test_confusable_skeleton():
    assert confusable_skeleton("PayPa1.com") == confusable_skeleton("paypal.com")
    assert confusable_skeleton("rnicrosoft.com") == confusable_skeleton("microsoft.com")


def test_whole_body_threshold_0_59_none_0_60_reason():
    assert WHOLE_BODY_LINK_SHARE == 0.6
    assert assess_link(link("Read more", "https://garden.example/", "garden.example", body_share=0.59), frozenset()).reasons == ()
    assert assess_link(link("Read more", "https://garden.example/", "garden.example", body_share=0.6), frozenset()).reasons == (
        ReasonCode.WHOLE_BODY_LINK,)


def test_script_href_reason():
    script_assessment = assess_link(link("Open the invoice", "javascript:location='https://x.example'", ""), frozenset())
    data_assessment = assess_link(link("Open the invoice", "data:text/html;base64,PGgxPg==", ""), frozenset())
    assert script_assessment.reasons == data_assessment.reasons == (ReasonCode.SCRIPT_HREF_LINK,)
    assert script_assessment.as_row()["kind"] == "script" and data_assessment.as_row()["kind"] == "data"


def test_idn_lookalike_of_a_family_domain_uses_lookalike_link_domain():
    punycode_domain = registrable_domain_of_host("xn--80ak6aa92e.com")
    idn_link = link("pay", "https://xn--80ak6aa92e.com/", punycode_domain, has_internationalised_host=True)
    assert assess_link(idn_link, frozenset({"apple.com"})).reasons == (ReasonCode.LOOKALIKE_LINK_DOMAIN, ReasonCode.IDN_LINK_DOMAIN)
    assert assess_link(idn_link, frozenset()).as_row()["domain"] == "xn--80ak6aa92e.com"  # shown as a browser shows it, never 'apple.com'


def test_every_new_link_reason_is_in_link_reason_codes():
    new_link_reasons = {
        ReasonCode.WHOLE_BODY_LINK, ReasonCode.SCRIPT_HREF_LINK, ReasonCode.CSS_HIDDEN_LINK, ReasonCode.IMAGE_MAP_LINK,
        ReasonCode.HTML_ATTACHMENT_LINK, ReasonCode.LINKS_TRUNCATED, ReasonCode.IDN_LINK_DOMAIN, ReasonCode.IMAGE_ONLY_BODY,
    }
    assert new_link_reasons <= LINK_REASON_CODES
    camouflaged = (
        replace(link("a", "https://a.example/", "a.example"), hidden_by=HiddenBy.CSS),
        replace(link("b", "https://b.example/", "b.example"), source=LinkSource.IMAGE_MAP, hidden_by=HiddenBy.IMAGE_ONLY),
        replace(link("c", "https://c.example/", "c.example"), source=LinkSource.HTML_ATTACHMENT),
    )
    picture_email = replace(PLAIN_EMAIL, links_truncated=True, image_count=1, visible_html_characters=39)
    assert link_reasons(assess_links(camouflaged, frozenset()), picture_email) == [
        ReasonCode.LINKS_TRUNCATED, ReasonCode.IMAGE_ONLY_BODY, ReasonCode.CSS_HIDDEN_LINK, ReasonCode.IMAGE_MAP_LINK,
        ReasonCode.HTML_ATTACHMENT_LINK]
    assert link_reasons([], replace(picture_email, links_truncated=False, visible_html_characters=40)) == []
