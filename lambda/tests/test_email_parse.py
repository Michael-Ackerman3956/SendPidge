"""Links come from the HTML part by parsing alone: visible text, href, registrable domain; nothing is fetched."""

import time
from email.message import EmailMessage
from pathlib import Path

import pytest

from shared.email_parse import LINKS_MAX_COUNT, EmailLink, parse_eml, registrable_domain_of_host, registrable_domain_of_href
from shared.enums import HiddenBy, LinkKind, LinkSource
from shared.lists import confusable_skeleton

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


def html_email(html_body: str, charset: str = "utf-8") -> bytes:
    return (
        "From: Garden Weekly <hello@gardenweekly.example>\nTo: grandma@sendpidge.example\nSubject: Hello\n"
        f"MIME-Version: 1.0\nContent-Type: text/html; charset=\"{charset}\"\n\n{html_body}\n"
    ).encode()


def test_html_anchors_become_links():
    parsed = parse_eml((SAMPLES / "newsletter-link.eml").read_bytes())
    newsletter_html_characters = parsed.visible_html_characters
    assert parsed.links == (EmailLink(
        "zelle.com", "https://zelle-pay-help.example/renew", "zelle-pay-help.example", False,
        LinkKind.WEB, HiddenBy.NONE, LinkSource.HTML_PART, len("zelle.com") / newsletter_html_characters, False,
    ),)
    assert (parsed.links_truncated, parsed.image_count) == (False, 0)
    assert parsed.links_unreadable is False
    assert "zelle-pay-help" not in parsed.text_body  # the Reader still sees the plain part only


def test_hidden_anchor_and_non_web_hrefs_are_kept_without_a_domain():
    parsed = parse_eml(html_email(
        '<a href="https://img.example/x"><img src="pixel.gif"></a><a href="mailto:a@b.example">write</a><a href="#top">top</a>'
    ))
    assert [(link.is_hidden, link.registrable_domain) for link in parsed.links] == [(True, "img.example"), (False, ""), (False, "")]


def test_links_are_capped_and_a_plain_email_has_none():
    many_anchors = "".join(f'<a href="https://site{index}.example/">s{index}</a>' for index in range(LINKS_MAX_COUNT + 5))
    assert len(parse_eml(html_email(many_anchors)).links) == LINKS_MAX_COUNT
    assert parse_eml((SAMPLES / "fake-jimin.eml").read_bytes()).links == ()


def test_an_html_part_that_will_not_decode_is_reported_not_ignored():
    raw = (
        "From: A <a@b.example>\nTo: grandma@sendpidge.example\nSubject: Hello\nMIME-Version: 1.0\n"
        'Content-Type: multipart/alternative; boundary="b"\n\n--b\nContent-Type: text/plain; charset="utf-8"\n\nplain words\n'
        '--b\nContent-Type: text/html; charset="no-such-charset"\n\n<a href="https://x.example">x</a>\n--b--\n'
    ).encode()
    parsed = parse_eml(raw)
    assert (parsed.text_body, parsed.links, parsed.links_unreadable) == ("plain words", (), True)


def test_registrable_domain_handles_two_part_suffixes_and_odd_hrefs():
    assert registrable_domain_of_href("https://pay.bank.co.kr/x") == "bank.co.kr"
    assert registrable_domain_of_href("HTTP://WWW.Zelle.com") == "zelle.com"
    assert registrable_domain_of_href("javascript:alert(1)") == ""
    assert registrable_domain_of_href("https://[::1") == ""


def test_plain_text_keeps_paragraph_breaks_and_joins_wrapped_lines():
    raw = (b"From: Jimin <jimin@example.com>\r\nTo: grandma@sendpidge.example\r\nSubject: Hi\r\n"
           b"Content-Type: text/plain\r\n\r\nGrandma, it's me.\r\nMy phone broke.\r\n\r\n  \r\nCall me back.\r\n")
    assert parse_eml(raw).text_body == "Grandma, it's me. My phone broke.\n\nCall me back."


def test_unclosed_anchor_is_kept_with_its_text():
    parsed = parse_eml(html_email('<p>Your parcel <a href="https://parcel-fee.example/pay">pay the fee here'))
    assert [(link.visible_text, link.registrable_domain) for link in parsed.links] == [("pay the fee here", "parcel-fee.example")]


