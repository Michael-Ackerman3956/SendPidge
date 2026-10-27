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
  PIDGE_REPLY: "pidge_reply",
  CONTACT_ADDED: "contact_added",
  CIRCLE_STATE: "circle_state",
  CONTACT_EDITED: "contact_edited",
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
  SAVE_PUSH_SUBSCRIPTION: "save_push_subscription",
  ASK_PIDGE: "ask_pidge",
  ADD_CONTACT: "add_contact",
  VERIFY_CONTACT: "verify_contact",
  EDIT_CONTACT: "edit_contact",
});

export const CHAT_CARD_KIND = Object.freeze({
  REFUSAL: "refusal",
});

export const CHAT_ROLE = Object.freeze({
  GRANDMA: "grandma",
  PIDGE: "pidge",
});

export const VERIFIED_VIA = Object.freeze({
  PENDING: "pending",
  SEED: "seed",
  MOCK: "mock",
  WHATSAPP: "whatsapp",
});

export const RELATIONSHIP = Object.freeze({
  SON: "son",
  DAUGHTER: "daughter",
  GRANDSON: "grandson",
  GRANDDAUGHTER: "granddaughter",
  SPOUSE: "spouse",
  SIBLING: "sibling",
  FRIEND: "friend",
  CARER: "carer",
  OTHER: "other",
});

export const RELATIONSHIP_LABELS = Object.freeze({
  [RELATIONSHIP.SON]: "Son",
  [RELATIONSHIP.DAUGHTER]: "Daughter",
  [RELATIONSHIP.GRANDSON]: "Grandson",
  [RELATIONSHIP.GRANDDAUGHTER]: "Granddaughter",
  [RELATIONSHIP.SPOUSE]: "Spouse",
  [RELATIONSHIP.SIBLING]: "Sibling",
  [RELATIONSHIP.FRIEND]: "Friend",
  [RELATIONSHIP.CARER]: "Carer",
  [RELATIONSHIP.OTHER]: "Other",
});

export const RECOVERY_DRAFT_KIND = Object.freeze({
  CALL_SCRIPT: "callScript",
  DISPUTE_LETTER: "disputeLetter",
  POLICE_REPORT: "policeReport",
});

export const CASES_TAB = Object.freeze({ OPEN: "open", CLOSED: "closed", NOTES: "notes" });

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

// Where an ask goes out and where an answer comes back (request.repliedVia). Mirrors Channel in enums.py.
export const CHANNEL = Object.freeze({
  RCS: "rcs",
  WHATSAPP: "whatsapp",
  INAPP: "inapp",
  EMAIL: "email",
});

export const USER_ROLE = Object.freeze({
  RECEIVER: "receiver",
  CONTACT: "contact",
  ORG_ADMIN: "org_admin",
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
  DUPLICATE_NAME: "duplicate_name",
  CIRCLE_FULL: "circle_full",
  LAST_CHECKED_NUMBER: "last_checked_number",
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
  LINK_REASONS: "link_reasons",
  ASK_CAPPED: "ask_capped",
});

export const REASON_CODE = Object.freeze({
  AUTH_MISSING: "auth_missing",
  AUTH_TAMPERED: "auth_tampered",
  AUTH_FAILED: "auth_failed",
  UNKNOWN_ADDRESS: "unknown_address",
  ESCALATION_CLEARED: "escalation_cleared",
  ESCALATION_FAILED: "escalation_failed",
  PERSON_CONFLICT: "person_conflict",
  LINK_TEXT_HREF_MISMATCH: "link_text_href_mismatch",
  HIDDEN_LINK: "hidden_link",
  KNOWN_SHORTENER: "known_shortener",
  LOOKALIKE_LINK_DOMAIN: "lookalike_link_domain",
  LINKS_UNREADABLE: "links_unreadable",
  LISTED_SENDER_ADDRESS: "listed_sender_address",
  LISTED_SENDER_DOMAIN: "listed_sender_domain",
  LOOKALIKE_SENDER_NAME: "lookalike_sender_name",
  LOOKALIKE_SENDER_DOMAIN: "lookalike_sender_domain",
  REPORTED_SENDER: "reported_sender",
  LIST_LOOKUP_FAILED: "list_lookup_failed",
  WHOLE_BODY_LINK: "whole_body_link",
  SCRIPT_HREF_LINK: "script_href_link",
  CSS_HIDDEN_LINK: "css_hidden_link",
  IMAGE_MAP_LINK: "image_map_link",
  HTML_ATTACHMENT_LINK: "html_attachment_link",
  LINKS_TRUNCATED: "links_truncated",
  IDN_LINK_DOMAIN: "idn_link_domain",
  IMAGE_ONLY_BODY: "image_only_body",
});

// What a link's href does (links[].kind). Mirrors LinkKind in enums.py.
export const LINK_KIND = Object.freeze({
  WEB: "web",
  MAILTO: "mailto",
  TEL: "tel",
  SCRIPT: "script",
  DATA: "data",
  OTHER: "other",
});

// How a link hides what it is (links[].hiddenBy). Mirrors HiddenBy in enums.py.
export const HIDDEN_BY = Object.freeze({
  NONE: "none",
  NO_TEXT: "no_text",
  IMAGE_ONLY: "image_only",
  CSS: "css",
});

export const CALL_PROMISE = Object.freeze({
  TODAY: "today",
  TOMORROW: "tomorrow",
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
// READING is an email_state whose state is null: the Reader is still on it and the body is withheld.
export const EMAIL_DISPLAY_STATE = Object.freeze({
  READING: "reading",
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

// PWA-only: which channel a mock inbox row came from (live rows are always email until slice 15).
export const ROW_KIND = Object.freeze({
  EMAIL: "email",
  SMS: "sms",
  APP: "app",
});

// PWA-only: Gmail's reading-pane choices on a wide screen; phones always read with no split.
export const READING_PANE_LAYOUT = Object.freeze({
  NO_SPLIT: "no_split",
  VERTICAL_SPLIT: "vertical_split",
  HORIZONTAL_SPLIT: "horizontal_split",
});
