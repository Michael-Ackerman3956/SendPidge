"""Closed sets for the MESSAGE# rows of the phone channels (WhatsApp, RCS, SMS) and of a resident's sends. Mirrored in
pwa/js/constants.js."""

from enum import StrEnum


class MessageDirection(StrEnum):
    """Which way a MESSAGE# row went: in from a phone channel, or out from the resident."""

    IN = "in"
    OUT = "out"


class ProviderStatus(StrEnum):
    """Where a resident's outgoing MESSAGE# row is with its provider: written SENDING before the call, then SENT or FAILED."""

    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
