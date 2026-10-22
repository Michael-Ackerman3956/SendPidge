// Closed sets shared with the backend. Mirrors lambda/shared/enums.py exactly; code branches on these, never on bare strings.

export const PUSH_MESSAGE_TYPE = Object.freeze({
  EMAIL_STATE: "email_state",
  VERIFICATION_CARD: "verification_card",
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
});

export const REASON_CODE = Object.freeze({
  AUTH_MISSING: "auth_missing",
  AUTH_TAMPERED: "auth_tampered",
  DMARC_FAIL: "dmarc_fail",
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
