"""A contact's Yes / No and the receiver's Confirm. No beats Yes; the first valid tap wins. Never reads the email."""

import hmac
import logging
from dataclasses import dataclass

from shared import graph
from shared.enums import ActionOutcome, Channel, RequestState, VerdictAnswer

logger = logging.getLogger(__name__)

QUARANTINE_TAG = {"Key": "quarantine", "Value": "true"}
CONTACT_FIELD_FOR_CHANNEL = {
    Channel.INAPP: "contactPwaUserId",
    Channel.WHATSAPP: "contactWhatsApp",
    Channel.RCS: "contactRcs",
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


def build_answer_update(answer: VerdictAnswer, sender: SenderIdentity, inbound_message_id: str, now: str) -> dict:
    """A No lands at any time unless already denied; a Yes needs pending and not expired. Same id twice never lands."""
    new_status = RequestState.DENIED if answer is VerdictAnswer.NO else RequestState.CONFIRMED
    values = {
        ":newStatus": str(new_status),
        ":answer": str(answer),
        ":channel": str(sender.channel),
        ":now": now,
        ":messageId": inbound_message_id,
        ":messageIdList": [inbound_message_id],
    }
    if answer is VerdictAnswer.NO:
        condition = "#status <> :newStatus"
    else:
        condition = "#status = :pending AND expiresAt > :now"
        values[":pending"] = str(RequestState.PENDING)
    return {
        "UpdateExpression": (
            "SET #status = :newStatus, #response = :answer, repliedVia = :channel, resolvedAt = :now, "
            "inboundMessageIds = list_append(inboundMessageIds, :messageIdList)"
        ),
        "ConditionExpression": f"attribute_exists(PK) AND {condition} AND NOT contains(inboundMessageIds, :messageId)",
        "ExpressionAttributeNames": {"#status": "status", "#response": "response"},
        "ExpressionAttributeValues": values,
    }


def is_expected_sender(contact_row: dict | None, sender: SenderIdentity) -> bool:
    expected_value = (contact_row or {}).get(CONTACT_FIELD_FOR_CHANNEL[sender.channel])
    return bool(expected_value) and hmac.compare_digest(str(expected_value), sender.value)


def record_answer(
    request_id: str, answer: VerdictAnswer, sender: SenderIdentity, inbound_message_id: str, now: str
) -> ActionResult:
    """now must come from graph.iso_timestamp so it compares with expiresAt as a string."""
    request_row = graph.get_request(request_id)
    if request_row is None:
        return ActionResult(ActionOutcome.UNKNOWN_REQUEST)
    contact_row = graph.get_contact_row(request_row["receiverEmail"], request_row["contactSK"])
    if not is_expected_sender(contact_row, sender):
        logger.warning("answer on %s from an unexpected %s sender", request_id, sender.channel)
        return ActionResult(ActionOutcome.WRONG_SENDER, request_row)
    if not graph.write_answer(request_id, build_answer_update(answer, sender, inbound_message_id, now)):
        logger.info("answer %s on %s not applied (duplicate, late Yes, or already denied)", answer, request_id)
        return ActionResult(ActionOutcome.NOT_APPLIED, request_row, contact_row)
    status = RequestState.DENIED if answer is VerdictAnswer.NO else RequestState.CONFIRMED
    return ActionResult(ActionOutcome.RECORDED, request_row, contact_row, status=status)


def receipt_text(status: RequestState, receiver_name: str) -> tuple[str, str]:
    if status is RequestState.DENIED:
        return "Pidge: your answer was recorded", f"You told Pidge you did not send the email to {receiver_name}. It stays held."
    return "Pidge: your answer was recorded", f"You told Pidge you sent the email to {receiver_name}. It was released."


def send_receipt(ses_client, sender_address: str, contact_row: dict, status: RequestState, receiver_name: str) -> bool:
    """Emails the contact's own address, never the monitored one. False (logged) when there is no address."""
    contact_email = contact_row.get("contactEmail")
    if not contact_email:
        logger.warning("no contactEmail for %s; receipt not sent", contact_row["SK"])
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
    graph.write_listed_address(email_row["senderAddress"], "Reported after the real contact said they did not send it")
    return ActionResult(ActionOutcome.RECORDED, request_row, email_row=email_row, status=RequestState.DENIED)
