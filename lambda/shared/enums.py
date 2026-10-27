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
    ASK_CAPPED = "ask_capped"          # held, but the contact already has the hour's asks open; nothing was sent


class ReasonCode(StrEnum):
    """Machine reasons attached to an email."""

    AUTH_MISSING = "auth_missing"      # no X-Pidge-Auth header at all
    AUTH_TAMPERED = "auth_tampered"    # more than one X-Pidge-Auth header
    AUTH_FAILED = "auth_failed"      # neither SPF nor DKIM passed at ingress
    UNKNOWN_ADDRESS = "unknown_address"        # gate 1: a contact's display name from an address they are not known to write from
    ESCALATION_CLEARED = "escalation_cleared"  # the second reviewer said a mark is enough (gate 2 only)
    ESCALATION_FAILED = "escalation_failed"    # the second reviewer could not answer; the hold stays
    PERSON_CONFLICT = "person_conflict"        # the two models named different people; the hold stays
    LINK_TEXT_HREF_MISMATCH = "link_text_href_mismatch"  # a link's visible text names one domain, its href another
    HIDDEN_LINK = "hidden_link"                          # an anchor with no visible text
    KNOWN_SHORTENER = "known_shortener"                  # a link through a public URL shortener
    LOOKALIKE_LINK_DOMAIN = "lookalike_link_domain"      # a link domain that imitates a family domain
    LINKS_UNREADABLE = "links_unreadable"                # the HTML part would not parse, so the links are unknown
    LISTED_SENDER_ADDRESS = "listed_sender_address"      # the exact sender address is on the scam list (sample rows in the POC)
    LISTED_SENDER_DOMAIN = "listed_sender_domain"        # the sender's registrable domain is on the scam list
    LOOKALIKE_SENDER_NAME = "lookalike_sender_name"      # a display name one edit or one confusable away from a contact's name
    LOOKALIKE_SENDER_DOMAIN = "lookalike_sender_domain"  # a sender domain that imitates a family domain
    REPORTED_SENDER = "reported_sender"                  # tier 0: Grandma reported this exact address before (Confirm)
    LIST_LOOKUP_FAILED = "list_lookup_failed"            # the scam-list read failed; the email is marked, never passed silently
    WHOLE_BODY_LINK = "whole_body_link"                  # one anchor's text is most of the HTML body, so any click goes there
    SCRIPT_HREF_LINK = "script_href_link"                # a javascript:, vbscript: or data: href that runs code instead of opening a page
    CSS_HIDDEN_LINK = "css_hidden_link"                  # an anchor hidden by its own or an ancestor's inline style or hidden attribute
    IMAGE_MAP_LINK = "image_map_link"                    # an <area href> inside an image map
    HTML_ATTACHMENT_LINK = "html_attachment_link"        # an anchor in an HTML or SVG part other than the body (parsed only, never opened)
    LINKS_TRUNCATED = "links_truncated"                  # more links than the cap; the ones past it were not checked
    IDN_LINK_DOMAIN = "idn_link_domain"                  # a punycode or non-ASCII link host (mixed-script or undecodable)
    IMAGE_ONLY_BODY = "image_only_body"                  # the HTML body is images with under 40 visible characters


LINK_REASON_CODES = frozenset({
    ReasonCode.LINK_TEXT_HREF_MISMATCH, ReasonCode.HIDDEN_LINK, ReasonCode.KNOWN_SHORTENER,
    ReasonCode.LOOKALIKE_LINK_DOMAIN, ReasonCode.LINKS_UNREADABLE,
    ReasonCode.WHOLE_BODY_LINK, ReasonCode.SCRIPT_HREF_LINK, ReasonCode.CSS_HIDDEN_LINK, ReasonCode.IMAGE_MAP_LINK,
    ReasonCode.HTML_ATTACHMENT_LINK, ReasonCode.LINKS_TRUNCATED, ReasonCode.IDN_LINK_DOMAIN, ReasonCode.IMAGE_ONLY_BODY,
})


class LinkKind(StrEnum):
    """What a link's href does when followed, from its scheme alone."""

    WEB = "web"        # http or https
    MAILTO = "mailto"
    TEL = "tel"
    SCRIPT = "script"  # javascript: or vbscript:
    DATA = "data"      # data: (an inline page or file)
    OTHER = "other"    # fragments, bare paths and any other scheme


class HiddenBy(StrEnum):
    """How a link hides what it is, if it does."""

    NONE = "none"
    NO_TEXT = "no_text"        # no visible text and no image
    IMAGE_ONLY = "image_only"  # an image (or an image-map region) is the only thing to click
    CSS = "css"                # an inline style (display:none, visibility:hidden or collapse, mso-hide:all, opacity under 0.1,
                               # font-size under 1px) or the hidden attribute, on the anchor or an ancestor


class LinkSource(StrEnum):
    """Where in the email a link was found."""

    HTML_PART = "html_part"              # an <a href> in the HTML body
    IMAGE_MAP = "image_map"              # an <area href> in the HTML body
    HTML_ATTACHMENT = "html_attachment"  # an <a href> in any other HTML or SVG part: an attachment, a forward, a second HTML part