def test_the_21st_link_sets_links_truncated():
    exactly_at_cap = "".join(f'<a href="https://site{index}.example/">s{index}</a>' for index in range(LINKS_MAX_COUNT))
    assert parse_eml(html_email(exactly_at_cap)).links_truncated is False
    one_past_cap = exactly_at_cap + '<a href="https://twenty-first.example/">last</a>'
    parsed = parse_eml(html_email(one_past_cap))
    assert (len(parsed.links), parsed.links_truncated) == (LINKS_MAX_COUNT, True)


def test_script_and_data_hrefs_are_typed_not_blank():
    parsed = parse_eml(html_email(
        '<a href="javascript:alert(1)">Open</a><a href=" java&#9;script:go()">Go</a><a href="data:text/html,hi">See</a>'
        '<a href="VBScript:x">V</a><a href="mailto:a@b.example">write</a><a href="tel:+15550100">call</a><a href="#top">top</a>'
    ))
    assert [link.kind for link in parsed.links] == [
        LinkKind.SCRIPT, LinkKind.SCRIPT, LinkKind.DATA, LinkKind.SCRIPT, LinkKind.MAILTO, LinkKind.TEL, LinkKind.OTHER]


def test_area_tags_become_image_map_links():
    parsed = parse_eml(html_email(
        '<img src="banner.png" usemap="#m"><map name="m"><area shape="rect" coords="0,0,9,9" href="https://evil.example/" alt="Pay">'
        '<area shape="rect" coords="9,9,19,19" nohref></map>'
    ))
    assert [(link.source, link.hidden_by, link.visible_text, link.registrable_domain) for link in parsed.links] == [
        (LinkSource.IMAGE_MAP, HiddenBy.IMAGE_ONLY, "Pay", "evil.example")]
    assert parsed.image_count == 1  # the picture-only reason reads this count; nothing else proves a real parse sets it


def test_css_hidden_ancestor_marks_the_link():
    parsed = parse_eml(html_email(
        '<div style="Display: None !important"><p><a href="https://a.example/">a</a></p></div>'
        '<span style="font-size:0px"><a href="https://b.example/">b</a></span>'
        '<a style="opacity:0" href="https://c.example/">c</a>'
        '<span style="opacity:0.5;font-size:12px"><a href="https://d.example/">d</a></span>'
    ))
    assert [link.hidden_by for link in parsed.links] == [HiddenBy.CSS, HiddenBy.CSS, HiddenBy.CSS, HiddenBy.NONE]


def test_punycode_host_decodes_before_the_registrable_domain():
    decoded_domain = registrable_domain_of_host("xn--80ak6aa92e.com")
    assert decoded_domain != "xn--80ak6aa92e.com"
    assert confusable_skeleton(decoded_domain) == "apple.com"
    link = parse_eml(html_email('<a href="https://login.xn--80ak6aa92e.com/">apple.com</a>')).links[0]
    assert (link.registrable_domain, link.has_internationalised_host) == (decoded_domain, True)
    assert registrable_domain_of_host("xn--zz.com") == "xn--zz.com"  # will not decode: kept raw, never guessed


def test_html_attachment_anchors_count_against_the_cap():
    body_anchors = "".join(f'<a href="https://body{index}.example/">b{index}</a>' for index in range(15))
    attachment_anchors = "".join(f'<a href="https://attached{index}.example/">a{index}</a>' for index in range(10))
    raw = (
        "From: A <a@b.example>\nTo: grandma@sendpidge.example\nSubject: Invoice\nMIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="b"\n\n--b\nContent-Type: text/html; charset="utf-8"\n\n'
        f"{body_anchors}\n--b\nContent-Type: text/html; charset=\"utf-8\"\nContent-Disposition: attachment; filename=\"invoice.html\"\n\n"
        f"{attachment_anchors}\n--b--\n"
    ).encode()
    parsed = parse_eml(raw)
    assert [link.source for link in parsed.links] == [LinkSource.HTML_PART] * 15 + [LinkSource.HTML_ATTACHMENT] * 5
    assert parsed.links_truncated is True


def test_body_share_is_one_for_a_wrapped_body_and_zero_point_one_for_a_short_link():
    wrapped = parse_eml(html_email('<a href="https://pay.example/"><p>Your account is locked.</p><p>Tap anywhere to unlock it.</p></a>'))
    short = parse_eml(html_email('abcdefghi<a href="https://pay.example/">x</a>'))  # text joins as rendered: 10 characters
    assert wrapped.links[0].body_share == 1.0
    assert short.links[0].body_share == pytest.approx(0.1)


