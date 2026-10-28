"""Case actions. The app sends only Done (close_case): Pidge drafts and Grandma reports to her bank herself (2026-10-28).
send_report, send_follow_up and record_bank_reply are the retired mock-bank loop: no route calls send_report, so no reply
token or follow-up is issued any more; pidge_case_inbound and pidge_case_followup stay deployed until they are removed."""

import hmac
import logging
import re
import secrets
from dataclasses import dataclass

from shared import graph, refund_rules
from shared.enums import ActionOutcome, CaseState, Jurisdiction, PaymentRail, ReasonCode
from shared.profiles import ReceiverProfile

logger = logging.getLogger(__name__)

REPLY_TOKEN_BYTES = 12
REPLY_TOKEN_PATTERN = re.compile(r"\[([A-Za-z0-9_-]{8,40})\]")
REPLY_COUNT_CAP = 10
REPLY_TEXT_MAX_CHARACTERS = 4_000
REPORT_SUBJECT_PREFIX = "Pidge case"
SIMULATED_BANK_LABEL = "SIMULATED"  # on every report so the mock mailbox is never mistaken for a real bank
OPEN_OR_REPORTED = (CaseState.OPEN, CaseState.REPORTED)


@dataclass(frozen=True)
class CaseActionResult:
    outcome: ActionOutcome
    case_row: dict | None = None


def report_subject(reply_token: str) -> str:
    return f"{REPORT_SUBJECT_PREFIX} [{reply_token}]"


def parse_reply_token(subject: str) -> str | None:
    """The token travels in the subject; a reply keeps it inside the brackets."""
    match = REPLY_TOKEN_PATTERN.search(subject or "")
    return match.group(1) if match else None


def build_report_body(case_row: dict, receiver_name: str) -> str:
    """Case facts and the live rule's steps only: nothing from the email, nothing from the model."""
    rule = refund_rules.find_live_rule(Jurisdiction(case_row["jurisdiction"]), PaymentRail(case_row["paymentRail"]))
    lines = [
        f"[{SIMULATED_BANK_LABEL}] Pidge is reporting a payment made under a family impersonation scam.",
        "",
        f"Customer: {receiver_name}",
        f"Payment method: {case_row['paymentRail']}",
        f"Bank or card: {case_row['bank']}",
        f"Amount: {case_row['amount']} {case_row['currency']}",
        f"Paid on: {case_row['paidAt']}",
        f"Payee hint: {case_row.get('payeeHint') or 'none given'}",
        f"Pidge case id: {case_row['PK'].removeprefix('CASE#')}",
        "",
        "What we are asking for:",
    ]
    if rule is None:
        lines.append("- Please open a fraud claim; Pidge has no published steps for this country and payment method yet.")
    else:
        lines.extend(f"- {step.action} ({step.contact})" for step in rule.steps)
    lines.extend(["", "Please reply to this email; the reply reaches the customer through Pidge."])
    return "\n".join(lines)


def read_residents_case(case_id: str, caller_user_id: str, resident: ReceiverProfile) -> tuple[CaseActionResult | None, dict | None]:
    """(None, the case row) when the case exists and both the caller and the case's receiverEmail are this resident's; else
    (the refusal, None). Another resident's case is NOT_ALLOWED and its row is never returned."""
    if not hmac.compare_digest(caller_user_id, resident.receiver_user_id):
        return CaseActionResult(ActionOutcome.NOT_ALLOWED), None
    case_row = graph.get_case(case_id)
    if case_row is None:
        return CaseActionResult(ActionOutcome.UNKNOWN_REQUEST), None
    if case_row["receiverEmail"] != resident.receiver_address:
        return CaseActionResult(ActionOutcome.NOT_ALLOWED), None
    return None, case_row


def send_report(case_id: str, caller_user_id: str, resident: ReceiverProfile, ses_client, sender_address: str,
                reply_to_address: str, mock_bank_mailbox: str) -> CaseActionResult:
    """The resident's own case only. Token and CASETOKEN# first, then the send, then open→reported. A failed send leaves
    open with the token kept."""
    refusal, case_row = read_residents_case(case_id, caller_user_id, resident)
    if refusal is not None:
        return refusal
    if case_row["status"] != CaseState.OPEN:
        return CaseActionResult(ActionOutcome.NOT_APPLIED, case_row)
    reply_token = case_row.get("replyToken") or secrets.token_urlsafe(REPLY_TOKEN_BYTES)
    graph.write_case_token(reply_token, case_id)
    report_body = build_report_body(case_row, resident.resident_name)
    graph.update_case(case_id, {
        "UpdateExpression": "SET replyToken = :token, reportDraft = :draft",
        "ExpressionAttributeValues": {":token": reply_token, ":draft": report_body},
    })
    ses_client.send_email(
        FromEmailAddress=sender_address,
        Destination={"ToAddresses": [mock_bank_mailbox]},
        ReplyToAddresses=[reply_to_address],
        Content={"Simple": {"Subject": {"Data": report_subject(reply_token)},
                            "Body": {"Text": {"Data": report_body}}}},
    )
    applied = graph.update_case(case_id, {
        "UpdateExpression": "SET #status = :reported, reportSentAt = :now, reportedTo = :to, replyCount = :zero",
        "ConditionExpression": "#status = :open",
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":reported": str(CaseState.REPORTED), ":open": str(CaseState.OPEN),
                                      ":now": graph.now_timestamp(), ":to": mock_bank_mailbox.casefold(), ":zero": 0},
    })
    if not applied:
        logger.warning("case %s: report sent but the status write was refused; the token still matches the reply", case_id)
    return CaseActionResult(ActionOutcome.RECORDED, graph.get_case(case_id))


