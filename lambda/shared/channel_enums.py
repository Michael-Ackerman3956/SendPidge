"""Closed sets for the wire values that WhatsApp and RCS records carry inbound over SNS, and that Amazon Connect flow events
carry into pidge-verdict. Code branches on these, never on bare strings."""

from enum import StrEnum


class RcsInboundMessageType(StrEnum):
    """The `type` inside an inbound RCS messageBody JSON. Only a suggestion tap carries postbackData."""

    SUGGESTION = "SUGGESTION"  # a chip tap: pidge-verdict reads it as an answer
    TEXT = "TEXT"  # typed text: pidge-inbound makes it an inbox row (raw value unverified on a live record)


class WhatsAppInboundMessageType(StrEnum):
    """The `type` of an inbound WhatsApp message: a quick-reply tap on our template, a reply-button tap, typed text, or media."""

    BUTTON = "button"
    INTERACTIVE = "interactive"  # a reply-button tap on a non-template interactive message (the call offer)
    TEXT = "text"                # typed text: an inbox row (pidge-inbound), or a pending contact's code (pidge-verdict)
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    STICKER = "sticker"


# Media is an inbox row with no text (never downloaded, captions ignored): the Reader is skipped and it reads as unreadable.
WHATSAPP_MEDIA_MESSAGE_TYPES = frozenset({
    WhatsAppInboundMessageType.IMAGE, WhatsAppInboundMessageType.AUDIO, WhatsAppInboundMessageType.VIDEO,
    WhatsAppInboundMessageType.DOCUMENT, WhatsAppInboundMessageType.STICKER,
})


class WhatsAppInteractiveType(StrEnum):
    """The `interactive.type` of an inbound interactive WhatsApp message. Only a reply-button tap is read."""

    BUTTON_REPLY = "button_reply"


class WhatsAppMessageStatus(StrEnum):
    """The `status` of a WhatsApp delivery status for one of our own messages. Only a failure is acted on."""

    FAILED = "failed"


class VoiceFlowStep(StrEnum):
    """The static voiceStep parameter on the Connect flow's Lambda block: which step of Pidge's call invoked pidge-verdict."""

    RECORD_ANSWER = "record_answer"
    LOAD_FACTS = "load_facts"  # the step before the AI bot: writes the agent's session facts