SCAM_ANCHOR_HTML = '<p>Pay now <a href="https://evil.example/pay">here</a></p>'


def new_mime_message(plain_text: str) -> EmailMessage:
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "A <a@b.example>", "grandma@sendpidge.example", "Hello"
    message.set_content(plain_text)
    return message


def test_unmatched_end_tags_parse_in_linear_time():
    html_body = "<b>" * 20000 + "</i>" * 20000 + '<a href="https://evil.example/">x</a>'
    started_at = time.perf_counter()
    parsed = parse_eml(html_email(html_body))
    assert time.perf_counter() - started_at < 1.5
    assert [link.registrable_domain for link in parsed.links] == ["evil.example"]


def test_hidden_text_counts_toward_neither_the_body_nor_the_anchor():
    hidden_filler = '<div style="display:none">' + "x" * 540 + "</div>"
    whole_body = parse_eml(html_email('<a href="https://evil.example/">Your account is locked. Tap to unlock.</a>' + hidden_filler))
    assert whole_body.links[0].body_share >= 0.6
    assert parse_eml(html_email('<img src="x">' + hidden_filler)).visible_html_characters < 40
    hidden_span = parse_eml(html_email('<a href="https://evil.example/"><span style="display:none">evil.example</span>Pay</a>'))
    assert hidden_span.links[0].visible_text == "Pay"


def test_split_anchor_text_reads_as_one_word():
    for anchor_text in ("zel<b></b>le.com", "zelle&#8203;.com", "zel&shy;le.com"):
        parsed = parse_eml(html_email(f'<a href="https://evil.example/">{anchor_text}</a>'))
        assert parsed.links[0].visible_text == "zelle.com", anchor_text


def test_an_image_only_whole_body_link_has_share_one():
    parsed = parse_eml(html_email('<a href="https://evil.example/"><img src="x.png"></a>'))
    assert parsed.links[0].body_share == 1.0


def test_hosts_are_read_the_way_a_browser_reads_them():
    cyrillic_a_host = "\u0430pple.com"
    expected_domain_by_href = {
        "https://evil.example\\@zelle.com/": "evil.example",
        "http:zelle-help.example": "zelle-help.example",
        "https:///zelle-help.example/x": "zelle-help.example",
        "https:\\\\zelle-help.example": "zelle-help.example",
        "https://%7Aelle-help.example": "zelle-help.example",
        "https://%D0%B0pple.com/": cyrillic_a_host,
    }
    anchors = "".join(f'<a href="{href}">x</a>' for href in expected_domain_by_href)
    links = parse_eml(html_email(anchors)).links
    assert [link.registrable_domain for link in links] == list(expected_domain_by_href.values())
    assert [link.href for link in links] == list(expected_domain_by_href)  # the raw href, shown on long-press
    assert [link.has_internationalised_host for link in links] == [False] * 5 + [True]


def build_messages_with_the_link_somewhere_else() -> dict[str, EmailMessage]:
    second_html = new_mime_message("plain words")
    second_html.add_alternative("<p>clean</p>", subtype="html")
    second_html.add_alternative(SCAM_ANCHOR_HTML, subtype="html")
    inline_html = new_mime_message("plain words")
    inline_html.add_alternative("<p>clean</p>", subtype="html")
    inline_html.add_attachment(SCAM_ANCHOR_HTML, subtype="html", disposition="inline")
    forwarded = new_mime_message("see below")
    forwarded_original = new_mime_message("")
    forwarded_original.set_content(SCAM_ANCHOR_HTML, subtype="html")
    forwarded.add_attachment(forwarded_original)
    nested_mixed = new_mime_message("see the statement")
    statement = EmailMessage()
    statement.set_content("statement attached")
    statement.add_attachment(SCAM_ANCHOR_HTML, subtype="html", filename="statement.html")
    nested_mixed.make_mixed()
    nested_mixed.attach(statement)
    octet_stream = new_mime_message("invoice attached")
    octet_stream.add_attachment(SCAM_ANCHOR_HTML.encode(), maintype="application", subtype="octet-stream", filename="invoice.html")
    svg = new_mime_message("picture attached")
    svg_text = '<svg xmlns:xlink="http://www.w3.org/1999/xlink"><a xlink:href="https://evil.example/"><text>Pay</text></a></svg>'
    svg.add_attachment(svg_text.encode(), maintype="image", subtype="svg+xml", filename="pay.svg")
    return {"second html": second_html, "inline html": inline_html, "forward": forwarded, "nested mixed": nested_mixed,
            "octet-stream .html": octet_stream, "svg": svg}


