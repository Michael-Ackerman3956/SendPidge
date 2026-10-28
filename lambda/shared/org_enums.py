"""Closed sets used only by org code (a home's office and its admins). Code branches on these, never on bare strings."""

from enum import StrEnum


class AdminRole(StrEnum):
    """What an org admin may do, stored on ORG#<orgId>/ADMIN#<userId>.role and written only by the org seed."""

    VIEWER = "viewer"
    SENDER = "sender"


class OrgVisibility(StrEnum):
    """What the resident lets the home's office see, stored on RECEIVER#<address>/ORGPREF#<orgId>.orgVisibility by the
    resident's own set_org_visibility. No row or no field reads as NONE (shared/org_preferences.read_org_visibility)."""

    ALERTS_ONLY = "alerts_only"
    ALERTS_AND_CASES = "alerts_and_cases"
    NONE = "none"


class OrgPreferenceSource(StrEnum):
    """Who wrote RECEIVER#<address>/ORGPREF#<orgId>, stored as orgVisibilitySource."""

    RESIDENT_APP = "resident_app"  # the resident's own PWA


class OrgEvent(StrEnum):
    """What happened in one member's episode, one ORGLOG#<orgId> row each, written only by pidge_org_projection. Declared in
    fold order: two events at the same time fold in this order (shared/org_events.episode_state)."""

    MESSAGE_HELD = "message_held"
    ASK_CAPPED = "ask_capped"
    ASK_TIMED_OUT = "ask_timed_out"
    CONTACT_CONFIRMED_YES = "contact_confirmed_yes"
    CONTACT_CONFIRMED_NOT_ME = "contact_confirmed_not_me"
    ANSWER_REVERSED = "answer_reversed"
    CONTACT_UNREACHABLE = "contact_unreachable"
    PAYMENT_DOUBTED = "payment_doubted"  # no writer yet
    WRONG_PAYMENT_REPORTED = "wrong_payment_reported"
    RECOVERY_PACK_DRAFTED = "recovery_pack_drafted"
    CASE_CLOSED = "case_closed"
    CONTACT_REQUEST = "contact_request"  # no writer yet
    BROADCAST_SENT = "broadcast_sent"  # no writer yet
    BROADCAST_ACKNOWLEDGED = "broadcast_acknowledged"  # no writer yet
    HELP_REQUESTED = "help_requested"  # no writer yet
    ORG_RESOLVED = "org_resolved"  # an admin marked the episode followed up (ORG#<orgId>/RESOLVE#<sourceKey>#<at>)


class MemberSafetyState(StrEnum):
    """How a member reads on the org dashboard. NOT_SHARED and NOTHING_OPEN are the member's; the rest are the state of one
    open episode (shared/org_events.EPISODE_TRANSITIONS)."""

    NOT_SHARED = "not_shared"  # the resident chose no visibility, or none
    NOTHING_OPEN = "nothing_open"
    HELD = "held"
    HELD_NO_ANSWER = "held_no_answer"
    CONFIRMED_NOT_ME = "confirmed_not_me"
    WARNED = "warned"
    CASE_OPEN = "case_open"
    CONTACT_UNREACHABLE = "contact_unreachable"


class OrgAuditAction(StrEnum):
    """What an admin did with member data, stored on ORGAUDIT#<orgId>/TS#<iso>#<uuid4>.action."""

    MEMBER_VIEW = "member_view"  # an admin sync: the dashboard was sent
    ANSWER = "answer"  # an in-app answer as the office
    ORG_RESOLVE = "org_resolve"
    BROADCAST_SEND = "broadcast_send"  # a safety alert sent to every member


class BroadcastKind(StrEnum):
    """What a broadcast is, on BROADCAST#<orgId>#<id>/META.kind. Only the in-app safety alert is built."""

    SAFETY_ALERT = "safety_alert"


class DeliveryState(StrEnum):
    """One member's copy of a broadcast, on BROADCAST#<orgId>#<id>/RECIPIENT#<receiverEmail>.state. Mirrored in
    pwa/js/constants.js."""

    SENT = "sent"  # written in-app, unanswered
    ACKNOWLEDGED = "acknowledged"  # she answered: ackAnswer holds which


class AckAnswer(StrEnum):
    """A resident's answer to a safety alert, on RECIPIENT#.ackAnswer. Mirrored in pwa/js/constants.js."""

    GOT_IT = "got_it"
    NEED_HELP = "need_help"


class StreamRecordEventName(StrEnum):
    """A DynamoDB stream record's eventName."""

    INSERT = "INSERT"
    MODIFY = "MODIFY"
    REMOVE = "REMOVE"


class ProjectedRecordKind(StrEnum):
    """Which rows pidge_org_projection reads from the stream, by key (the five stream filters in infra/lib/org-functions.ts)."""

    REQUEST_META = "request_meta"  # REQUEST#<id>/META
    REQUEST_NOTE = "request_note"  # REQUEST#<id>/NOTE#<writtenAt>
    EMAIL_META = "email_meta"  # EMAIL#<id>/META
    CASE_META = "case_meta"  # CASE#<id>/META
    ORG_RESOLVE = "org_resolve"  # ORG#<orgId>/RESOLVE#<sourceKey>#<at>
    ORG_CANARY = "org_canary"  # ORG#<orgId>/CANARY, put every 5 minutes by the scheduler
    RECEIVER_ORG_PREFERENCE = "receiver_org_preference"  # RECEIVER#<address>/ORGPREF#<orgId>
    RECEIVER_CONTACT = "receiver_contact"  # RECEIVER#<address>/CONTACT#<id>
    USER_CONNECTION = "user_connection"  # USER#<id>/CONN#<connectionId>
