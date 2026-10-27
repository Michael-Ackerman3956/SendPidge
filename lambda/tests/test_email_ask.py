"""The ask by email: SES is one more sender in the fan-out; a NO mailed back from the contact's own, DKIM-aligned address
denies the hold; a YES by email never releases (the mailbox is the channel scammers hijack); every other reply is dropped."""

import asyncio

import boto3
import pytest

import time

from shared import ask_link, channels, email_ask, graph, reach_points, verdict
from shared.enums import ActionOutcome, Channel, EmailedAnswerOutcome, RequestState, VerdictAnswer, VerifiedVia
from tests.test_channels import REQUEST_ROW, sender_returning
from tests.test_verdict import BUCKET_NAME, CONTACT_EMAIL, aws, new_request, status_of  # noqa: F401
from tests.test_verify_contact import verdict_handler  # noqa: F401

JIMIN_WHATSAPP_POINT = reach_points.ReachPoint(Channel.WHATSAPP, "+821012345678", VerifiedVia.SEED)
CONTACT_WITH_EMAIL = graph.Contact("CONTACT#jimin", "Jimin", "jimin", (JIMIN_WHATSAPP_POINT, reach_points.ReachPoint(Channel.EMAIL, CONTACT_EMAIL, None)))
REPLY_OBJECT_KEY = "asks/reply-1.eml"
REPLY_MESSAGE_ID = "<reply-1@example.com>"


