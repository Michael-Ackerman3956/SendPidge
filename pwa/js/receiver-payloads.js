// Grandma's incoming data: the five static mock rows (email, SMS, app notice) and validation of every EMAIL / request_state payload.
// Mock rows carry rowKind; a live row is always ROW_KIND.EMAIL (other channels come in at slice 15).
import { CALL_PROMISE, CHANNEL, POLICY_DECISION, REQUEST_STATUS, ROW_KIND } from "./constants.js";

const MILLISECONDS_PER_HOUR = 3600000;
const KNOWN_POLICY_DECISIONS = new Set(Object.values(POLICY_DECISION));
const KNOWN_REQUEST_STATUSES = new Set(Object.values(REQUEST_STATUS));
const KNOWN_CALL_PROMISES = new Set(Object.values(CALL_PROMISE));
const KNOWN_CHANNELS = new Set(Object.values(CHANNEL));

export function buildMockFamilyEmails() {
  const now = Date.now();
  const receivedHoursAgo = (hours) => new Date(now - hours * MILLISECONDS_PER_HOUR).toISOString();
  const mockRow = (rowKind, emailId, senderName, senderAddress, subject, hoursAgo, textBody) => ({
    emailId, rowKind, senderName, senderAddress, subject, receivedAt: receivedHoursAgo(hoursAgo), textBody,
    state: POLICY_DECISION.PASS, warnReason: null, reasons: [], links: [], request: null, quarantined: false,
  });
  return [
    mockRow(ROW_KIND.EMAIL, "mock-sunday-lunch", "Minsoo", "minsoo@example.com", "Sunday lunch", 2,
      "Hi Mom, we'll come by around noon on Sunday. I'll bring the kimchi jjigae you like. See you then!"),
    mockRow(ROW_KIND.SMS, "mock-pharmacy", "Haneul Pharmacy", "+82 2-734-··12", "Prescription ready", 3,
      "Your prescription is ready for pickup until 7 pm today."),
    mockRow(ROW_KIND.EMAIL, "mock-garden-photos", "Young-hee", "younghee@example.com", "Your tomatoes!", 27,
      "The tomatoes from your garden were so sweet. Can you show me how you grow them next time?"),
    mockRow(ROW_KIND.APP, "mock-bank-statement", "Kakao Bank", "App notification", "October statement is ready", 51,
      "Your October statement is ready to view in the app."),
    mockRow(ROW_KIND.EMAIL, "mock-church-picnic", "Pastor Kim", "pastor.kim@example.com", "Picnic next Saturday", 75,
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

function isValidEmailPayload(email) {
  const hasRequiredFields = email !== null && typeof email === "object" && typeof email.emailId === "string"
    && typeof email.senderName === "string" && typeof email.subject === "string" && typeof email.textBody === "string";
  const isKnownState = email !== null && typeof email === "object" && (email.state === null || KNOWN_POLICY_DECISIONS.has(email.state));
  if (!hasRequiredFields || !isKnownState
    || !isValidEmailRequest(email.request) || !isValidQuarantinedField(email.quarantined)) {
    console.error("SendPidge: email push has a missing field, unknown state or unknown request status", email);
    return false;
  }
  if (email.state === POLICY_DECISION.HOLD && (email.request === null || email.request === undefined)) {
    console.error("SendPidge: HOLD push arrived without a request; showing the row flagged without the overlay", email.emailId);
  }
  return true;
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

function normalizeEmailRequest(request) {
  if (request === null || request === undefined) return null;
  return { ...request, callPromise: normalizeCallPromise(request.callPromise), repliedVia: normalizeRepliedVia(request.repliedVia) };
}

// Returns the email with request, quarantined and links made explicit, or null (after logging) when the payload is invalid.
export function normalizeEmailPayloadOrReportError(email) {
  if (!isValidEmailPayload(email)) return null;
  return { ...email, rowKind: ROW_KIND.EMAIL, request: normalizeEmailRequest(email.request), quarantined: email.quarantined === true, links: normalizeEmailLinks(email) };
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
