"""pidge-verdict's circle actions: add_contact from the Add-someone form, verify_contact (the receiver's labelled demo
reply, CONTACT_VERIFICATION_MOCK only) and the WhatsApp text that brings a pending contact's code back from their own
number. Settings are read per call so a test can change the flags between calls."""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from shared import channels, circle, graph, payloads, whatsapp
from shared.enums import ActionOutcome, PushMessageType, VerifiedVia
from shared.metrics import emit_count_metric

logger = logging.getLogger(__name__)
CONTACT_NOTE_PUSH_FAILED_METRIC_NAME = "ContactNotePushFailed"  # the note never reached her socket; adding the name again re-issues it
WELCOME_SEND_FAILED_METRIC_NAME = "WelcomeSendFailed"


@dataclass(frozen=True)
class CircleSettings:
    receiver_address: str
    receiver_user_id: str
    receiver_name: str
    pwa_base_url: str
    pidge_whatsapp_number: str  # Pidge's own WhatsApp number in E.164, "" when the stack has none to reply to
    is_rcs_enabled: bool
    is_whatsapp_enabled: bool
    is_email_ask_enabled: bool
    is_mock_on: bool

    @property
    def is_whatsapp_verification_on(self) -> bool:
        return self.is_whatsapp_enabled and self.pidge_whatsapp_number != ""

    @property
    def reach_flags(self) -> payloads.ReachFlags:
        return payloads.ReachFlags(self.is_rcs_enabled, self.is_whatsapp_enabled, self.is_email_ask_enabled)


def read_settings() -> CircleSettings:
    return CircleSettings(
        receiver_address=os.environ["RECEIVER_ADDRESS"],
        receiver_user_id=os.environ["RECEIVER_USER_ID"],
        receiver_name=os.environ["RECEIVER_NAME"],
        pwa_base_url=os.environ["PWA_BASE_URL"],
        pidge_whatsapp_number=os.environ.get("PIDGE_WHATSAPP_NUMBER", ""),
        is_rcs_enabled=os.environ.get("RCS_ENABLED") == "true",
        is_whatsapp_enabled=os.environ.get("WHATSAPP_ENABLED") == "true",
        is_email_ask_enabled=os.environ.get("EMAIL_ASK_ENABLED") == "true",
        is_mock_on=os.environ.get("CONTACT_VERIFICATION_MOCK") == "true",
    )


def verification_flags(settings: CircleSettings) -> dict:
    """What sync tells the PWA about how a new contact can be verified on this stack."""
    return {"whatsapp": settings.is_whatsapp_verification_on, "mock": settings.is_mock_on}


def circle_entries(settings: CircleSettings) -> list[dict]:
    return [payloads.circle_entry_for_receiver(row, settings.reach_flags) for row in graph.list_contact_rows(settings.receiver_address)]


def push_circle_state(settings: CircleSettings) -> None:
    channels.push_to_user(settings.receiver_user_id, {"type": PushMessageType.CIRCLE_STATE, "circle": circle_entries(settings)})


def handle_add_contact(connection_id: str, user_id: str, body: dict) -> None:
    """The receiver only. A bad field, a duplicate name or a full circle writes nothing. On success the caller's socket alone
    gets the note (it carries the code and the contact's PWA token), then every receiver socket gets the new circle."""
    settings = read_settings()
    if user_id != settings.receiver_user_id:
        channels.reject_action(connection_id, None, ActionOutcome.NOT_ALLOWED)
        return
    try:
        form = circle.parse_contact_form(body)
    except ValueError as error:
        logger.info("add_contact refused: %s", error)
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    enrolment = circle.enrol_contact(settings.receiver_address, form, datetime.now(timezone.utc))
    if enrolment.outcome is not ActionOutcome.RECORDED:
        logger.info("add_contact refused: %s", enrolment.outcome)
        channels.reject_action(connection_id, None, enrolment.outcome)
        return
    pwa_link = f"{settings.pwa_base_url}/?token={enrolment.access_token}"
    note_text = circle.build_contact_note(settings.receiver_name, enrolment.code, pwa_link,
                                          settings.pidge_whatsapp_number if settings.is_whatsapp_verification_on else None)
    try:
        channels.push_to_connection(connection_id, {
            "type": PushMessageType.CONTACT_ADDED,
            "contact": payloads.circle_entry_for_receiver(enrolment.contact_row, settings.reach_flags),
            "noteText": note_text,
            "code": enrolment.code,
        })
    except Exception:  # the socket went away after the write: the row stays pending and adding the name again re-issues the note
        logger.exception("contact_added for %s could not be pushed", enrolment.contact_row["SK"])
        emit_count_metric(CONTACT_NOTE_PUSH_FAILED_METRIC_NAME)
    logger.info("contact %s added or re-issued as %s", enrolment.contact_row["SK"], VerifiedVia.PENDING)
    push_circle_state(settings)


def handle_verify_contact(connection_id: str, user_id: str, body: dict) -> None:
    """The labelled demo: the receiver's own PWA plays the contact's reply with the code it was shown. Mock mode only; the
    row must be PENDING with a live code, so a seeded row can never be 'verified' this way."""
    settings = read_settings()
    if user_id != settings.receiver_user_id or not settings.is_mock_on:
        channels.reject_action(connection_id, None, ActionOutcome.NOT_ALLOWED)
        return
    contact_user_id, code = body.get("contactUserId"), body.get("code")
    if not isinstance(contact_user_id, str) or not isinstance(code, str):
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    contact_row = graph.find_contact_row_for_user(settings.receiver_address, contact_user_id)
    if contact_row is None or not circle.verify_with_code(settings.receiver_address, contact_row, code, VerifiedVia.MOCK,
                                                          datetime.now(timezone.utc)):
        logger.info("contact verification refused for %s: %s", user_id, "unknown contact" if contact_row is None else "wrong, expired or no code")
        channels.reject_action(connection_id, None, ActionOutcome.NOT_APPLIED)
        return
    logger.info("contact %s verified via %s", contact_row["SK"], VerifiedVia.MOCK)
    push_circle_state(settings)


def handle_whatsapp_text(inbound_text: whatsapp.InboundText) -> None:
    """Typed text on Pidge's WhatsApp number: it verifies a PENDING contact only when it comes from that row's own number
    and carries the live code. Anything else is logged and ignored; the text itself is never stored."""
    settings = read_settings()
    contact_row = circle.find_pending_contact_by_whatsapp(settings.receiver_address, inbound_text.sender_number)
    if contact_row is None:
        logger.info("WhatsApp text %s from a number with no pending contact; ignored", inbound_text.message_id)
        return
    if not circle.verify_with_code(settings.receiver_address, contact_row, inbound_text.text, VerifiedVia.WHATSAPP, datetime.now(timezone.utc)):
        logger.info("WhatsApp text %s did not verify %s: wrong or expired code", inbound_text.message_id, contact_row["SK"])
        return
    logger.info("contact %s verified via %s", contact_row["SK"], VerifiedVia.WHATSAPP)
    push_circle_state(settings)
    send_welcome(contact_row["contactWhatsApp"], settings.receiver_name)


def send_welcome(destination_number: str, receiver_name: str) -> None:
    """One plain-text reply inside the 24-h window the contact's text opened; a failed send is logged, the row is already verified."""
    try:
        channels.send_whatsapp_message(whatsapp.build_text_message(destination_number, circle.build_welcome_text(receiver_name)))
    except Exception:
        logger.exception("whatsapp welcome to the new contact failed")
        emit_count_metric(WELCOME_SEND_FAILED_METRIC_NAME)
