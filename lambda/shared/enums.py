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


class ReasonCode(StrEnum):
    """Machine reasons attached to an email. Lookup tables add more in a later slice."""

    AUTH_MISSING = "auth_missing"      # no X-Pidge-Auth header at all
    AUTH_TAMPERED = "auth_tampered"    # more than one X-Pidge-Auth header
    AUTH_FAILED = "auth_failed"      # neither SPF nor DKIM passed at ingress


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


class ReaderModelChoice(StrEnum):
    """Allowed values of the READER_MODEL setting."""

    HAIKU_5_5 = "haiku-5.5"
    HAIKU_4_5 = "haiku-4.5"
    NOVA_MICRO = "nova-micro"
    GEMMA_4_E2B = "gemma-4-e2b"


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
