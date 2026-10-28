// Grandma's incoming data: the three static email mock rows and validation of every email_state, message_state and request_state
// payload. A text (a MESSAGE# row, in or out) becomes the same row shape as an email, with rowKind from its channel; every row
// also carries its channel, direction and threadKey (the conversation it belongs to).
import {
  CALL_PROMISE, CHANNEL, MESSAGE_DIRECTION, POLICY_DECISION, PROVIDER_STATUS, REQUEST_STATUS, ROW_KIND, SCAM_LIST_COVERAGE,
} from "./constants.js";

const MILLISECONDS_PER_HOUR = 3600000;
const KNOWN_POLICY_DECISIONS = new Set(Object.values(POLICY_DECISION));
const KNOWN_REQUEST_STATUSES = new Set(Object.values(REQUEST_STATUS));
const KNOWN_CALL_PROMISES = new Set(Object.values(CALL_PROMISE));
const KNOWN_CHANNELS = new Set(Object.values(CHANNEL));
const KNOWN_SCAM_LIST_COVERAGES = new Set(Object.values(SCAM_LIST_COVERAGE));
const KNOWN_PROVIDER_STATUSES = new Set(Object.values(PROVIDER_STATUS));
// The channels a text comes in on and the row kind each shows as; a message on any other channel is dropped, never shown as email.
const MESSAGE_ROW_KINDS = Object.freeze({
  [CHANNEL.SMS]: ROW_KIND.SMS,
  [CHANNEL.WHATSAPP]: ROW_KIND.WHATSAPP,
  [CHANNEL.RCS]: ROW_KIND.RCS,
});
// The channels Grandma sends on (an out row) and the row kind each shows as: the text channels and email.
const OUT_MESSAGE_ROW_KINDS = Object.freeze({ ...MESSAGE_ROW_KINDS, [CHANNEL.EMAIL]: ROW_KIND.EMAIL });

export function buildMockFamilyEmails() {
  const now = Date.now();
  const receivedHoursAgo = (hours) => new Date(now - hours * MILLISECONDS_PER_HOUR).toISOString();
  const mockRow = (emailId, senderName, senderAddress, subject, hoursAgo, textBody) => ({
    emailId, rowKind: ROW_KIND.EMAIL, senderName, senderAddress, subject, receivedAt: receivedHoursAgo(hoursAgo), textBody,
    state: POLICY_DECISION.PASS, warnReason: null, reasons: [], links: [], request: null, quarantined: false,
    channel: CHANNEL.EMAIL, direction: MESSAGE_DIRECTION.IN, threadKey: null,
  });
  return [
    mockRow("mock-sunday-lunch", "Minsoo", "minsoo@example.com", "Sunday lunch", 2,
      "Hi Mom, we'll come by around noon on Sunday. I'll bring the kimchi jjigae you like. See you then!"),
    mockRow("mock-garden-photos", "Young-hee", "younghee@example.com", "Your tomatoes!", 27,
      "The tomatoes from your garden were so sweet. Can you show me how you grow them next time?"),
    mockRow("mock-church-picnic", "Pastor Kim", "pastor.kim@example.com", "Picnic next Saturday", 75,
      "Just a reminder that the church picnic is next Saturday at the park. Hope to see you there."),
  ];
}

function isValidEmailRequest(request) {
  if (request === null || request === undefined) return true;
  return typeof request === "object" && typeof request.requestId === "string"
    && typeof request.contactName === "string" && KNOWN_REQUEST_STATUSES.has(request.status);
}

// quarantined may be absent only on a slice-2 email_state (a new email cannot be quarantined); any other type is rejected.
function isValidQuarantinedField(quarantined) {
  return quarantined === undefined || typeof quarantined === "boolean";
}

// state, request and quarantined: the verdict fields every inbox row carries, email or text.
function hasValidVerdictFields(row) {
  return (row.state === null || KNOWN_POLICY_DECISIONS.has(row.state))
    && isValidEmailRequest(row.request) && isValidQuarantinedField(row.quarantined);
}

function reportHoldWithoutRequest(row, rowId) {
  if (row.state === POLICY_DECISION.HOLD && (row.request === null || row.request === undefined)) {
    console.error("SendPidge: HOLD push arrived without a request; showing the row flagged without the overlay", rowId);
  }
}

