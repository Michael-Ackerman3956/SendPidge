"""Questions mailed to pidge@: only her real mailbox with DKIM passed and signed by her own domain writes the CHAT# row,
the row matches the window's, the hourly cap still applies, and the answer is mailed to her mailbox only."""

import importlib

import boto3
from botocore.stub import Stubber

from shared import chat, chat_email
from shared.enums import ChatVia, EmailedQuestionOutcome, GuardrailOutcome
from tests.test_chat import chat_on, message_rows  # noqa: F401
from tests.test_verdict import RECEIVER_USER_ID, aws  # noqa: F401

RECEIVER_MAILBOX = "grandma.real@family.example"
PIDGE_ADDRESS = "pidge@sendpidge.example"
QUESTION = "Was that email from Jimin really him?"


def raw_email(sender: str = f"Grandma <{RECEIVER_MAILBOX}>", auth_headers: tuple[str, ...] = ("spf=pass;dkim=pass",),
              signing_domain: str = "family.example", body: str = QUESTION, subject: str = "A question") -> bytes:
    header_lines = [f"From: {sender}", f"To: {PIDGE_ADDRESS}", f"Subject: {subject}",
                    f"DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed;\r\n d={signing_domain}; s=google; h=from:to; b=abc"]
    header_lines += [f"X-Pidge-Auth: {value}" for value in auth_headers]
    return ("\r\n".join(header_lines) + "\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + body + "\r\n").encode()


def test_her_mailbox_with_aligned_dkim_writes_the_same_row_as_the_window(chat_on):  # noqa: F811
    body = (f"{QUESTION}\r\n\r\n-- \r\nGrandma\r\n\r\nOn Sat, Oct 24, 2026 at 3:00 PM Pidge <{PIDGE_ADDRESS}>\r\nwrote:\r\n"
            "> I can only help with your own emails and cases.")
    outcome = chat_email.accept_emailed_question(raw_email(body=body), RECEIVER_MAILBOX, RECEIVER_USER_ID)
    assert outcome is EmailedQuestionOutcome.ACCEPTED
    [emailed_row] = message_rows(RECEIVER_USER_ID)
    window_row = chat.accept_question(RECEIVER_USER_ID, QUESTION, ChatVia.WINDOW).message_row
    assert (emailed_row["PK"], emailed_row["role"], emailed_row["via"], emailed_row["text"]) == ("CHAT#grandma", "grandma", "email", QUESTION)
    assert emailed_row["emailSubject"] == "A question"
    assert set(emailed_row) - {"emailSubject"} == set(window_row)


def test_quoted_history_on_one_line_is_cut_and_an_empty_body_falls_back_to_the_subject():
    assert chat_email.strip_quoted_history("Is it safe?\nOn Sat, Oct 24, 2026, Pidge wrote:\n> old") == "Is it safe?"
    emailed = chat_email.read_emailed_question(raw_email(body="", subject="Was that Jimin?"), RECEIVER_MAILBOX)
    assert (emailed.outcome, emailed.question_text) == (EmailedQuestionOutcome.ACCEPTED, "Was that Jimin?")


def test_any_other_sender_or_unproven_mail_writes_nothing(chat_on):  # noqa: F811
    cases = [
        (raw_email(sender="Grandma <someone.else@family.example>"), EmailedQuestionOutcome.NOT_FROM_RECEIVER),
        (raw_email(auth_headers=("spf=pass;dkim=fail",)), EmailedQuestionOutcome.DKIM_NOT_PASSED),
        (raw_email(auth_headers=()), EmailedQuestionOutcome.DKIM_NOT_PASSED),
        (raw_email(auth_headers=("spf=pass;dkim=pass", "spf=pass;dkim=pass")), EmailedQuestionOutcome.DKIM_NOT_PASSED),
        (raw_email(signing_domain="family-example.20230601.gappssmtp.com"), EmailedQuestionOutcome.DKIM_NOT_ALIGNED),
    ]
    for raw, expected_outcome in cases:
        assert chat_email.accept_emailed_question(raw, RECEIVER_MAILBOX, RECEIVER_USER_ID) is expected_outcome
    assert message_rows(RECEIVER_USER_ID) == []


def test_the_hourly_cap_and_the_chat_flag_also_drop_emailed_questions(aws, monkeypatch):  # noqa: F811
    assert chat_email.accept_emailed_question(raw_email(), RECEIVER_MAILBOX, RECEIVER_USER_ID) is EmailedQuestionOutcome.REFUSED
    monkeypatch.setenv("CHAT_ENABLED", "true")
    for _ in range(chat.QUESTIONS_PER_HOUR_LIMIT):
        assert chat_email.accept_emailed_question(raw_email(), RECEIVER_MAILBOX, RECEIVER_USER_ID) is EmailedQuestionOutcome.ACCEPTED
    assert chat_email.accept_emailed_question(raw_email(), RECEIVER_MAILBOX, RECEIVER_USER_ID) is EmailedQuestionOutcome.REFUSED
    assert len(message_rows(RECEIVER_USER_ID)) == chat.QUESTIONS_PER_HOUR_LIMIT


def test_an_emailed_question_is_answered_by_mail_to_her_mailbox_only(chat_on, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RECEIVER_USER_ID", RECEIVER_USER_ID)
    monkeypatch.setenv("CASE_AGENT_RUNTIME", "lambda")
    monkeypatch.setenv("CASE_AGENT_MODEL", "haiku-4.5")
    dispatch = importlib.import_module("pidge_dispatch.handler")
    monkeypatch.setattr(dispatch, "RECEIVER_MAILBOX", RECEIVER_MAILBOX)
    monkeypatch.setattr(dispatch, "RECEIPT_SENDER", PIDGE_ADDRESS)
    monkeypatch.setattr(dispatch, "check_question_with_guardrail", lambda _question: GuardrailOutcome.PASSED)
    monkeypatch.setattr(dispatch, "ask_chat_agent", lambda _user_id, _question: "Jimin said it was not him.")
    pushed_messages = []
    monkeypatch.setattr(dispatch.channels, "push_to_user", lambda user_id, message: pushed_messages.append(message) or 1)
    chat_email.accept_emailed_question(raw_email(), RECEIVER_MAILBOX, RECEIVER_USER_ID)
    [question_row] = message_rows(RECEIVER_USER_ID)
    ses_client = boto3.client("sesv2", region_name="us-east-1")
    monkeypatch.setattr(dispatch, "_ses_client", ses_client)
    with Stubber(ses_client) as stubber:
        stubber.add_response("send_email", {"MessageId": "m1"}, {
            "FromEmailAddress": PIDGE_ADDRESS,
            "Destination": {"ToAddresses": [RECEIVER_MAILBOX]},
            "Content": {"Simple": {"Subject": {"Data": "Re: A question"}, "Body": {"Text": {"Data": "Jimin said it was not him."}}}},
        })
        dispatch.answer_and_push_question(RECEIVER_USER_ID, question_row["SK"])
        stubber.assert_no_pending_responses()
    assert pushed_messages == []
    reply_rows = [row for row in message_rows(RECEIVER_USER_ID) if row["role"] == "pidge"]
    assert [(row["via"], row["inReplyTo"]) for row in reply_rows] == [("email", question_row["SK"])]
