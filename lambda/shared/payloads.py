"""The JSON shapes pushed to the PWAs. One place, so the Reader, the verdict handler and sync agree."""

from shared.enums import CaseEvent, PushMessageType, RequestState

EMAIL_FIELDS_FOR_PWA = ("senderName", "senderAddress", "subject", "receivedAt", "textBody")


def request_timing(request_row: dict) -> dict:
    return {
        "expiresAt": request_row["expiresAt"],
        "resolvedAt": request_row.get("resolvedAt"),
        "releaseAt": request_row.get("releaseAt"),
    }


def request_for_receiver(request_row: dict, contact_name: str, status: RequestState) -> dict:
    return {
        "requestId": request_row["PK"].removeprefix("REQUEST#"),
        "emailId": request_row["emailId"],
        "contactName": contact_name,
        "intent": request_row["verdict"]["intent"],
        "summary": request_row["verdict"]["summary"],
        "status": status,
        **request_timing(request_row),
    }


def email_for_receiver(email_row: dict, request_row: dict | None, contact_name: str | None, status: RequestState | None) -> dict:
    """email_row carries state / warnReason / reasons once the policy has run; status is the request's effective status."""
    return {
        "emailId": email_row["s3Key"],
        **{field: email_row[field] for field in EMAIL_FIELDS_FOR_PWA},
        "state": email_row["state"],
        "warnReason": email_row.get("warnReason"),
        "reasons": list(email_row.get("reasons", [])),
        "quarantined": bool(email_row.get("quarantined", False)),
        "request": request_for_receiver(request_row, contact_name, status) if request_row else None,
    }


def email_state_message(email_row: dict, request_row: dict | None, contact_name: str | None, status: RequestState | None) -> dict:
    return {"type": PushMessageType.EMAIL_STATE, "email": email_for_receiver(email_row, request_row, contact_name, status)}


def card_for_contact(request_row: dict, email_row: dict | None, receiver_name: str, status: RequestState) -> dict:
    """Jimin's card: the summary plus the sender address and subject so an impersonator's address is on the card."""
    return {
        "requestId": request_row["PK"].removeprefix("REQUEST#"),
        "summary": request_row["verdict"]["summary"],
        "senderAddress": (email_row or {}).get("senderAddress", ""),
        "subject": (email_row or {}).get("subject", ""),
        "friction": request_row["friction"],
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
            "requestId": request_row["PK"].removeprefix("REQUEST#"),
            "emailId": request_row["emailId"],
            "status": status,
            "contactName": contact_name,
            "quarantined": quarantined,
            **request_timing(request_row),
        },
    }


def note_for_receiver(note_row: dict) -> dict:
    return {
        "noteId": note_row["SK"],
        "requestId": note_row["PK"].removeprefix("REQUEST#"),
        "writtenAt": note_row["writtenAt"],
        "noteText": note_row["noteText"],
        "recoveryPack": note_row.get("recoveryPack"),
    }


def case_note_message(note_row: dict) -> dict:
    return {"type": PushMessageType.CASE_NOTE, "note": note_for_receiver(note_row)}


CASE_FIELDS_FOR_PWA = ("requestId", "jurisdiction", "paymentRail", "bank", "amount", "currency", "paidAt", "payeeHint", "status",
                       "ruleGap", "createdAt", "reportSentAt", "followUpSentAt", "bankRepliedAt", "closedAt")


def case_for_receiver(case_row: dict, reply_rows: list[dict]) -> dict:
    """The case and the bank's replies as quoted text. The reply token and the mock mailbox stay server-side."""
    return {
        "caseId": case_row["PK"].removeprefix("CASE#"),
        **{field: (str(case_row[field]) if field == "amount" else case_row.get(field)) for field in CASE_FIELDS_FOR_PWA},
        "replies": [{"from": reply["from"], "receivedAt": reply["receivedAt"], "textContent": reply["textContent"]} for reply in reply_rows],
    }


def case_state_message(case_row: dict, reply_rows: list[dict], event: CaseEvent) -> dict:
    return {"type": PushMessageType.CASE_STATE, "event": event, "case": case_for_receiver(case_row, reply_rows)}


def contact_notice_message(text: str) -> dict:
    return {"type": PushMessageType.CONTACT_NOTICE, "text": text}
