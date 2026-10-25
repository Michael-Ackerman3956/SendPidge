"""Links come from the HTML part by parsing alone: visible text, href, registrable domain; nothing is fetched."""

from pathlib import Path

from shared.email_parse import LINKS_MAX_COUNT, EmailLink, parse_eml, registrable_domain_of_href

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


def html_email(html_body: str, charset: str = "utf-8") -> bytes:
    return (
        "From: Garden Weekly <hello@gardenweekly.example>\nTo: grandma@sendpidge.example\nSubject: Hello\n"
        f"MIME-Version: 1.0\nContent-Type: text/html; charset=\"{charset}\"\n\n{html_body}\n"
    ).encode()


def test_html_anchors_become_links():
    parsed = parse_eml((SAMPLES / "newsletter-link.eml").read_bytes())
    assert parsed.links == (EmailLink("zelle.com", "https://zelle-pay-help.example/renew", "zelle-pay-help.example", False),)
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