def raw_reply(request_id: str, sender: str = f"Jimin <{CONTACT_EMAIL}>", body: str = "NO",
              auth_headers: tuple[str, ...] = ("spf=pass;dkim=pass",), signing_domain: str = "example.com",
              subject: str | None = None, message_id: str = REPLY_MESSAGE_ID) -> bytes:
    subject = f"Re: {email_ask.ask_subject(request_id)}" if subject is None else subject
    header_lines = [f"From: {sender}", "To: pidge@sendpidge.example", f"Subject: {subject}", f"Message-ID: {message_id}",
                    f"DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed;\r\n d={signing_domain}; s=google; h=from:to; b=abc"]
    header_lines += [f"X-Pidge-Auth: {value}" for value in auth_headers]
    quoted_ask = "\r\n\r\nOn Mon, Oct 26, 2026 at 8:00 PM Pidge <pidge@sendpidge.example> wrote:\r\n> Grandma received an email"
    return ("\r\n".join(header_lines) + "\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + body + quoted_ask + "\r\n").encode()


def test_email_joins_the_fan_out_only_with_the_flag_and_an_address(monkeypatch):
    monkeypatch.setattr(channels, "WHATSAPP_ENABLED", True)
    monkeypatch.setattr(channels, "EMAIL_ASK_ENABLED", True)
    monkeypatch.setattr(channels, "ask_in_app", sender_returning(Channel.INAPP))
    monkeypatch.setattr(channels, "ask_on_whatsapp", sender_returning(Channel.WHATSAPP))
    monkeypatch.setattr(channels, "ask_on_email", sender_returning(Channel.EMAIL))
    sent = asyncio.run(channels.ask_on_every_channel(REQUEST_ROW, CONTACT_WITH_EMAIL, "Grandma"))
    assert sent == [Channel.INAPP, Channel.WHATSAPP, Channel.EMAIL]
    without_email = graph.Contact("CONTACT#jimin", "Jimin", "jimin", (JIMIN_WHATSAPP_POINT,))
    assert asyncio.run(channels.ask_on_every_channel(REQUEST_ROW, without_email, "Grandma")) == [Channel.INAPP, Channel.WHATSAPP]
    monkeypatch.setattr(channels, "EMAIL_ASK_ENABLED", False)
    assert asyncio.run(channels.ask_on_every_channel(REQUEST_ROW, CONTACT_WITH_EMAIL, "Grandma")) == [Channel.INAPP, Channel.WHATSAPP]


def test_the_ask_goes_from_pidge_to_the_contacts_own_address_with_the_request_id_in_the_subject(monkeypatch):
    monkeypatch.setenv("RECEIPT_SENDER", "pidge@sendpidge.example")
    monkeypatch.setenv("ASK_LINK_BASE_URL", "https://app.sendpidge.example")
    monkeypatch.setattr(ask_link, "_signing_key", b"test-signing-key")
    sent_messages = []

    class FakeSesClient:
        def send_email(self, **kwargs):
            sent_messages.append(kwargs)
            return {"MessageId": "ses-1"}

    monkeypatch.setattr(channels, "get_ses_client", lambda: FakeSesClient())
    assert channels.ask_on_email(REQUEST_ROW, CONTACT_WITH_EMAIL, "Grandma", CONTACT_EMAIL) is Channel.EMAIL
    [message] = sent_messages
    assert (message["FromEmailAddress"], message["Destination"]) == ("pidge@sendpidge.example", {"ToAddresses": [CONTACT_EMAIL]})
    assert message["Content"]["Simple"]["Subject"]["Data"] == "Pidge: was this you? [req-1]"
    body = message["Content"]["Simple"]["Body"]["Text"]["Data"]
    assert "Asks for money." in body and "reply to this email with one word: NO" in body and "A Yes by email is not accepted" in body
    [no_link] = [line for line in body.splitlines() if line.startswith("https://app.sendpidge.example/no/")]
    path, query = no_link.removeprefix("https://app.sendpidge.example").split("?")
    query_fields = dict(field.split("=") for field in query.split("&"))
    assert path == "/no/req-1" and query_fields["sig"] == ask_link.sign("req-1", int(query_fields["exp"]))
    assert "https://app.sendpidge.example/\n" in body and "?token=" not in body  # the Yes line opens the app, never carries a token


REQUEST_ID = "req-0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"


@pytest.mark.parametrize("raw, expected_outcome", [
    (raw_reply(REQUEST_ID, subject="Re: hello"), EmailedAnswerOutcome.NO_REQUEST_ID),
    (raw_reply(REQUEST_ID, auth_headers=("spf=pass;dkim=fail",)), EmailedAnswerOutcome.DKIM_NOT_PASSED),
    (raw_reply(REQUEST_ID, auth_headers=()), EmailedAnswerOutcome.DKIM_NOT_PASSED),
    (raw_reply(REQUEST_ID, signing_domain="example-com.20230601.gappssmtp.com"), EmailedAnswerOutcome.DKIM_NOT_ALIGNED),
    (raw_reply(REQUEST_ID, body="Thanks, looks fine"), EmailedAnswerOutcome.NOT_AN_ANSWER),
    (raw_reply(REQUEST_ID, body=""), EmailedAnswerOutcome.NOT_AN_ANSWER),
])
def test_a_reply_that_is_not_a_proven_answer_is_dropped_with_a_reason(raw, expected_outcome):
    assert email_ask.read_emailed_answer(raw).outcome is expected_outcome


def test_no_and_yes_are_read_from_the_first_word_above_the_quoted_ask():
    no_answer = email_ask.read_emailed_answer(raw_reply(REQUEST_ID, body="no, that wasn't me!"))
    assert (no_answer.outcome, no_answer.request_id, no_answer.answer, no_answer.sender_address, no_answer.message_id) == (
        EmailedAnswerOutcome.ACCEPTED, REQUEST_ID, VerdictAnswer.NO, CONTACT_EMAIL, REPLY_MESSAGE_ID)
    assert email_ask.read_emailed_answer(raw_reply(REQUEST_ID, body="YES")).answer is VerdictAnswer.YES


def s3_event(object_key: str = REPLY_OBJECT_KEY) -> dict:
    return {"Records": [{"s3": {"bucket": {"name": BUCKET_NAME}, "object": {"key": object_key}}}]}


def store_reply(raw: bytes, object_key: str = REPLY_OBJECT_KEY) -> None:
    s3_client = boto3.client("s3")
    if BUCKET_NAME not in [bucket["Name"] for bucket in s3_client.list_buckets()["Buckets"]]:
        s3_client.create_bucket(Bucket=BUCKET_NAME)
    s3_client.put_object(Bucket=BUCKET_NAME, Key=object_key, Body=raw)


def test_a_mailed_no_from_the_contacts_own_address_denies_the_hold_once(verdict_handler):  # noqa: F811
    handler, _ = verdict_handler
    request_id = new_request()
    store_reply(raw_reply(request_id))
    handler.handle(s3_event(), None)
    request_row = graph.get_request(request_id)
    assert (request_row["status"], request_row["repliedVia"], request_row["inboundMessageIds"]) == (
        str(RequestState.DENIED), str(Channel.EMAIL), [f"email:{REPLY_MESSAGE_ID}"])
    store_reply(raw_reply(request_id), "asks/reply-1-redelivered.eml")
    handler.handle(s3_event("asks/reply-1-redelivered.eml"), None)
    assert graph.get_request(request_id)["inboundMessageIds"] == [f"email:{REPLY_MESSAGE_ID}"]


def test_a_mailed_yes_never_releases_and_a_stranger_cannot_deny(verdict_handler):  # noqa: F811
    handler, _ = verdict_handler
    request_id = new_request()
    store_reply(raw_reply(request_id, body="YES"))
    handler.handle(s3_event(), None)
    assert status_of(request_id) == str(RequestState.PENDING)
    store_reply(raw_reply(request_id, sender="Jimin <jimin@other.example>", signing_domain="other.example"))
    handler.handle(s3_event(), None)
    assert status_of(request_id) == str(RequestState.PENDING)


def test_the_cross_channel_rule_refuses_a_yes_only_on_the_inbound_channel(aws):  # noqa: F811
    request_id = new_request()
    assert graph.get_request(request_id)["inboundChannel"] == "email"  # written at creation, not read from a default
    now = graph.now_timestamp()
    by_email = verdict.SenderIdentity(Channel.EMAIL, CONTACT_EMAIL.upper())
    assert verdict.record_answer(request_id, VerdictAnswer.YES, by_email, "email:a", now).outcome is ActionOutcome.WRONG_CHANNEL
    assert status_of(request_id) == str(RequestState.PENDING)
    in_app = verdict.SenderIdentity(Channel.INAPP, "jimin")
    assert verdict.record_answer(request_id, VerdictAnswer.YES, in_app, "inapp:a", now).outcome is ActionOutcome.RECORDED
    assert verdict.inbound_channel_of({"PK": "REQUEST#req-old"}) is Channel.EMAIL
