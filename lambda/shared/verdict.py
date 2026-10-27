"""A contact's Yes / No / Undo / Reverse and the receiver's Confirm. No beats Yes; the first valid tap wins; a Yes never counts
on the channel the suspicious message arrived on (today: email). Never reads the email."""

import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from shared import graph, reach_points, refund_rules
from shared.enums import (
    FINAL_REQUEST_STATES, PHONE_CHANNEL_ANSWERS, ActionOutcome, Channel, Jurisdiction, PaymentRail, RequestState, VerdictAnswer,
)

logger = logging.getLogger(__name__)

RELEASE_DELAY_SECONDS = 10  # a Yes waits this long for an Undo before the email is released
REVERSE_WINDOW_SECONDS = 5 * 60  # a No can be taken back for this long
QUARANTINE_TAG = {"Key": "quarantine", "Value": "true"}
REPORTABLE_REQUEST_STATES = (RequestState.DENIED, RequestState.TIMEOUT)  # "I already paid" only after a No or a silence
PAYEE_HINT_MAX_LENGTH = 40
BANK_NAME_MAX_LENGTH = 60
STATUS_FOR_ANSWER = {
    VerdictAnswer.YES: RequestState.CONFIRMED_PENDING,
    VerdictAnswer.NO: RequestState.DENIED,
    VerdictAnswer.UNDO: RequestState.DENIED,
    VerdictAnswer.REVERSE: RequestState.REVERSED_NO,
}


@dataclass(frozen=True)
class SenderIdentity:
    """Who a reply came from, as the channel itself reports it (never from the message body)."""

    channel: Channel
    value: str  # PWA user id, WhatsApp number or RCS number


@dataclass(frozen=True)
class ActionResult:
    outcome: ActionOutcome
    request_row: dict | None = None
    contact_row: dict | None = None
    email_row: dict | None = None
    status: RequestState | None = None


def parse_phone_answer(postback_text: str, separator: str) -> tuple[str, VerdictAnswer] | None:
    """'<request id><separator><answer>' from an RCS chip or WhatsApp template button; None for undo and reverse (a WhatsApp
    reverse arrives as a reply-button id instead, see whatsapp.parse_reverse_button_id).

    Raises ValueError on an unknown answer, which the channel parsers treat as "not a tap".
    """
    request_id, answer_value = postback_text.rsplit(separator, 1)
    answer = VerdictAnswer(answer_value)
    return (request_id, answer) if answer in PHONE_CHANNEL_ANSWERS else None