def send_follow_up(case_id: str, ses_client, sender_address: str, reply_to_address: str) -> CaseActionResult:
    """Exactly one reminder, and only while the bank is silent: the guard is the conditional write, taken before the send."""
    case_row = graph.get_case(case_id)
    if case_row is None:
        return CaseActionResult(ActionOutcome.UNKNOWN_REQUEST)
    claimed = graph.update_case(case_id, {
        "UpdateExpression": "SET followUpSentAt = :now",
        "ConditionExpression": "#status = :reported AND attribute_not_exists(bankRepliedAt) AND attribute_not_exists(followUpSentAt)",
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":reported": str(CaseState.REPORTED), ":now": graph.now_timestamp()},
    })
    if not claimed:
        return CaseActionResult(ActionOutcome.NOT_APPLIED, case_row)
    ses_client.send_email(
        FromEmailAddress=sender_address,
        Destination={"ToAddresses": [case_row["reportedTo"]]},
        ReplyToAddresses=[reply_to_address],
        Content={"Simple": {"Subject": {"Data": f"Re: {report_subject(case_row['replyToken'])}"},
                            "Body": {"Text": {"Data": f"[{SIMULATED_BANK_LABEL}] A reminder from Pidge: the report below has had no reply yet.\n\n"
                                                      + case_row["reportDraft"]}}}},
    )
    return CaseActionResult(ActionOutcome.RECORDED, graph.get_case(case_id))


@dataclass(frozen=True)
class InboundCaseReply:
    s3_key: str
    sender_address: str
    subject: str
    text_body: str
    auth_reasons: tuple[ReasonCode, ...]


def record_bank_reply(reply: InboundCaseReply) -> CaseActionResult:
    """Accepts only a tokened reply from the reported address with passing auth on an open or reported case."""
    reply_token = parse_reply_token(reply.subject)
    case_id = graph.find_case_id_for_token(reply_token) if reply_token else None
    case_row = graph.get_case(case_id) if case_id else None
    if case_row is None:
        return CaseActionResult(ActionOutcome.UNKNOWN_REQUEST)
    if case_row["status"] not in OPEN_OR_REPORTED or int(case_row.get("replyCount", 0)) >= REPLY_COUNT_CAP:
        return CaseActionResult(ActionOutcome.NOT_APPLIED, case_row)
    if ReasonCode.AUTH_FAILED in reply.auth_reasons or ReasonCode.AUTH_TAMPERED in reply.auth_reasons:
        return CaseActionResult(ActionOutcome.WRONG_SENDER, case_row)
    if not hmac.compare_digest(reply.sender_address.casefold(), str(case_row.get("reportedTo", "")).casefold()):
        return CaseActionResult(ActionOutcome.WRONG_SENDER, case_row)
    received_at = graph.now_timestamp()
    stored = graph.write_case_reply(case_id, reply.s3_key, {
        "from": reply.sender_address, "receivedAt": received_at,
        "textContent": reply.text_body[:REPLY_TEXT_MAX_CHARACTERS], "auth": [str(reason) for reason in reply.auth_reasons],
    })
    if not stored:
        return CaseActionResult(ActionOutcome.NOT_APPLIED, case_row)
    graph.update_case(case_id, {
        "UpdateExpression": "SET bankRepliedAt = if_not_exists(bankRepliedAt, :now), replyCount = if_not_exists(replyCount, :zero) + :one",
        "ExpressionAttributeValues": {":now": received_at, ":zero": 0, ":one": 1},
    })
    return CaseActionResult(ActionOutcome.RECORDED, graph.get_case(case_id))


def close_case(case_id: str, caller_user_id: str, resident: ReceiverProfile) -> CaseActionResult:
    """Done: open (or a legacy reported case) → closed, by the case's own resident only."""
    refusal, case_row = read_residents_case(case_id, caller_user_id, resident)
    if refusal is not None:
        return refusal
    applied = graph.update_case(case_id, {
        "UpdateExpression": "SET #status = :closed, closedAt = :now",
        "ConditionExpression": "#status IN (:open, :reported)",
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":closed": str(CaseState.CLOSED), ":open": str(CaseState.OPEN), ":reported": str(CaseState.REPORTED),
                                      ":now": graph.now_timestamp()},
    })
    if not applied:
        return CaseActionResult(ActionOutcome.NOT_APPLIED, case_row)
    return CaseActionResult(ActionOutcome.RECORDED, graph.get_case(case_id))
