"""Closed sets of the agent runner and the output gate on agent text. Code branches on these, never on bare strings."""

from enum import StrEnum


class UnsafeTextKind(StrEnum):
    """What the output gate found in agent text bound for a person; any one refuses the text."""

    LINK = "link"
    EMAIL_ADDRESS = "email_address"
    MESSENGER_HANDLE = "messenger_handle"  # "@refund_desk" on Telegram, Instagram or X
    LONG_NUMBER = "long_number"  # 6+ digits: a phone, an account, a code; not a calendar date
    DISALLOWED_CHARACTER = "disallowed_character"  # outside the allow-list: not Latin, Hangul or plain punctuation


class AgentRunFailure(StrEnum):
    """Why one tool-holding agent run produced no output."""

    TURN_LIMIT = "turn_limit"
    NO_OUTPUT = "no_output"
