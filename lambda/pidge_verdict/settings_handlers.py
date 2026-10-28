"""pidge-verdict's resident Settings actions: set_personal_email (mails a code to the new address) and confirm_personal_email
(the code saves it). Each acts for the ReceiverProfile route_resident_action resolved from the caller's own PROFILE row, never
an address or user from the body. A refusal goes back to the caller's socket; a recorded step pushes her Settings rows to
every one of her sockets."""

import os
from datetime import datetime, timezone

import boto3

from shared import channels, personal_email
from shared.enums import ActionOutcome, PushMessageType
from shared.profiles import ReceiverProfile

_ses_client = None


def get_ses_client():
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("sesv2")
    return _ses_client


def read_protected_mail_domains() -> frozenset[str]:
    """Our own mail domains (PROTECTED_MAIL_DOMAINS, comma-separated, read per call): a personal email there would re-enter
    Mail Manager instead of reaching her."""
    return frozenset(domain.strip().casefold() for domain in os.environ["PROTECTED_MAIL_DOMAINS"].split(",") if domain.strip())


def push_personal_email_state(resident: ReceiverProfile) -> None:
    state = personal_email.personal_email_for_sync(resident, datetime.now(timezone.utc))
    channels.push_to_user(resident.receiver_user_id, {"type": PushMessageType.PERSONAL_EMAIL_STATE, "personalEmail": state})


def answer_settings_action(connection_id: str, resident: ReceiverProfile, outcome: ActionOutcome) -> None:
    if outcome is not ActionOutcome.RECORDED:
        channels.reject_action(connection_id, None, outcome)
        return
    push_personal_email_state(resident)


def handle_set_personal_email(connection_id: str, resident: ReceiverProfile, body: dict) -> None:
    outcome = personal_email.start_change(resident, body.get("address"), datetime.now(timezone.utc), get_ses_client(),
                                          os.environ["RECEIPT_SENDER"], read_protected_mail_domains())
    answer_settings_action(connection_id, resident, outcome)
    if outcome is ActionOutcome.SEND_FAILED:
        push_personal_email_state(resident)  # after the refusal: the change is waiting, so she can send the code again


def handle_confirm_personal_email(connection_id: str, resident: ReceiverProfile, body: dict) -> None:
    outcome = personal_email.confirm_change(resident, body.get("code"), datetime.now(timezone.utc))
    answer_settings_action(connection_id, resident, outcome)
