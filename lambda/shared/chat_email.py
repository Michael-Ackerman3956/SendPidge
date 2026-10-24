"""Grandma's questions mailed to pidge@: proven to come from her real mailbox, turned into the same CHAT# row as the
window, and answered by email to that mailbox only.

Mail Manager's SPF or DKIM pass alone does not prove the From header, so a question counts only when DKIM passed and a
DKIM-Signature is signed by her mailbox's own domain.
"""

import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr

from shared import chat
from shared.auth import PASS_VALUE, parse_authentication_header
from shared.email_parse import PIDGE_AUTH_HEADER_NAME, collapse_whitespace, extract_text_body
from shared.enums import ChatVia, EmailedQuestionOutcome

DKIM_SIGNATURE_HEADER_NAME = "DKIM-Signature"
EMAIL_SUBJECT_MAX_CHARACTERS = 200
REPLY_SUBJECT_FALLBACK = "Pidge's answer"
# A reply quotes the earlier mail under "On <date>, <name> wrote:" (Gmail may wrap that line); a signature starts at "-- ".
QUOTED_HISTORY_PATTERN = re.compile(r"(^|\n)\s*On [^\n]{5,200}?(\n[^\n]{0,200}?)?wrote:\s*(\n|$)")
SIGNATURE_DELIMITER_PATTERN = re.compile(r"\n-- ?\n")


@dataclass(frozen=True)
class EmailedQuestion:
    """One pidge@ email after the checks: the outcome, and the question text and subject when it was accepted."""

    outcome: EmailedQuestionOutcome
    question_text: str = ""
    subject: str = ""


def signing_domains(dkim_signature_values: list[str]) -> list[str]:
    """The d= tag of every DKIM-Signature header, lowercased."""
    domains = []
    for signature in dkim_signature_values:
        for tag in str(signature).split(";"):
            name, separator, value = tag.strip().partition("=")
            if separator and name.strip().lower() == "d":
                domains.append(value.strip().lower())
    return domains


def has_dkim_pass(pidge_auth_header_values: list[str]) -> bool:
    """Exactly one Mail Manager stamp, and it says dkim=pass (SPF alone never vouches for the From header)."""
    if len(pidge_auth_header_values) != 1:
        return False
    return parse_authentication_header(str(pidge_auth_header_values[0])).get("dkim") == PASS_VALUE


def strip_quoted_history(plain_text: str) -> str:
    """Keeps only what she wrote above the quoted earlier mail and above her signature."""
    question_text = QUOTED_HISTORY_PATTERN.split(plain_text, maxsplit=1)[0]
    question_text = SIGNATURE_DELIMITER_PATTERN.split(question_text, maxsplit=1)[0]
    return collapse_whitespace(question_text)


def question_text_of(message: EmailMessage, subject: str) -> str:
    """The new text of the body (plain part kept line by line so the quote marker is found); the subject when empty."""
    plain_part = message.get_body(preferencelist=("plain",))
    body_text = plain_part.get_content() if plain_part is not None else extract_text_body(message)
    return strip_quoted_history(body_text.replace("\r\n", "\n")) or subject


def read_emailed_question(raw_email: bytes, receiver_mailbox: str) -> EmailedQuestion:
    message = message_from_bytes(raw_email, policy=policy.default)
    sender_address = parseaddr(str(message.get("From", "")))[1]
    if sender_address.casefold() != receiver_mailbox.casefold():
        return EmailedQuestion(EmailedQuestionOutcome.NOT_FROM_RECEIVER)
    if not has_dkim_pass(message.get_all(PIDGE_AUTH_HEADER_NAME, [])):
        return EmailedQuestion(EmailedQuestionOutcome.DKIM_NOT_PASSED)
    mailbox_domain = receiver_mailbox.rpartition("@")[2].lower()
    if mailbox_domain not in signing_domains(message.get_all(DKIM_SIGNATURE_HEADER_NAME, [])):
        return EmailedQuestion(EmailedQuestionOutcome.DKIM_NOT_ALIGNED)
    subject = collapse_whitespace(str(message.get("Subject", "")))[:EMAIL_SUBJECT_MAX_CHARACTERS]
    return EmailedQuestion(EmailedQuestionOutcome.ACCEPTED, question_text_of(message, subject), subject)


def accept_emailed_question(raw_email: bytes, receiver_mailbox: str, user_id: str) -> EmailedQuestionOutcome:
    """The sender checks, then the same flag, length and hourly cap as the window; only ACCEPTED writes the row."""
    emailed_question = read_emailed_question(raw_email, receiver_mailbox)
    if emailed_question.outcome is not EmailedQuestionOutcome.ACCEPTED:
        return emailed_question.outcome
    accepted = chat.accept_question(user_id, emailed_question.question_text, ChatVia.EMAIL,
                                    {"emailSubject": emailed_question.subject})
    return EmailedQuestionOutcome.ACCEPTED if accepted.message_row else EmailedQuestionOutcome.REFUSED


def reply_subject(question_subject: str) -> str:
    if not question_subject:
        return REPLY_SUBJECT_FALLBACK
    return question_subject if question_subject.lower().startswith("re:") else f"Re: {question_subject}"


def send_reply_email(ses_client, sender_address: str, receiver_mailbox: str, question_row: dict, reply_row: dict) -> None:
    """Pidge's answer to her real mailbox only; raises when SES refuses (the stream retry logs it)."""
    ses_client.send_email(
        FromEmailAddress=sender_address,
        Destination={"ToAddresses": [receiver_mailbox]},
        Content={"Simple": {"Subject": {"Data": reply_subject(question_row.get("emailSubject", ""))},
                            "Body": {"Text": {"Data": reply_row["text"]}}}},
    )
