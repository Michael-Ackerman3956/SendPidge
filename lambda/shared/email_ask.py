"""The ask by email: SES sends 'Was this you?' to the contact's own address (CONTACT#.contactEmail) with a signed
'No' link (shared/ask_link.py, pidge_ask_link); a reply of NO also comes back to pidge@ through Mail Manager (rule
store-ask-reply, S3 asks/) into pidge-verdict.

A Yes by email never counts (verdict.record_answer refuses it: the mailbox is the channel scammers hijack), so this channel
can only keep a hold, never lift one. A reply counts only when Mail Manager stamped dkim=pass and a DKIM-Signature is signed
by the From address's own domain, the same proof pidge-question-inbound asks of Grandma.
"""

import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr

from shared.chat_email import DKIM_SIGNATURE_HEADER_NAME, has_dkim_pass, signing_domains, strip_quoted_history
from shared.email_parse import PIDGE_AUTH_HEADER_NAME, extract_text_body
from shared.enums import EmailedAnswerOutcome, VerdictAnswer

ASK_SUBJECT_PREFIX = "Pidge: was this you?"
ASK_SUBJECT_MARKER = "[req-"  # Mail Manager routes a reply by this substring of the subject (infra/lib/mail-ingress.ts)
REQUEST_ID_PATTERN = re.compile(r"\[(req-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\]")
FIRST_WORD_PATTERN = re.compile(r"[A-Za-z]+")
ANSWER_FOR_WORD = {"no": VerdictAnswer.NO, "yes": VerdictAnswer.YES}


@dataclass(frozen=True)
class EmailedAnswer:
    """One reply under asks/ after the checks; request id, answer and sender are set only when ACCEPTED."""

    outcome: EmailedAnswerOutcome
    request_id: str = ""
    answer: VerdictAnswer | None = None
    sender_address: str = ""
    message_id: str = ""


def ask_subject(request_id: str) -> str:
    return f"{ASK_SUBJECT_PREFIX} [{request_id}]"


def ask_body(summary: str, receiver_name: str, no_link: str, app_url: str) -> str:
    """The Yes line only opens the app (no token in the mail): a hijacked mailbox must not be able to release the email."""
    return (
        f"{receiver_name} received an email that claims to come from you. Pidge summarised it as: {summary}\n\n"
        "If you did NOT send it, open this link and press the button:\n"
        f"{no_link}\n"
        "or reply to this email with one word: NO\n\n"
        "If you did send it, answer Yes in the SendPidge app or on WhatsApp:\n"
        f"{app_url}/\n"
        "A Yes by email is not accepted, because a hijacked mailbox could send one.\n\n"
        "Pidge"
    )


def reply_text_of(message: EmailMessage) -> str:
    """What the contact typed above the quoted ask, plain part first so the quote marker is found."""
    plain_part = message.get_body(preferencelist=("plain",))
    body_text = plain_part.get_content() if plain_part is not None else extract_text_body(message)
    return strip_quoted_history(body_text.replace("\r\n", "\n"))


def parse_answer_word(reply_text: str) -> VerdictAnswer | None:
    """The first word decides: NO or YES (any case); anything else is not an answer."""
    first_word = FIRST_WORD_PATTERN.search(reply_text)
    return ANSWER_FOR_WORD.get(first_word.group(0).casefold()) if first_word else None


def read_emailed_answer(raw_email: bytes) -> EmailedAnswer:
    message = message_from_bytes(raw_email, policy=policy.default)
    request_id_match = REQUEST_ID_PATTERN.search(str(message.get("Subject", "")))
    if request_id_match is None:
        return EmailedAnswer(EmailedAnswerOutcome.NO_REQUEST_ID)
    if not has_dkim_pass(message.get_all(PIDGE_AUTH_HEADER_NAME, [])):
        return EmailedAnswer(EmailedAnswerOutcome.DKIM_NOT_PASSED)
    sender_address = parseaddr(str(message.get("From", "")))[1]
    sender_domain = sender_address.rpartition("@")[2].lower()
    if not sender_domain or sender_domain not in signing_domains(message.get_all(DKIM_SIGNATURE_HEADER_NAME, [])):
        return EmailedAnswer(EmailedAnswerOutcome.DKIM_NOT_ALIGNED)
    answer = parse_answer_word(reply_text_of(message))
    if answer is None:
        return EmailedAnswer(EmailedAnswerOutcome.NOT_AN_ANSWER)
    message_id = str(message.get("Message-ID", "")).strip()
    return EmailedAnswer(EmailedAnswerOutcome.ACCEPTED, request_id_match.group(1), answer, sender_address, message_id)
