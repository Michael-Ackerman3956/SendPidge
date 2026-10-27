"""The JSON shapes pushed to the PWAs. One place, so the Reader, the verdict handler and sync agree."""

import logging

from shared import graph
from shared.enums import CaseEvent, Channel, Friction, PushMessageType, RequestState, VerifiedVia

logger = logging.getLogger(__name__)

EMAIL_FIELDS_FOR_PWA = ("senderName", "senderAddress", "subject", "receivedAt", "textBody")


def request_timing(request_row: dict) -> dict:
    return {
        "expiresAt": request_row["expiresAt"],
        "resolvedAt": request_row.get("resolvedAt"),
        "releaseAt": request_row.get("releaseAt"),
    }


def request_for_receiver(request_row: dict, contact_name: str, status: RequestState) -> dict:
    return {
        "requestId": graph.request_id_of(request_row),
        "emailId": request_row["emailId"],
        "contactName": contact_name,
        "intent": request_row["verdict"]["intent"],
        "summary": request_row["verdict"]["summary"],
        "status": status,
        "repliedVia": request_row.get("repliedVia"),  # the channel the answer came on; None until answered
        **request_timing(request_row),
    }


def reading_email_for_receiver(email_row: dict) -> dict:
    """The row while the Reader is still on it: state None, no body, no links, so nothing from the email is shown before the
    verdict. The email_state push after the policy run replaces it (same emailId)."""
    return {
        "emailId": email_row["s3Key"],
        **{field: email_row[field] for field in EMAIL_FIELDS_FOR_PWA},
        "textBody": "",
        "state": None,
        "warnReason": None,
        "reasons": [],
        "links": [],
        "quarantined": False,
        "request": None,
    }


def email_reading_message(email_row: dict) -> dict:
    return {"type": PushMessageType.EMAIL_STATE, "email": reading_email_for_receiver(email_row)}


def email_for_receiver(email_row: dict, request_row: dict | None, contact_name: str | None, status: RequestState | None) -> dict:
    """email_row carries state / warnReason / reasons once the policy has run; status is the request's effective status."""
    return {
        "emailId": email_row["s3Key"],
        **{field: email_row[field] for field in EMAIL_FIELDS_FOR_PWA},
        "state": email_row["state"],
        "warnReason": email_row.get("warnReason"),
        "reasons": list(email_row.get("reasons", [])),
        "links": list(email_row.get("links", [])),  # absent on rows written before slice 9a
        "quarantined": bool(email_row.get("quarantined", False)),
        "request": request_for_receiver(request_row, contact_name, status) if request_row else None,
    }


def email_state_message(email_row: dict, request_row: dict | None, contact_name: str | None, status: RequestState | None) -> dict:
    return {"type": PushMessageType.EMAIL_STATE, "email": email_for_receiver(email_row, request_row, contact_name, status)}


def card_for_contact(request_row: dict, email_row: dict | None, receiver_name: str, status: RequestState) -> dict:
    """Jimin's card: the summary plus the sender address and subject so an impersonator's address is on the card."""
    return {
        "requestId": graph.request_id_of(request_row),
        "summary": request_row["verdict"]["summary"],
        "senderAddress": (email_row or {}).get("senderAddress", ""),
        "subject": (email_row or {}).get("subject", ""),
        "friction": request_row.get("friction", Friction.SINGLE_TAP),  # absent on rows written before slice 8 (one tap then)
        "status": status,
        "receiverName": receiver_name,
        **request_timing(request_row),
    }


def verification_card_message(request_row: dict, email_row: dict | None, receiver_name: str, status: RequestState) -> dict:
    return {"type": PushMessageType.VERIFICATION_CARD, "request": card_for_contact(request_row, email_row, receiver_name, status)}


def request_state_message(request_row: dict, status: RequestState, contact_name: str, quarantined: bool) -> dict:
    return {
        "type": PushMessageType.REQUEST_STATE,
        "request": {
            "requestId": graph.request_id_of(request_row),
            "emailId": request_row["emailId"],
            "status": status,
            "contactName": contact_name,
            "quarantined": quarantined,
            "repliedVia": request_row.get("repliedVia"),
            **request_timing(request_row),
        },
    }


def note_for_receiver(note_row: dict) -> dict:
    return {
        "noteId": note_row["SK"],
        "requestId": graph.request_id_of(note_row),
        "writtenAt": note_row["writtenAt"],
        "noteText": note_row["noteText"],
        "recoveryPack": note_row.get("recoveryPack"),
        "packMailedAt": note_row.get("packMailedAt"),  # set once the pack reached her own mailbox
    }


def case_note_message(note_row: dict) -> dict:
    return {"type": PushMessageType.CASE_NOTE, "note": note_for_receiver(note_row)}


CASE_FIELDS_FOR_PWA = ("requestId", "jurisdiction", "paymentRail", "bank", "amount", "currency", "paidAt", "payeeHint", "status",
                       "ruleGap", "createdAt", "reportSentAt", "followUpSentAt", "bankRepliedAt", "closedAt")


def case_for_receiver(case_row: dict, reply_rows: list[dict]) -> dict:
    """The case and the bank's replies as quoted text. The reply token and the mock mailbox stay server-side."""
    return {
        "caseId": graph.case_id_of(case_row),
        **{field: (str(case_row[field]) if field == "amount" else case_row.get(field)) for field in CASE_FIELDS_FOR_PWA},
        "replies": [{"from": reply["from"], "receivedAt": reply["receivedAt"], "textContent": reply["textContent"]} for reply in reply_rows],
    }


def case_state_message(case_row: dict, reply_rows: list[dict], event: CaseEvent) -> dict:
    return {"type": PushMessageType.CASE_STATE, "event": event, "case": case_for_receiver(case_row, reply_rows)}


def contact_notice_message(text: str) -> dict:
    return {"type": PushMessageType.CONTACT_NOTICE, "text": text}


def verified_via_of(contact_row: dict) -> VerifiedVia:
    """Only scripts/seed_graph.py writes CONTACT# rows, so a row without the field is a seeded one; it is logged, never silent.
    An unknown value still raises."""
    if "verifiedVia" not in contact_row:
        logger.warning("contact row %s has no verifiedVia; treating as seed (re-run scripts/seed_graph.py)", contact_row["SK"])
        return VerifiedVia.SEED
    return VerifiedVia(contact_row["verifiedVia"])


def circle_entry_for_receiver(contact_row: dict, is_rcs_enabled: bool) -> dict:
    """One member of the receiver's circle: who, how verified, which channels reach them. Never a number or an address.
    RCS lights only when the stack sends on it; a seeded number with the flag off is not a reach."""
    return {
        "contactUserId": contact_row["contactPwaUserId"],
        "contactName": contact_row["contactName"],
        "relationship": contact_row.get("relationship"),  # a Relationship value; absent on seeded rows
        "verifiedVia": str(verified_via_of(contact_row)),
        "verifiedAt": contact_row.get("verifiedAt"),
        "reach": {
            str(Channel.INAPP): True,
            str(Channel.RCS): is_rcs_enabled and "contactRcs" in contact_row,
            str(Channel.WHATSAPP): "contactWhatsApp" in contact_row,
            str(Channel.EMAIL): "contactEmail" in contact_row,
        },
    }