function isValidEmailPayload(email) {
  const hasRequiredFields = email !== null && typeof email === "object" && typeof email.emailId === "string"
    && typeof email.senderName === "string" && typeof email.subject === "string" && typeof email.textBody === "string";
  if (!hasRequiredFields || !hasValidVerdictFields(email)) {
    console.error("SendPidge: email push has a missing field, unknown state or unknown request status", email);
    return false;
  }
  reportHoldWithoutRequest(email, email.emailId);
  return true;
}

// Only incoming texts on a known text channel are listed; any other channel or direction is logged and dropped.
function isValidMessagePayload(message) {
  const hasRequiredFields = message !== null && typeof message === "object" && typeof message.messageId === "string"
    && typeof message.senderName === "string" && typeof message.senderAddress === "string" && typeof message.textBody === "string";
  const isIncomingText = hasRequiredFields && Object.prototype.hasOwnProperty.call(MESSAGE_ROW_KINDS, message.channel)
    && message.direction === MESSAGE_DIRECTION.IN;
  if (!isIncomingText || !hasValidVerdictFields(message)) {
    console.error("SendPidge: message push has a missing field, unknown channel or direction, unknown state or unknown request status",
      message);
    return false;
  }
  reportHoldWithoutRequest(message, message.messageId);
  return true;
}

// Grandma's own sent text or email: who it went to, when, and where its send stands.
function isValidOutMessagePayload(message) {
  const isValid = message !== null && typeof message === "object" && typeof message.messageId === "string"
    && Object.prototype.hasOwnProperty.call(OUT_MESSAGE_ROW_KINDS, message.channel) && message.direction === MESSAGE_DIRECTION.OUT
    && typeof message.threadKey === "string" && typeof message.recipientName === "string"
    && typeof message.recipientAddress === "string" && typeof message.textBody === "string" && typeof message.sentAt === "string"
    && KNOWN_PROVIDER_STATUSES.has(message.providerStatus);
  if (!isValid) console.error("SendPidge: sent message push has a missing field, unknown channel or unknown providerStatus", message);
  return isValid;
}

// The conversation a row belongs to (lambda/shared/inbox_rows.py thread_key_for), or null when absent (a row written before
// slice 16); a non-text value is logged and read as null, so the row offers no Reply.
function normalizeThreadKey(row) {
  if (row.threadKey === undefined || row.threadKey === null) return null;
  if (typeof row.threadKey === "string") return row.threadKey;
  console.error("SendPidge: row carries a non-text threadKey", row.threadKey);
  return null;
}

function isValidEmailLink(link) {
  return link !== null && typeof link === "object" && typeof link.text === "string" && typeof link.domain === "string"
    && typeof link.href === "string" && typeof link.flagged === "boolean";
}

// links is absent on rows written before slice 9a (then []); a malformed entry is logged and dropped, never rendered.
function normalizeEmailLinks(email) {
  if (email.links === undefined) return [];
  if (!Array.isArray(email.links)) {
    console.error("SendPidge: email push has a non-array links field", email.emailId);
    return [];
  }
  return email.links.filter((link) => {
    if (isValidEmailLink(link)) return true;
    console.error("SendPidge: dropping a malformed link entry", email.emailId);
    return false;
  });
}

// Jimin's call promise, or null when absent; an unknown value is logged and shown as no promise.
export function normalizeCallPromise(value) {
  if (value === null || value === undefined) return null;
  if (KNOWN_CALL_PROMISES.has(value)) return value;
  console.error("SendPidge: request has an unknown callPromise", value);
  return null;
}

// The channel the answer came on, or null (unanswered, absent on rows written before 2026-10-27, or unknown after logging).
export function normalizeRepliedVia(repliedVia) {
  if (repliedVia === null || repliedVia === undefined) return null;
  if (KNOWN_CHANNELS.has(repliedVia)) return repliedVia;
  console.error("SendPidge: request carries an unknown repliedVia", repliedVia);
  return null;
}

