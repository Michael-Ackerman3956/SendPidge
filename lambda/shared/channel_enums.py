"""Closed sets for the wire values that WhatsApp and RCS records carry inbound over SNS. Code branches on these, never on
bare strings."""

from enum import StrEnum


class RcsInboundMessageType(StrEnum):
    """The `type` inside an inbound RCS messageBody JSON. Only a suggestion tap carries postbackData."""

    SUGGESTION = "SUGGESTION"


class WhatsAppInboundMessageType(StrEnum):
    """The `type` of an inbound WhatsApp message: a quick-reply tap on our template, a reply-button tap, or typed text."""

    BUTTON = "button"
    INTERACTIVE = "interactive"  # a reply-button tap on a non-template interactive message (the call offer)
    TEXT = "text"                # typed text: only a pending contact's verification code is read from it


class WhatsAppInteractiveType(StrEnum):
    """The `interactive.type` of an inbound interactive WhatsApp message. Only a reply-button tap is read."""

    BUTTON_REPLY = "button_reply"


class WhatsAppMessageStatus(StrEnum):
    """The `status` of a WhatsApp delivery status for one of our own messages. Only a failure is acted on."""

    FAILED = "failed"
