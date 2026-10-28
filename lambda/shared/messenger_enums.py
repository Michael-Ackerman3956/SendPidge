"""Closed sets for the MESSAGE# rows of the phone channels (WhatsApp, RCS, SMS). Mirrored in pwa/js/constants.js."""

from enum import StrEnum


class MessageDirection(StrEnum):
    """Which way a MESSAGE# row went: in from a phone channel, or out from the resident."""

    IN = "in"
    OUT = "out"