class IndicatorKind(StrEnum):
    """What a LIST# row keys on: LIST#<kind> / <normalised value>."""

    ADDRESS = "address"      # a full sender address, lower-cased
    DOMAIN = "domain"        # a registrable domain
    LOOKALIKE = "lookalike"  # the confusable skeleton of a contact's name
    PHONE = "phone"          # E.164 (seeded, not yet queried)
    URL = "url"              # a registrable domain or full URL (seeded, not yet queried)


class ListCategory(StrEnum):
    """Why a value is listed."""

    SCAM = "scam"
    LOOKALIKE = "lookalike"


class ListStrength(StrEnum):
    """How much a hit means on its own: a reason, never a verdict."""

    LISTED = "listed"
    SUSPECT = "suspect"


class ListSource(StrEnum):
    """Where a LIST# row came from. Only sample rows and Pidge's own confirms exist in the POC."""

    SAMPLE = "sample"                    # hand-written infra/seed/lists.sample.json, labelled as sample data in the PWA
    PIDGE_CONFIRMED = "pidge-confirmed"  # tier 0: written by pidge-verdict on Confirm


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
    """What a contact can answer. Phone taps carry YES and NO; UNDO comes from the PWA; REVERSE from the PWA or the WhatsApp
    'I made a mistake' button."""

    YES = "yes"
    NO = "no"
    UNDO = "undo"        # take back a Yes while it is still confirmed_pending
    REVERSE = "reverse"  # take back a No within the reverse window ("I made a mistake", PWA or WhatsApp)


PHONE_CHANNEL_ANSWERS = (VerdictAnswer.YES, VerdictAnswer.NO)
FINAL_REQUEST_STATES = (RequestState.DENIED, RequestState.CONFIRMED, RequestState.REVERSED_NO)


class Channel(StrEnum):
    """Where a 'Was this you?' ask can be sent, and where the suspicious message came in (REQUEST#.inboundChannel).
    Mirrored in pwa/js/constants.js."""

    RCS = "rcs"
    WHATSAPP = "whatsapp"
    INAPP = "inapp"
    EMAIL = "email"  # the ask goes out through SES; only a No may come back this way (verdict.record_answer)


class AskLinkOutcome(StrEnum):
    """What ask_link.verify_no_link found; only VALID may reach record_answer."""

    VALID = "valid"
    MALFORMED = "malformed"
    EXPIRED = "expired"
    BAD_SIGNATURE = "bad_signature"


class EmailedAnswerOutcome(StrEnum):
    """What happened to one reply mailed to pidge@ under asks/: only ACCEPTED reaches record_answer; the rest are dropped."""

    ACCEPTED = "accepted"
    NO_REQUEST_ID = "no_request_id"
    DKIM_NOT_PASSED = "dkim_not_passed"
    DKIM_NOT_ALIGNED = "dkim_not_aligned"
    NOT_AN_ANSWER = "not_an_answer"


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


class CallPromise(StrEnum):
    """When the contact promised to call the receiver, as tapped on the call offer."""

    TODAY = "today"
    TOMORROW = "tomorrow"


class CallNudgeState(StrEnum):
    """Lifecycle of the 'call Grandma?' offer sent to the contact after a verdict."""

    OFFERED = "offered"                  # the reply-button offer was sent
    CAPPED = "capped"                    # not sent: the contact already got the allowed offers
    OFFER_FAILED = "offer_failed"        # the send raised, or Meta reported the offer failed
    NOT_ON_WHATSAPP = "not_on_whatsapp"  # the contact has no WhatsApp number, so nothing was sent
    PROMISED = "promised"                # the contact tapped today or tomorrow
    REMINDED = "reminded"                # the reminder text was sent
    REMINDER_FAILED = "reminder_failed"  # the reminder send raised, or Meta reported it failed


PROMISED_CALL_NUDGE_STATES = (CallNudgeState.PROMISED, CallNudgeState.REMINDED, CallNudgeState.REMINDER_FAILED)


class ReaderModelChoice(StrEnum):
    """Allowed values of the READER_MODEL setting."""

    HAIKU_5_5 = "haiku-5.5"
    HAIKU_4_5 = "haiku-4.5"
    NOVA_MICRO = "nova-micro"
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
    PIDGE_REPLY = "pidge_reply"              # to the receiver: Pidge's answer to her question, or a refusal
    CONTACT_ADDED = "contact_added"          # to the receiver's own socket once: the new circle entry, the note to send and its code
    CIRCLE_STATE = "circle_state"            # to the receiver: the circle changed (someone added, edited or verified)
    CONTACT_EDITED = "contact_edited"        # to her own socket once: the edit is saved; a note and code when it added a number


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
    ASK_PIDGE = "ask_pidge"                            # the receiver: a question typed in the Pidge chat window
    ADD_CONTACT = "add_contact"                        # the receiver: the Add-someone form → a pending CONTACT# row
    VERIFY_CONTACT = "verify_contact"                  # the receiver: plays the contact's code reply (CONTACT_VERIFICATION_MOCK only)
    EDIT_CONTACT = "edit_contact"                      # the receiver: the same form saved on an existing CONTACT# row