def test_every_html_part_and_attachment_is_scanned():
    for case_name, message in build_messages_with_the_link_somewhere_else().items():
        parsed = parse_eml(message.as_bytes())
        assert "evil.example" in {link.registrable_domain for link in parsed.links}, case_name


def test_more_ways_to_hide_a_link():
    hidden_anchors = [
        '<a hidden href="https://a.example/">a</a>', '<div hidden><a href="https://a.example/">a</a></div>',
        *(f'<span style="{style}"><a href="https://a.example/">a</a></span>' for style in (
            "mso-hide:all", "display:/**/none", "display:n\\one", "opacity:.0", "opacity:0%", "visibility:collapse",
            "font-size:0.5px")),
    ]
    shown_anchors = [f'<span style="{style}"><a href="https://a.example/">a</a></span>' for style in ("color:#fff", "font-size:1px")]
    parsed = parse_eml(html_email("".join(hidden_anchors + shown_anchors)))
    assert [link.hidden_by for link in parsed.links] == [HiddenBy.CSS] * len(hidden_anchors) + [HiddenBy.NONE] * 2


def test_an_area_inside_an_open_anchor_counts_against_the_cap():
    anchors = "".join(f'<a href="https://site{index}.example/">s{index}</a>' for index in range(LINKS_MAX_COUNT - 1))
    parsed = parse_eml(html_email(anchors + '<a href="https://a.example/"><area href="https://b.example/" alt="b"></a>'))
    assert (len(parsed.links), parsed.links_truncated) == (LINKS_MAX_COUNT, True)


WHOLE_BODY_ANCHOR = '<a href="https://evil.example/">Your account is locked. Tap to unlock.</a>'
HIDDEN_PADDING = "x" * 540


def test_a_fullwidth_slash_in_the_userinfo_still_finds_the_host():
    expected_domain_by_href = {
        "https://zelle.com／login@evil.example/pay": "evil.example",
        "https://zelle.com？@evil.example/": "evil.example",
        "https://ｅｖｉｌ．example/": "evil.example",
    }
    anchors = "".join(f'<a href="{href}">www.zelle.com</a>' for href in expected_domain_by_href)
    links = parse_eml(html_email(anchors)).links
    assert [link.registrable_domain for link in links] == list(expected_domain_by_href.values())


def test_content_past_the_open_element_cap_counts_as_hidden():
    too_deep = parse_eml(html_email("<div>" * 300 + "y" * 200))
    assert too_deep.visible_html_characters == 0
    padded = parse_eml(html_email(WHOLE_BODY_ANCHOR + "<div>" * 256 + f'<p style="display:none">{HIDDEN_PADDING}</p>'))
    assert padded.links[0].body_share >= 0.6


def test_an_ignored_end_tag_keeps_the_padding_hidden():
    padded = parse_eml(html_email(WHOLE_BODY_ANCHOR + f'<span style="display:none"><div></span>{HIDDEN_PADDING}'))
    assert padded.links[0].body_share >= 0.6
    unmatched = parse_eml(html_email('<div style="display:none"></i></b><a href="https://evil.example/">x</a></div>'))
    assert [link.hidden_by for link in unmatched.links] == [HiddenBy.CSS]


def test_more_inline_hiding_forms():
    for style in ("max-height:0;overflow:hidden", "height:0;overflow:hidden", "position:absolute;left:-9999px",
                  "color:transparent"):
        parsed = parse_eml(html_email(WHOLE_BODY_ANCHOR + f'<div style="{style}">{HIDDEN_PADDING}</div>'))
        assert parsed.links[0].body_share >= 0.6, style


def test_body_closes_an_unclosed_head():
    sloppy_document = (
        '<html><head><title>t</title><body><a href="https://shop.example/">click here now to see the sale</a><img src=x>'
    )
    parsed = parse_eml(html_email(sloppy_document))
    assert [link.hidden_by for link in parsed.links] == [HiddenBy.NONE]
    assert parsed.visible_html_characters > 0
