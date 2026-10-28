"""The JSON shapes pushed to the PWAs. One place, so the Reader, the verdict handler and sync agree."""

from dataclasses import dataclass

from shared import graph, reach_points
from shared.enums import CaseEvent, Channel, Friction, PushMessageType, Relationship, RequestState
from shared.list_enums import ScamListCoverageState

EMAIL_FIELDS_FOR_PWA = ("senderName", "senderAddress", "subject", "receivedAt", "textBody")


def optional_enum_value(enum_type, stored_value) -> str | None:
    """A stored closed-set value parsed through its Enum (an unknown value raises), or None when the row has none."""
    return None if stored_value is None else str(enum_type(stored_value))


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
        "repliedVia": optional_enum_value(Channel, request_row.get("repliedVia")),  # the channel the answer came on; None until answered
        **request_timing(request_row),
    }


def reading_email_for_receiver(email_row: dict) -> dict:
    """The row while the Reader is still on it: state None, no body, no links; only the sender line and the subject show before
    the verdict. The email_state push after the policy run replaces it (same emailId)."""
    return {
        "emailId": email_row["s3Key"],
        **{field: email_row[field] for field in EMAIL_FIELDS_FOR_PWA},
        "textBody": "",
        "state": None,
        "warnReason": None,
        "reasons": [],
        "links": [],
        "scamListCoverage": None,
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
        "scamListCoverage": optional_enum_value(ScamListCoverageState, email_row.get("scamListCoverage")),  # absent before SX3
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
            "repliedVia": optional_enum_value(Channel, request_row.get("repliedVia")),
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


@dataclass(frozen=True)
class ReachFlags:
    """Which phone and mail channels this stack sends asks on (the RCS_ENABLED / WHATSAPP_ENABLED / EMAIL_ASK_ENABLED flags)."""

    is_rcs_enabled: bool
    is_whatsapp_enabled: bool
    is_email_ask_enabled: bool


def circle_entry_for_receiver(contact_row: dict, reach_flags: ReachFlags) -> dict:
    """One member of the receiver's circle: who, how verified, which channels reach them, and the numbers and emails she
    typed with each one's check state (her own sockets only: the edit form shows them). A chip lights only when the row has
    a point Pidge asks on that channel AND the stack sends on it; a number still waiting for its code, a phone she typed (never
    checked) and a number behind a flag that is off are not a reach."""
    points = reach_points.reach_points_of(contact_row)
    channels_on_row = {point.channel for point in points if point.is_askable}
    return {
        "contactUserId": contact_row["contactPwaUserId"],
        "contactName": contact_row["contactName"],
        "relationship": optional_enum_value(Relationship, contact_row.get("relationship")),  # absent on seeded rows
        "verifiedVia": str(graph.verified_via_of(contact_row)),
        "verifiedAt": contact_row.get("verifiedAt"),
        "codeExpiresAt": contact_row.get("verificationExpiresAt"),  # set while a number waits for its code: past it, the code is dead
        "reach": {
            str(Channel.INAPP): True,  # every row has a PWA token: seeded by scripts/seed_graph.py, or minted with the row
            str(Channel.RCS): reach_flags.is_rcs_enabled and Channel.RCS in channels_on_row,
            str(Channel.WHATSAPP): reach_flags.is_whatsapp_enabled and Channel.WHATSAPP in channels_on_row,
            str(Channel.EMAIL): reach_flags.is_email_ask_enabled and Channel.EMAIL in channels_on_row,
        },
        "reachPoints": [{"channel": str(point.channel), "address": point.address,
                         "verifiedVia": None if point.verified_via is None else str(point.verified_via)} for point in points],
    }