def parse_timestamp(iso_text: str) -> datetime:
    return datetime.strptime(iso_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def release_time_for(now: str) -> str:
    return graph.iso_timestamp(parse_timestamp(now) + timedelta(seconds=RELEASE_DELAY_SECONDS))


def effective_status(request_row: dict, now: str) -> RequestState:
    """A confirmed_pending past its releaseAt reads as confirmed, so a missed release never leaves a hold stuck."""
    status = RequestState(request_row["status"])
    if status is RequestState.CONFIRMED_PENDING and request_row.get("releaseAt", "") <= now:
        return RequestState.CONFIRMED
    return status


def answer_condition(answer: VerdictAnswer, now: str, values: dict) -> str:
    """Yes: pending and not expired. No: anything but denied. Undo: still waiting to release. Reverse: a recent No."""
    if answer is VerdictAnswer.YES:
        values[":pending"] = str(RequestState.PENDING)
        return "#status = :pending AND expiresAt > :now"
    if answer is VerdictAnswer.NO:
        return "#status <> :newStatus"
    if answer is VerdictAnswer.UNDO:
        values[":confirmedPending"] = str(RequestState.CONFIRMED_PENDING)
        return "#status = :confirmedPending AND releaseAt > :now"
    values[":denied"] = str(RequestState.DENIED)
    values[":reverseDeadline"] = graph.iso_timestamp(parse_timestamp(now) - timedelta(seconds=REVERSE_WINDOW_SECONDS))
    return "#status = :denied AND resolvedAt >= :reverseDeadline"


def build_answer_update(answer: VerdictAnswer, sender: SenderIdentity, inbound_message_id: str, now: str) -> dict:
    """One conditional update per answer; the same message id twice never lands."""
    new_status = STATUS_FOR_ANSWER[answer]
    values = {
        ":newStatus": str(new_status),
        ":answer": str(answer),
        ":channel": str(sender.channel),
        ":now": now,
        ":messageId": inbound_message_id,
        ":messageIdList": [inbound_message_id],
    }
    condition = answer_condition(answer, now, values)
    release_clause = ""
    if answer is VerdictAnswer.YES:
        values[":releaseAt"] = release_time_for(now)
        release_clause = ", releaseAt = :releaseAt"
    return {
        "UpdateExpression": (
            "SET #status = :newStatus, #response = :answer, repliedVia = :channel, resolvedAt = :now, "
            f"inboundMessageIds = list_append(inboundMessageIds, :messageIdList){release_clause}"
        ),
        "ConditionExpression": f"attribute_exists(PK) AND {condition} AND NOT contains(inboundMessageIds, :messageId)",
        "ExpressionAttributeNames": {"#status": "status", "#response": "response"},
        "ExpressionAttributeValues": values,
    }


def is_expected_sender(contact_row: dict | None, sender: SenderIdentity) -> bool:
    """The contact's own PWA user, or one of the addresses Pidge may ask on that channel, exactly; an email address compares
    case-insensitively."""
    if contact_row is None:
        return False
    if sender.channel is Channel.INAPP:
        return hmac.compare_digest(str(contact_row["contactPwaUserId"]), sender.value)
    askable_addresses = reach_points.askable_addresses(reach_points.reach_points_of(contact_row), sender.channel)
    if sender.channel is Channel.EMAIL:
        return any(hmac.compare_digest(address.casefold(), sender.value.casefold()) for address in askable_addresses)
    return any(hmac.compare_digest(address, sender.value) for address in askable_addresses)


def inbound_channel_of(request_row: dict) -> Channel:
    """Rows written before 2026-10-26 carry no inboundChannel; email was the only inbound channel then."""
    return Channel(request_row.get("inboundChannel", Channel.EMAIL))


def record_answer(
    request_id: str, answer: VerdictAnswer, sender: SenderIdentity, inbound_message_id: str, now: str
) -> ActionResult:
    """now must come from graph.iso_timestamp so it compares with expiresAt / releaseAt / resolvedAt as a string."""
    request_row = graph.get_request(request_id)
    if request_row is None:
        return ActionResult(ActionOutcome.UNKNOWN_REQUEST)
    contact_row = graph.get_contact_row(request_row["receiverEmail"], request_row["contactSK"])
    if not is_expected_sender(contact_row, sender):
        logger.warning("answer on %s from an unexpected %s sender", request_id, sender.channel)
        return ActionResult(ActionOutcome.WRONG_SENDER, request_row)
    if answer is VerdictAnswer.YES and sender.channel is inbound_channel_of(request_row):
        logger.warning("a Yes on %s by %s, the channel the message came in on, is refused", request_id, sender.channel)
        return ActionResult(ActionOutcome.WRONG_CHANNEL, request_row, contact_row)
    if not graph.write_answer(request_id, build_answer_update(answer, sender, inbound_message_id, now)):
        logger.info("answer %s on %s not applied (duplicate, late, or wrong state)", answer, request_id)
        return ActionResult(ActionOutcome.NOT_APPLIED, request_row, contact_row)
    if answer is VerdictAnswer.REVERSE:
        email_row = graph.get_email(request_row["emailId"])
        if not graph.delete_listed_address(email_row["senderAddress"], request_id):  # only the row this request's Confirm wrote
            logger.info("reverse on %s left LIST#address as it was: no Confirm row from this request", request_id)
    return ActionResult(ActionOutcome.RECORDED, graph.get_request(request_id), contact_row, status=STATUS_FOR_ANSWER[answer])


def release_if_still_pending(request_id: str) -> ActionResult:
    """The release timer fired: confirmed only if nobody tapped Undo (or No) meanwhile."""
    request_row = graph.get_request(request_id)
    if request_row is None:
        return ActionResult(ActionOutcome.UNKNOWN_REQUEST)
    applied = graph.write_answer(request_id, {
        "UpdateExpression": "SET #status = :confirmed",
        "ConditionExpression": "#status = :confirmedPending",
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":confirmed": str(RequestState.CONFIRMED), ":confirmedPending": str(RequestState.CONFIRMED_PENDING)},
    })
    if not applied:
        return ActionResult(ActionOutcome.NOT_APPLIED, request_row)
    contact_row = graph.get_contact_row(request_row["receiverEmail"], request_row["contactSK"])
    return ActionResult(ActionOutcome.RECORDED, graph.get_request(request_id), contact_row, status=RequestState.CONFIRMED)


def receipt_text(status: RequestState, receiver_name: str) -> tuple[str, str]:
    subject = "Pidge: your answer was recorded"
    if status is RequestState.DENIED:
        return subject, f"You told Pidge you did not send the email to {receiver_name}. It stays held."
    if status is RequestState.REVERSED_NO:
        return subject, f"You changed your answer: you did send the email to {receiver_name}. The hold was lifted."
    return subject, f"You told Pidge you sent the email to {receiver_name}. It was released."


def receipt_address_of(contact_row: dict) -> str | None:
    """The contact's first email, where the receipt goes; None when the row has no email."""
    return next((point.address for point in reach_points.reach_points_of(contact_row) if point.channel is Channel.EMAIL), None)


