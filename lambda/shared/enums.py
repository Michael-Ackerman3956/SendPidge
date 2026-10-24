"""Every closed set in SendPidge, in one place. Code branches on these, never on bare strings."""

from enum import StrEnum


class Intent(StrEnum):
    """What the email asks the receiver to do, as judged by the Reader."""

    MONEY = "money"
    CODE = "code"
    URGENT_ACTION = "urgent_action"
    NONE = "none"


class PolicyDecision(StrEnum):
    """What the policy does with a verdict."""

    HOLD = "hold"
    WARN = "warn"
    PASS = "pass"


class WarnReason(StrEnum):
    """Why an email is marked but not held."""

    UNREADABLE = "unreadable"          # Reader failed: "Pidge couldn't check this one"
    UNKNOWN_NAME = "unknown_name"      # an ask from a name that is not a contact
    LOW_CONFIDENCE = "low_confidence"  # a weak ask from a known name
    LISTED_SENDER = "listed_sender"    # no ask, but the sender carries reasons
    UNREACHABLE = "unreachable"        # every channel failed; the hold is kept
    ESCALATION_CLEARED = "escalation_cleared"  # a model-driven hold that the second reviewer downgraded to a mark


class ReasonCode(StrEnum):
    """Machine reasons attached to an email. Lookup tables add more in a later slice."""

    AUTH_MISSING = "auth_missing"      # no X-Pidge-Auth header at all
    AUTH_TAMPERED = "auth_tampered"    # more than one X-Pidge-Auth header
    AUTH_FAILED = "auth_failed"      # neither SPF nor DKIM passed at ingress
    UNKNOWN_ADDRESS = "unknown_address"        # gate 1: a contact's display name from an address not in knownAddresses
    ESCALATION_CLEARED = "escalation_cleared"  # the second reviewer said a mark is enough (gate 2 only)
    ESCALATION_FAILED = "escalation_failed"    # the second reviewer could not answer; the hold stays
    PERSON_CONFLICT = "person_conflict"        # the two models named different people; the hold stays


class GateFired(StrEnum):
    """Which policy gate created a hold."""

    GATE_1 = "gate_1"  # code: known display name, unknown address
    GATE_2 = "gate_2"  # model: a clear ask from a claimed contact


class Friction(StrEnum):
    """How many taps the contact's Yes needs."""

    SINGLE_TAP = "single_tap"
    DOUBLE_TAP = "double_tap"  # impersonator asking for money or a code


class EscalationDecision(StrEnum):
    """What the second reviewer (Opus) says about a hold it was shown."""

    HOLD = "hold"
    WARN = "warn"


class RequestState(StrEnum):
    """Lifecycle of one 'Was this you?' request row."""

    PENDING = "pending"
    DENIED = "denied"
    CONFIRMED = "confirmed"
    TIMEOUT = "timeout"


class VerdictAnswer(StrEnum):
    """The only two things a contact can answer."""

    YES = "yes"
    NO = "no"


class Channel(StrEnum):
    """Where a 'Was this you?' ask can be sent."""

    RCS = "rcs"
    WHATSAPP = "whatsapp"
    INAPP = "inapp"


class RcsInboundMessageType(StrEnum):
    """The `type` inside an inbound RCS messageBody JSON. Only a suggestion tap carries postbackData."""

    SUGGESTION = "SUGGESTION"


class WhatsAppInboundMessageType(StrEnum):
    """The `type` of an inbound WhatsApp message. Only a quick-reply tap on our template carries a payload."""

    BUTTON = "button"


class ReaderModelChoice(StrEnum):
    """Allowed values of the READER_MODEL setting."""

    HAIKU_5_5 = "haiku-5.5"
    HAIKU_4_5 = "haiku-4.5"
    NOVA_MICRO = "nova-micro"
    GEMMA_4_E2B = "gemma-4-e2b"
    OPUS_4_5 = "opus-4.5"  # the escalation reviewer, never the Reader (the only Opus this account can call, 2026-10-24)
    OPUS_5_5 = "opus-5.5"  # the escalation reviewer once the account is granted access


class PushMessageType(StrEnum):
    """Kinds of message the backend pushes to a PWA over the WebSocket. Mirrored in pwa/js/constants.js."""

    EMAIL_STATE = "email_state"              # to the receiver: one email row and its hold / warn / pass state
    VERIFICATION_CARD = "verification_card"  # to the contact: "Was this you?"
    REQUEST_STATE = "request_state"          # to both: a request's status changed (answered, quarantined)
    SYNC_STATE = "sync_state"                # reply to ClientAction.SYNC: the user's role and current rows
    ACTION_REJECTED = "action_rejected"      # reply when a client action is refused


class ClientAction(StrEnum):
    """What a PWA may send on the WebSocket $default route. Mirrored in pwa/js/constants.js."""

    SYNC = "sync"        # either user, right after connecting
    ANSWER = "answer"    # the contact: yes / no on a request
    CONFIRM = "confirm"  # the receiver: quarantine an email the contact denied


class UserRole(StrEnum):
    """Which screen a PWA shows. Mirrored in pwa/js/constants.js."""

    RECEIVER = "receiver"
    CONTACT = "contact"


class ActionOutcome(StrEnum):
    """What happened to one answer or Confirm. Every value but RECORDED is sent back as action_rejected."""

    RECORDED = "recorded"
    UNKNOWN_REQUEST = "unknown_request"
    WRONG_SENDER = "wrong_sender"  # the reply did not come from the contact's own channel
    NOT_APPLIED = "not_applied"    # duplicate, redelivered, or a Yes after the hold or after a No
    NOT_ALLOWED = "not_allowed"    # this user may not do this action
    BAD_MESSAGE = "bad_message"    # unparseable or missing fields
