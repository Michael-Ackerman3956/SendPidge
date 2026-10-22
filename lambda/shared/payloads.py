"""The JSON shapes pushed to the PWAs. One place, so the Reader, the verdict handler and sync agree."""

from shared.enums import PushMessageType, RequestState

EMAIL_FIELDS_FOR_PWA = ("senderName", "senderAddress", "subject", "receivedAt", "textBody")


def request_for_receiver(request_row: dict, contact_name: str) -> dict:
    return {
        "requestId": request_row["PK"].removeprefix("REQUEST#"),
        "emailId": request_row["emailId"],
        "contactName": contact_name,
        "intent": request_row["verdict"]["intent"],
        "summary": request_row["verdict"]["summary"],
        "expiresAt": request_row["expiresAt"],
        "status": request_row["status"],
    }


def email_for_receiver(email_row: dict, request_row: dict | None, contact_name: str | None) -> dict:
    """email_row carries state / warnReason / reasons once the policy has run."""
    return {
        "emailId": email_row["s3Key"],
        **{field: email_row[field] for field in EMAIL_FIELDS_FOR_PWA},
        "state": email_row["state"],
        "warnReason": email_row.get("warnReason"),
        "reasons": list(email_row.get("reasons", [])),
        "quarantined": bool(email_row.get("quarantined", False)),
        "request": request_for_receiver(request_row, contact_name) if request_row else None,
    }


def email_state_message(email_row: dict, request_row: dict | None, contact_name: str | None) -> dict:
    return {"type": PushMessageType.EMAIL_STATE, "email": email_for_receiver(email_row, request_row, contact_name)}


def card_for_contact(request_row: dict, receiver_name: str) -> dict:
    return {
        "requestId": request_row["PK"].removeprefix("REQUEST#"),
        "summary": request_row["verdict"]["summary"],
        "expiresAt": request_row["expiresAt"],
        "status": request_row["status"],
        "receiverName": receiver_name,
    }


def verification_card_message(request_row: dict, receiver_name: str) -> dict:
    return {"type": PushMessageType.VERIFICATION_CARD, "request": card_for_contact(request_row, receiver_name)}


def request_state_message(request_row: dict, status: RequestState, contact_name: str, quarantined: bool) -> dict:
    return {
        "type": PushMessageType.REQUEST_STATE,
        "request": {
            "requestId": request_row["PK"].removeprefix("REQUEST#"),
            "emailId": request_row["emailId"],
            "status": status,
            "contactName": contact_name,
            "quarantined": quarantined,
        },
    }
