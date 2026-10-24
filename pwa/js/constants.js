// Closed sets shared with the backend. Mirrors lambda/shared/enums.py exactly; code branches on these, never on bare strings.

export const PUSH_MESSAGE_TYPE = Object.freeze({
  EMAIL_STATE: "email_state",
  VERIFICATION_CARD: "verification_card",
  REQUEST_STATE: "request_state",
  SYNC_STATE: "sync_state",
  ACTION_REJECTED: "action_rejected",
  CASE_NOTE: "case_note",
  CASE_STATE: "case_state",
  CONTACT_NOTICE: "contact_notice",
});

export const CLIENT_ACTION = Object.freeze({
  SYNC: "sync",
  ANSWER: "answer",
  UNDO: "undo",
  REVERSE: "reverse",
  CONFIRM: "confirm",
  REPORT_PAYMENT: "report_payment",
  SEND_REPORT: "send_report",
  CLOSE_CASE: "close_case",
});

export const CASE_STATUS = Object.freeze({
  OPEN: "open",
  REPORTED: "reported",
  CLOSED: "closed",
});

export const CASE_EVENT = Object.freeze({
  OPENED: "opened",
  REPORT_SENT: "report_sent",
  BANK_REPLIED: "bank_replied",
  FOLLOWED_UP: "followed_up",
  CLOSED: "closed",
});

export const PAYMENT_RAIL = Object.freeze({
  CARD: "card",
  ZELLE: "zelle",
  WIRE: "wire",
  GIFT_CARD: "gift_card",
});

export const JURISDICTION = Object.freeze({
  US: "US",
  KR: "KR",
  GB: "GB",
});

export const USER_ROLE = Object.freeze({
  RECEIVER: "receiver",
  CONTACT: "contact",
});

export const REQUEST_STATUS = Object.freeze({
  PENDING: "pending",
  DENIED: "denied",
  CONFIRMED_PENDING: "confirmed_pending",
  CONFIRMED: "confirmed",
  TIMEOUT: "timeout",
  REVERSED_NO: "reversed_no",
});

export const VERDICT_ANSWER = Object.freeze({
  YES: "yes",
  NO: "no",
  UNDO: "undo",
  REVERSE: "reverse",
});

export const ACTION_REJECTED_REASON = Object.freeze({
  UNKNOWN_REQUEST: "unknown_request",
  WRONG_SENDER: "wrong_sender",
  NOT_APPLIED: "not_applied",
  NOT_ALLOWED: "not_allowed",
  BAD_MESSAGE: "bad_message",
});

export const POLICY_DECISION = Object.freeze({
  HOLD: "hold",
  WARN: "warn",
  PASS: "pass",
});

export const WARN_REASON = Object.freeze({
  UNREADABLE: "unreadable",
  UNKNOWN_NAME: "unknown_name",
  LOW_CONFIDENCE: "low_confidence",
  LISTED_SENDER: "listed_sender",
  UNREACHABLE: "unreachable",
  ESCALATION_CLEARED: "escalation_cleared",
});

export const REASON_CODE = Object.freeze({
  AUTH_MISSING: "auth_missing",
  AUTH_TAMPERED: "auth_tampered",
  AUTH_FAILED: "auth_failed",
  UNKNOWN_ADDRESS: "unknown_address",
  ESCALATION_CLEARED: "escalation_cleared",
  ESCALATION_FAILED: "escalation_failed",
  PERSON_CONFLICT: "person_conflict",
});

export const FRICTION = Object.freeze({
  SINGLE_TAP: "single_tap",
  DOUBLE_TAP: "double_tap",
});

export const INTENT = Object.freeze({
  MONEY: "money",
  CODE: "code",
  URGENT_ACTION: "urgent_action",
  NONE: "none",
});

// PWA-only: WebSocket connection state shown as the top bar dot.
export const CONNECTION_STATUS = Object.freeze({
  CONNECTED: "connected",
  RECONNECTING: "reconnecting",
});

// PWA-only: how one inbox row and its detail look, derived from state, request.status, quarantined and local release.
export const EMAIL_DISPLAY_STATE = Object.freeze({
  QUARANTINED: "quarantined",
  DENIED: "denied",
  RELEASING: "releasing",
  CONFIRMED: "confirmed",
  REVERSED: "reversed",
  RELEASED_BY_USER: "released_by_user",
  AWAITING_VERDICT: "awaiting_verdict",
  FLAGGED: "flagged",
  PLAIN: "plain",
});
