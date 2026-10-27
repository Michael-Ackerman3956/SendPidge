"""The three drafts in Grandma's recovery pack (call script, dispute letter, police report), filled by code from the CASE# row
only, and the email that carries the pack to her own mailbox. No model output and nothing from the held email goes in."""

import logging
from decimal import Decimal

from shared.enums import PaymentRail

logger = logging.getLogger(__name__)

PAYMENT_RAIL_LABELS = {
    PaymentRail.CARD: "card payment",
    PaymentRail.ZELLE: "Zelle transfer",
    PaymentRail.WIRE: "bank transfer",
    PaymentRail.GIFT_CARD: "gift card purchase",
}
PACK_EMAIL_SUBJECT = "Pidge: your recovery pack"

CALL_SCRIPT_TEMPLATE = """Call {bank} and say:

"I need to report a {rail_label} made under false pretences. On {paid_on} I sent {amount} {payee_clause}. \
The request came by email from someone pretending to be a family member; the family member has confirmed they never sent it. \
Please stop or recall the payment if you still can, open a fraud case, and tell me the case number and what you need from me."

Write down the case number, the name of the person you spoke to, and the time of the call."""

DISPUTE_LETTER_TEMPLATE = """To {bank}, fraud department

Date: {today}

I am writing to dispute a {rail_label} of {amount} made on {paid_on}{payee_clause}.

I made this payment because of an email that pretended to come from a family member and asked for money urgently. \
The family member has since confirmed they did not send it. I did not receive any goods or services and I consider the payment fraudulent.

Please reverse or recall the payment where possible, open a fraud case, and confirm in writing what you have done.

Yours faithfully,"""

POLICE_REPORT_TEMPLATE = """What happened, in order:

1. I received an email claiming to be from a family member and asking for money urgently.
2. On {paid_on} I made a {rail_label} of {amount} through {bank}{payee_clause}.
3. The family member was asked on their own phone and confirmed they never sent the email.
4. I have reported the payment to {bank}.

I have kept the email and my payment record and can provide both."""


def format_amount(case_row: dict) -> str:
    amount = case_row["amount"]
    amount_text = f"{amount:,.2f}" if isinstance(amount, Decimal) else str(amount)
    return f"{amount_text} {case_row.get('currency', '')}".strip()


def payee_clause_for(case_row: dict) -> str:
    payee_hint = str(case_row.get("payeeHint", "")).strip()
    return f" to {payee_hint}" if payee_hint else ""


def build_recovery_drafts(case_row: dict, today: str) -> dict:
    """{callScript, disputeLetter, policeReport} from the case's bank, rail, amount, date and payee hint; today is a YYYY-MM-DD."""
    fields = {
        "bank": str(case_row["bank"]),
        "rail_label": PAYMENT_RAIL_LABELS[PaymentRail(case_row["paymentRail"])],
        "amount": format_amount(case_row),
        "paid_on": str(case_row["paidAt"])[:10],
        "payee_clause": payee_clause_for(case_row),
        "today": today,
    }
    return {
        "callScript": CALL_SCRIPT_TEMPLATE.format(**fields),
        "disputeLetter": DISPUTE_LETTER_TEMPLATE.format(**fields),
        "policeReport": POLICE_REPORT_TEMPLATE.format(**fields),
    }


def describe_pack_step(step: dict) -> str:
    deadline = ""
    if step.get("deadlineHours"):
        deadline = f" (within {step['deadlineHours']} hours)"
    elif step.get("deadlineDays"):
        deadline = f" (within {step['deadlineDays']} days)"
    return f"{step['order']}. {step['action']} {step['contact']}{deadline}"


def build_pack_email_body(recovery_pack: dict) -> str:
    """The steps in order, then the three drafts. Only the pack's own fields go in."""
    steps = sorted(recovery_pack.get("steps", []), key=lambda step: step["order"])
    step_lines = "\n".join(describe_pack_step(step) for step in steps) or "Pidge has no steps for this country and payment yet."
    drafts = recovery_pack["drafts"]
    return (
        "Your recovery pack from Pidge. Pidge sends nothing to anyone else; these are for you to use.\n\n"
        f"Steps\n{step_lines}\n\n"
        f"Call script\n{drafts['callScript']}\n\n"
        f"Dispute letter\n{drafts['disputeLetter']}\n\n"
        f"Police report\n{drafts['policeReport']}\n"
    )


def send_pack_email(ses_client, sender_address: str, receiver_mailbox: str, recovery_pack: dict) -> None:
    """Mails the pack to the receiver's own mailbox, never anywhere else. Raises on an SES refusal; the caller logs it."""
    ses_client.send_email(
        FromEmailAddress=sender_address,
        Destination={"ToAddresses": [receiver_mailbox]},
        Content={"Simple": {"Subject": {"Data": PACK_EMAIL_SUBJECT}, "Body": {"Text": {"Data": build_pack_email_body(recovery_pack)}}}},
    )
