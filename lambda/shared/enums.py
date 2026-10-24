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
    LINK_REASONS = "link_reasons"      # no ask, and every reason is about a link


class ReasonCode(StrEnum):
    """Machine reasons attached to an email. Lookup tables add more in a later slice."""

    AUTH_MISSING = "auth_missing"      # no X-Pidge-Auth header at all
    AUTH_TAMPERED = "auth_tampered"    # more than one X-Pidge-Auth header
    AUTH_FAILED = "auth_failed"      # neither SPF nor DKIM passed at ingress
    UNKNOWN_ADDRESS = "unknown_address"        # gate 1: a contact's display name from an address not in knownAddresses
    ESCALATION_CLEARED = "escalation_cleared"  # the second reviewer said a mark is enough (gate 2 only)
    ESCALATION_FAILED = "escalation_failed"    # the second reviewer could not answer; the hold stays
    PERSON_CONFLICT = "person_conflict"        # the two models named different people; the hold stays
    LINK_TEXT_HREF_MISMATCH = "link_text_href_mismatch"  # a link's visible text names one domain, its href another
    HIDDEN_LINK = "hidden_link"                          # an anchor with no visible text
    KNOWN_SHORTENER = "known_shortener"                  # a link through a public URL shortener
    LOOKALIKE_LINK_DOMAIN = "lookalike_link_domain"      # a link domain that imitates a family domain
    LINKS_UNREADABLE = "links_unreadable"                # the HTML part would not parse, so the links are unknown


LINK_REASON_CODES = frozenset({
    ReasonCode.LINK_TEXT_HREF_MISMATCH, ReasonCode.HIDDEN_LINK, ReasonCode.KNOWN_SHORTENER,
    ReasonCode.LOOKALIKE_LINK_DOMAIN, ReasonCode.LINKS_UNREADABLE,
})


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
    CONFIRMED_PENDING = "confirmed_pending"  # a Yes, waiting RELEASE_DELAY_SECONDS for an Undo
    CONFIRMED = "confirmed"
    TIMEOUT = "timeout"
    REVERSED_NO = "reversed_no"  # a No the contact took back within the reverse window


class VerdictAnswer(StrEnum):
    """What a contact can answer. Phone channels carry only YES and NO; UNDO and REVERSE come from the PWA."""

    YES = "yes"
    NO = "no"
    UNDO = "undo"        # take back a Yes while it is still confirmed_pending
    REVERSE = "reverse"  # take back a No within the reverse window ("I made a mistake")


PHONE_CHANNEL_ANSWERS = (VerdictAnswer.YES, VerdictAnswer.NO)
FINAL_REQUEST_STATES = (RequestState.DENIED, RequestState.CONFIRMED, RequestState.REVERSED_NO)


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
    CASE_NOTE = "case_note"                  # to the receiver: the case agent wrote a note (and maybe a recovery pack)
    CASE_STATE = "case_state"                # to the receiver: a case changed (sent, bank replied, reminded, closed)
    CONTACT_NOTICE = "contact_notice"        # to the contact: one fixed sentence (the bank replied to the receiver's case)


class ClientAction(StrEnum):
    """What a PWA may send on the WebSocket $default route. Mirrored in pwa/js/constants.js."""

    SYNC = "sync"        # either user, right after connecting
    ANSWER = "answer"    # the contact: yes / no on a request
    UNDO = "undo"        # the contact: take back a Yes within the release delay
    REVERSE = "reverse"  # the contact: take back a No within the reverse window
    CONFIRM = "confirm"  # the receiver: quarantine an email the contact denied
    REPORT_PAYMENT = "report_payment"  # the receiver: "I already paid" → a CASE# row
    SEND_REPORT = "send_report"        # the receiver: "Send to my bank" → code emails the SIMULATED bank
    CLOSE_CASE = "close_case"          # the receiver: "Done" → the case is closed
    SAVE_PUSH_SUBSCRIPTION = "save_push_subscription"  # either user: this browser may get Web Push notifications



class PushNotice(StrEnum):
    """Which Web Push notification is sent; each maps to one fixed sentence in shared/web_push.py, never email text."""

    BANK_REPLIED = "bank_replied"

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


class CaseState(StrEnum):
    """Lifecycle of a lost-payment case."""

    OPEN = "open"
    REPORTED = "reported"
    CLOSED = "closed"


class PaymentRail(StrEnum):
    """How the money left, as Grandma picks it."""

    CARD = "card"
    ZELLE = "zelle"
    WIRE = "wire"
    GIFT_CARD = "gift_card"


class Jurisdiction(StrEnum):
    """Which country's refund rules apply."""

    US = "US"
    KR = "KR"
    GB = "GB"


class RuleState(StrEnum):
    """A refund rule version's status; only live rows are ever read for a recovery pack."""

    LIVE = "live"
    PROPOSED = "proposed"
    REJECTED = "rejected"


class ProposedBy(StrEnum):
    """Who wrote a refund rule version."""

    SEED = "seed"
    ADMIN = "admin"
    KEEPER = "keeper"


class CaseTrigger(StrEnum):
    """What woke the case agent."""

    VERDICT = "verdict"          # a REQUEST# reached a final state
    CASE = "case"                # Grandma reported a payment (CASE# written)
    CASE_CLOSED = "case_closed"  # Grandma tapped Done on the case


class CaseEvent(StrEnum):
    """What just happened to a case, as pushed with CASE_STATE."""

    OPENED = "opened"
    REPORT_SENT = "report_sent"
    BANK_REPLIED = "bank_replied"
    FOLLOWED_UP = "followed_up"
    CLOSED = "closed"


class CaseAgentRuntime(StrEnum):
    """Where the case agent runs."""

    AGENTCORE = "agentcore"
    LAMBDA = "lambda"