class PushNotice(StrEnum):
    """Which Web Push notification is sent; each maps to one fixed sentence in shared/web_push.py, never email text."""

    BANK_REPLIED = "bank_replied"
    FAMILY_CALL_TODAY = "family_call_today"        # the contact promised to call the receiver today
    FAMILY_CALL_TOMORROW = "family_call_tomorrow"  # the contact promised to call the receiver tomorrow


class VerifiedVia(StrEnum):
    """Whether and how a CONTACT# row was verified. Mirrored in pwa/js/constants.js. A PENDING contact is never asked."""

    PENDING = "pending"    # added from the app; the code has not come back from the contact's own number yet
    SEED = "seed"          # written by scripts/seed_graph.py
    MOCK = "mock"          # the receiver's own PWA played the reply with CONTACT_VERIFICATION_MOCK on (labelled demo)
    WHATSAPP = "whatsapp"  # the code arrived as a WhatsApp text from that number


class RecoveryDraftKind(StrEnum):
    """The three code-written drafts in a recovery pack (keys of recoveryPack.drafts). Mirrored in pwa/js/constants.js."""

    CALL_SCRIPT = "callScript"
    DISPUTE_LETTER = "disputeLetter"
    POLICE_REPORT = "policeReport"


class Relationship(StrEnum):
    """Who the contact is to the receiver, as shown on the circle row. Mirrored in pwa/js/constants.js."""

    SON = "son"
    DAUGHTER = "daughter"
    GRANDSON = "grandson"
    GRANDDAUGHTER = "granddaughter"
    SPOUSE = "spouse"
    SIBLING = "sibling"
    FRIEND = "friend"
    CARER = "carer"
    OTHER = "other"


class ChatRole(StrEnum):
    """Who wrote one CHAT# message row."""

    GRANDMA = "grandma"
    PIDGE = "pidge"


class ChatVia(StrEnum):
    """How the receiver's question arrived."""

    WINDOW = "window"
    EMAIL = "email"


class ChatCardKind(StrEnum):
    """The card a Pidge reply carries. Mirrored in pwa/js/constants.js."""

    REFUSAL = "refusal"


class GuardrailOutcome(StrEnum):
    """The prompt-attack check on a question; a failed call raises instead of returning."""

    PASSED = "passed"
    INTERVENED = "intervened"


class EmailedQuestionOutcome(StrEnum):
    """What happened to one email sent to pidge@: only ACCEPTED writes a CHAT# row; every other value drops the email."""

    ACCEPTED = "accepted"
    NOT_FROM_RECEIVER = "not_from_receiver"
    DKIM_NOT_PASSED = "dkim_not_passed"
    DKIM_NOT_ALIGNED = "dkim_not_aligned"
    REFUSED = "refused"


class AgentInvocationKind(StrEnum):
    """What pidge-dispatch asks the AgentCore runtime for; a payload without a kind is a case note."""

    CASE_NOTE = "case_note"
    CHAT = "chat"


class UserRole(StrEnum):
    """Which screen a PWA shows. Mirrored in pwa/js/constants.js."""

    RECEIVER = "receiver"
    CONTACT = "contact"
    ORG_ADMIN = "org_admin"  # no PWA screen yet


class ActionOutcome(StrEnum):
    """What happened to one answer or Confirm. Every value but RECORDED is sent back as action_rejected."""

    RECORDED = "recorded"
    UNKNOWN_REQUEST = "unknown_request"
    WRONG_SENDER = "wrong_sender"  # the reply did not come from the contact's own channel
    WRONG_CHANNEL = "wrong_channel"  # a Yes on the channel the suspicious message arrived on (a hijacked mailbox could send it)
    NOT_APPLIED = "not_applied"    # duplicate, redelivered, or a Yes after the hold or after a No
    NOT_ALLOWED = "not_allowed"    # this user may not do this action
    BAD_MESSAGE = "bad_message"    # unparseable or missing fields
    DUPLICATE_NAME = "duplicate_name"  # add_contact: the circle already has that name (asks are matched by name)
    CIRCLE_FULL = "circle_full"        # add_contact: the circle has CIRCLE_MAXIMUM_SIZE people
    LAST_CHECKED_NUMBER = "last_checked_number"  # edit_contact: the edit drops every checked WhatsApp number


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
    CALL_PROMISED = "call_promised"  # the contact promised to call Grandma


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


class UnsafeTextKind(StrEnum):
    """What the output gate found in agent text bound for a person; any one refuses the text."""

    LINK = "link"
    EMAIL_ADDRESS = "email_address"
    MESSENGER_HANDLE = "messenger_handle"  # "@refund_desk" on Telegram, Instagram or X
    LONG_NUMBER = "long_number"  # 6+ digits: a phone, an account, a code; not a calendar date


class AgentRunFailure(StrEnum):
    """Why one tool-holding agent run produced no output."""

    TURN_LIMIT = "turn_limit"
    NO_OUTPUT = "no_output"
