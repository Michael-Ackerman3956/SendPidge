"""Function URL behind the PWA's CloudFront (/no/*): the 'No, that wasn't me' link from the ask by email.

GET shows one button; POST records the No through the same verdict.record_answer as every other channel. The signed
link is the proof that the contact's own mailbox received the ask; nothing here reads the email or takes a Yes. The
resident named on the page is the one the REQUEST# row's receiverEmail names; no request or no profile is a 404.
"""

import html
import logging
import os
import time

import boto3

from shared import ask_link, channels, graph, profiles, verdict
from shared.enums import ActionOutcome, AskLinkOutcome, Channel, VerdictAnswer
from shared.profiles import ReceiverProfile, UnknownReceiverError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RECEIPT_SENDER = os.environ["RECEIPT_SENDER"]
HTML_HEADERS = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}
PAGE_STYLE = ("body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1117;color:#e6edf3;"
              "font:17px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;text-align:center;padding:24px}"
              "button{font:inherit;font-weight:700;padding:14px 28px;border-radius:12px;border:0;background:#e5484d;color:#fff}")

ses_client = boto3.client("sesv2")


def handle(event, context):
    method = event["requestContext"]["http"]["method"]
    check = ask_link.verify_no_link(event.get("rawPath", ""), event.get("queryStringParameters"), int(time.time()))
    if check.outcome is AskLinkOutcome.EXPIRED:
        return page(410, "This link has expired.", "Reply NO to Pidge's email, or answer in the SendPidge app.")
    if check.outcome is not AskLinkOutcome.VALID:
        logger.warning("ask link refused: %s", check.outcome)
        return page(404, "This link is not valid.")
    if method not in ("GET", "HEAD", "POST"):
        return page(405, "This link is not valid.")
    request_row = graph.get_request(check.request_id)
    if request_row is None:
        return page(404, "This link is not valid.")
    try:
        resident = profiles.require_receiver_profile(request_row["receiverEmail"])
    except UnknownReceiverError:
        logger.warning("ask link on %s refused: its receiver has no profile", check.request_id)
        return page(404, "This link is not valid.")
    if method == "POST":
        return record_no(check, request_row, resident)
    return confirm_page(resident)


def record_no(check: ask_link.LinkCheck, request_row: dict, resident: ReceiverProfile):
    contact_row = graph.get_contact_row(request_row["receiverEmail"], request_row["contactSK"])
    contact_email = verdict.receipt_address_of(contact_row) if contact_row is not None else None
    if contact_email is None:
        logger.warning("ask link No on %s: the contact has no email", check.request_id)
        return page(404, "This link is not valid.")
    sender = verdict.SenderIdentity(Channel.EMAIL, contact_email)
    inbound_message_id = f"link:no:{check.signature[:ask_link.SIGNATURE_PREFIX_LENGTH]}"
    result = verdict.record_answer(check.request_id, VerdictAnswer.NO, sender, inbound_message_id, graph.now_timestamp())
    if result.outcome is ActionOutcome.RECORDED:
        channels.announce_recorded_answer(result, resident.receiver_user_id, resident.resident_name, RECEIPT_SENDER, ses_client)
        return page(200, "Recorded.", f"The email to {resident.resident_name} stays held.")
    if result.outcome is ActionOutcome.NOT_APPLIED:
        return page(200, "Already recorded.", f"The email to {resident.resident_name} stays held.")
    logger.warning("ask link No on %s not recorded: %s", check.request_id, result.outcome)
    return page(404, "This link is not valid.")


def confirm_page(resident: ReceiverProfile):
    body = (f"<p>{html.escape(resident.resident_name)} received an email that claims to come from you.</p>"
            '<form method="post"><button type="submit">No, that wasn\'t me</button></form>'
            "<p>If you did send it, answer Yes in the SendPidge app or on WhatsApp.</p>")
    return {"statusCode": 200, "headers": HTML_HEADERS, "body": document("Was this you?", body)}


def page(status_code: int, title: str, detail: str = ""):
    body = f"<h1>{html.escape(title)}</h1>" + (f"<p>{html.escape(detail)}</p>" if detail else "")
    return {"statusCode": status_code, "headers": HTML_HEADERS, "body": document(title, body)}


def document(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>Pidge: {html.escape(title)}</title><style>{PAGE_STYLE}</style></head><body><main>{body}</main></body></html>")