def send_receipt(ses_client, sender_address: str, contact_row: dict, status: RequestState, receiver_name: str) -> bool:
    """Emails the contact's own address, never the monitored one, for a final state only. False (logged) when there is no address."""
    if status not in FINAL_REQUEST_STATES:
        return False  # confirmed_pending: the receipt follows the release
    contact_email = receipt_address_of(contact_row)
    if contact_email is None:
        logger.warning("no email for %s; receipt not sent", contact_row["SK"])
        return False
    subject, body = receipt_text(status, receiver_name)
    ses_client.send_email(
        FromEmailAddress=sender_address,
        Destination={"ToAddresses": [contact_email]},
        Content={"Simple": {"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}}},
    )
    return True


def confirm_quarantine(
    request_id: str, confirming_user_id: str, receiver_user_id: str, receiver_address: str, s3_client, bucket_name: str
) -> ActionResult:
    """Only the receiver, only after a No: tag the raw email, mark it quarantined, list the sender's full address."""
    if not hmac.compare_digest(confirming_user_id, receiver_user_id):
        return ActionResult(ActionOutcome.NOT_ALLOWED)
    request_row = graph.get_request(request_id)
    if request_row is None:
        return ActionResult(ActionOutcome.UNKNOWN_REQUEST)
    if request_row["receiverEmail"] != receiver_address:
        return ActionResult(ActionOutcome.NOT_ALLOWED, request_row)
    if request_row["status"] != RequestState.DENIED:
        return ActionResult(ActionOutcome.NOT_APPLIED, request_row)
    email_row = graph.get_email(request_row["emailId"])
    s3_client.put_object_tagging(Bucket=bucket_name, Key=email_row["s3Key"], Tagging={"TagSet": [QUARANTINE_TAG]})
    if not graph.mark_email_quarantined(request_row["emailId"]):
        return ActionResult(ActionOutcome.NOT_APPLIED, request_row, email_row=email_row)
    graph.write_listed_address(email_row["senderAddress"], "Reported after the real contact said they did not send it", request_id)
    return ActionResult(ActionOutcome.RECORDED, request_row, email_row=email_row, status=RequestState.DENIED)


@dataclass(frozen=True)
class PaymentReport:
    """What Grandma typed on "I already paid", parsed at the boundary; raises ValueError on anything odd."""

    request_id: str
    jurisdiction: Jurisdiction
    payment_rail: PaymentRail
    bank: str
    amount: Decimal
    currency: str
    paid_at: str
    payee_hint: str

    @classmethod
    def from_client_body(cls, body: dict) -> "PaymentReport":
        amount = Decimal(str(body["amount"]))
        if amount <= 0 or not amount.is_finite():
            raise ValueError("amount must be positive")
        bank, payee_hint, currency = str(body["bank"]).strip(), str(body.get("payeeHint", "")).strip(), str(body["currency"]).strip().upper()
        if not bank or len(bank) > BANK_NAME_MAX_LENGTH or len(payee_hint) > PAYEE_HINT_MAX_LENGTH or len(currency) != 3:
            raise ValueError("bank, payee hint or currency out of shape")
        return cls(str(body["requestId"]), Jurisdiction(body["jurisdiction"]), PaymentRail(body["paymentRail"]),
                   bank, amount, currency, parse_timestamp(str(body["paidAt"])).strftime("%Y-%m-%dT%H:%M:%SZ"), payee_hint)


def report_payment(report: PaymentReport, reporting_user_id: str, receiver_user_id: str, receiver_address: str) -> ActionResult:
    """Only the receiver, only on a denied or timed-out request: writes the CASE# row (ruleGap from the live RULE# rows)."""
    if not hmac.compare_digest(reporting_user_id, receiver_user_id):
        return ActionResult(ActionOutcome.NOT_ALLOWED)
    request_row = graph.get_request(report.request_id)
    if request_row is None:
        return ActionResult(ActionOutcome.UNKNOWN_REQUEST)
    if request_row["receiverEmail"] != receiver_address:
        return ActionResult(ActionOutcome.NOT_ALLOWED, request_row)
    if request_row["status"] not in REPORTABLE_REQUEST_STATES:
        return ActionResult(ActionOutcome.NOT_APPLIED, request_row)
    rule = refund_rules.find_live_rule(report.jurisdiction, report.payment_rail)
    case_row = graph.create_case(receiver_user_id, {
        "requestId": report.request_id, "receiverEmail": receiver_address,
        "jurisdiction": str(report.jurisdiction), "paymentRail": str(report.payment_rail), "bank": report.bank,
        "amount": report.amount, "currency": report.currency, "paidAt": report.paid_at, "payeeHint": report.payee_hint,
        "ruleGap": rule is None,
    })
    return ActionResult(ActionOutcome.RECORDED, request_row, email_row=case_row, status=RequestState(request_row["status"]))