// The scam-list coverage, or null (a reading row, or a row written before SX3). An unknown value is logged and read as
// UNAVAILABLE, never as complete.
function normalizeScamListCoverage(scamListCoverage) {
  if (scamListCoverage === null || scamListCoverage === undefined) return null;
  if (KNOWN_SCAM_LIST_COVERAGES.has(scamListCoverage)) return scamListCoverage;
  console.error("SendPidge: email carries an unknown scamListCoverage", scamListCoverage);
  return SCAM_LIST_COVERAGE.UNAVAILABLE;
}

function normalizeEmailRequest(request) {
  if (request === null || request === undefined) return null;
  return { ...request, callPromise: normalizeCallPromise(request.callPromise), repliedVia: normalizeRepliedVia(request.repliedVia) };
}

// Returns the email with request, quarantined, links and scamListCoverage made explicit, or null (after logging) when the
// payload is invalid. A reading row (state null) never carries a body, links or coverage on this side either.
export function normalizeEmailPayloadOrReportError(email) {
  if (!isValidEmailPayload(email)) return null;
  const isReading = email.state === null;
  return {
    ...email, rowKind: ROW_KIND.EMAIL, request: normalizeEmailRequest(email.request), quarantined: email.quarantined === true,
    textBody: isReading ? "" : email.textBody, links: isReading ? [] : normalizeEmailLinks(email),
    scamListCoverage: isReading ? null : normalizeScamListCoverage(email.scamListCoverage),
    channel: CHANNEL.EMAIL, direction: MESSAGE_DIRECTION.IN, threadKey: normalizeThreadKey(email),
  };
}

// Returns the text as an inbox row, or null (after logging) when the payload is invalid. The row is keyed by messageId as its
// emailId (request_state names a held text by that id too) and has no subject, links or list coverage; senderName "" (not a
// checked number of a contact) shows the sender's number. A reading row never carries a body on this side either. An out row
// (Grandma's own send) goes to normalizeOutMessagePayloadOrReportError.
export function normalizeMessagePayloadOrReportError(message) {
  if (message?.direction === MESSAGE_DIRECTION.OUT) return normalizeOutMessagePayloadOrReportError(message);
  if (!isValidMessagePayload(message)) return null;
  return {
    emailId: message.messageId, rowKind: MESSAGE_ROW_KINDS[message.channel],
    senderName: message.senderName === "" ? message.senderAddress : message.senderName, senderAddress: message.senderAddress,
    subject: "", receivedAt: message.receivedAt, textBody: message.state === null ? "" : message.textBody, links: [], scamListCoverage: null,
    state: message.state, warnReason: message.warnReason, reasons: message.reasons,
    request: normalizeEmailRequest(message.request), quarantined: message.quarantined === true,
    channel: message.channel, direction: MESSAGE_DIRECTION.IN, threadKey: normalizeThreadKey(message),
  };
}

// An out row as an inbox row named by its recipient: a plain row (state pass, no request) whose time is sentAt, plus its
// providerStatus. Null (after logging) when the payload is invalid.
function normalizeOutMessagePayloadOrReportError(message) {
  if (!isValidOutMessagePayload(message)) return null;
  return {
    emailId: message.messageId, rowKind: OUT_MESSAGE_ROW_KINDS[message.channel],
    senderName: message.recipientName === "" ? message.recipientAddress : message.recipientName, senderAddress: message.recipientAddress,
    subject: "", receivedAt: message.sentAt, textBody: message.textBody, links: [], scamListCoverage: null,
    state: POLICY_DECISION.PASS, warnReason: null, reasons: [], request: null, quarantined: false,
    channel: message.channel, direction: MESSAGE_DIRECTION.OUT, threadKey: message.threadKey, providerStatus: message.providerStatus,
  };
}

// A reading row must never replace a row this PWA already holds with a verdict (a slow sync landing after the verdict push).
export function keepsDecidedRow(existingEmail, incomingEmail) {
  return existingEmail !== undefined && existingEmail.state !== null && incomingEmail.state === null;
}

export function isValidRequestStateUpdate(requestUpdate) {
  const isValid = requestUpdate !== null && typeof requestUpdate === "object"
    && typeof requestUpdate.requestId === "string" && typeof requestUpdate.emailId === "string"
    && KNOWN_REQUEST_STATUSES.has(requestUpdate.status) && typeof requestUpdate.quarantined === "boolean";
  if (!isValid) {
    console.error("SendPidge: request_state push has a missing field or unknown status", requestUpdate);
  }
  return isValid;
}
