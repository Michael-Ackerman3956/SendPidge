// An org admin's Messages tab: the safety alert composer (senders only) and the report of the newest alerts, each with every
// recipient's state (Sent, Acknowledged, Need help). Alert text and names come from the server: textContent only. A send
// has no reply on success: the org_changed re-sync lists the new alert, which clears the composer; a refusal shows why.
import { ACK_ANSWER, ACTION_REJECTED_REASON, ADMIN_ROLE, CLIENT_ACTION, DELIVERY_STATE } from "./constants.js";
import { createElementWithText, formatTimestampForDisplay, parseTimestampMilliseconds } from "./dom-helpers.js";
import { sendClientAction } from "./websocket.js";

// lambda/shared/org_broadcasts.py ALERT_TEXT_MAX_CHARACTERS: keep the two equal (the textarea's maxlength too).
const ALERT_TEXT_MAX_CHARACTERS = 280;
const KNOWN_DELIVERY_STATES = new Set(Object.values(DELIVERY_STATE));
const KNOWN_ACK_ANSWERS = new Set(Object.values(ACK_ANSWER));
const EMPTY_ALERT_TEXT = "Write the alert first.";
const SEND_REFUSAL_TEXTS = Object.freeze({
  [ACTION_REJECTED_REASON.NOT_ALLOWED]: "Sign in with your passkey to send.",
  [ACTION_REJECTED_REASON.TEXT_REFUSED]: "Remove links, addresses, long numbers and symbols.",
  [ACTION_REJECTED_REASON.OVER_CAP]: "Hourly limit reached. Try again later.",
  [ACTION_REJECTED_REASON.NOT_APPLIED]: "No members to send to.",
});
const GENERIC_SEND_REFUSAL_TEXT = "Not sent. Try again.";

const pageElements = {
  form: document.getElementById("org-broadcast-form"),
  textInput: document.getElementById("org-broadcast-text"),
  errorLine: document.getElementById("org-broadcast-error"),
  sendButton: document.getElementById("org-broadcast-send"),
  broadcastList: document.getElementById("org-broadcast-list"),
  emptyMessage: document.getElementById("org-broadcast-empty"),
};
// The newest alert listed when Send was tapped; null when no send is in flight.
let sendInFlight = null;
let newestShownBroadcastId = null;

function isValidRecipient(recipient) {
  return recipient !== null && typeof recipient === "object" && typeof recipient.memberRef === "string"
    && typeof recipient.residentName === "string" && KNOWN_DELIVERY_STATES.has(recipient.state)
    && (recipient.state === DELIVERY_STATE.ACKNOWLEDGED ? KNOWN_ACK_ANSWERS.has(recipient.answer) : recipient.answer === null);
}

// A malformed alert or recipient is left out and logged; org-view's status line reports it.
export function isValidBroadcast(broadcast) {
  const isValid = broadcast !== null && typeof broadcast === "object" && typeof broadcast.broadcastId === "string"
    && typeof broadcast.text === "string" && parseTimestampMilliseconds(broadcast.sentAt) !== null
    && Array.isArray(broadcast.recipients) && broadcast.recipients.every(isValidRecipient);
  if (!isValid) console.error("SendPidge: org broadcast has a missing field or unknown state", broadcast);
  return isValid;
}

// Called only with a validated recipient: the chip's label and the tone org.css colours it with.
function describeRecipientState(recipient) {
  if (recipient.state === DELIVERY_STATE.SENT) return { label: "Sent", toneClass: "is-neutral" };
  if (recipient.answer === ACK_ANSWER.NEED_HELP) return { label: "Need help", toneClass: "is-held" };
  return { label: "Acknowledged", toneClass: "is-good" };
}

function buildRecipientRowElement(recipient) {
  const recipientState = describeRecipientState(recipient);
  const row = createElementWithText("li", "org-log-row");
  row.append(
    createElementWithText("span", "org-log-who", recipient.residentName),
    createElementWithText("span", `org-chip org-broadcast-chip ${recipientState.toneClass}`, recipientState.label),
  );
  return row;
}

function buildBroadcastElement(broadcast) {
  const card = createElementWithText("li", "org-card org-broadcast");
  const recipientRows = createElementWithText("ul", "org-log");
  recipientRows.append(...broadcast.recipients.map(buildRecipientRowElement));
  const sentLine = `Sent ${formatTimestampForDisplay(broadcast.sentAt)} · ${broadcast.recipients.length} residents`;
  card.append(
    createElementWithText("p", "org-broadcast-message", broadcast.text),
    createElementWithText("p", "org-broadcast-sent", sentLine),
    recipientRows,
  );
  return card;
}

function showComposerError(errorText) {
  pageElements.errorLine.textContent = errorText;
  pageElements.errorLine.hidden = false;
}

function finishSend() {
  sendInFlight = null;
  pageElements.sendButton.disabled = false;
}

function handleUserSubmittedAlert(submitEvent) {
  submitEvent.preventDefault();
  if (sendInFlight !== null) return;
  const alertText = pageElements.textInput.value.trim();
  if (alertText.length === 0 || alertText.length > ALERT_TEXT_MAX_CHARACTERS) {
    showComposerError(EMPTY_ALERT_TEXT);
    return;
  }
  if (!sendClientAction({ action: CLIENT_ACTION.ORG_BROADCAST_SEND, text: alertText })) {
    showComposerError(GENERIC_SEND_REFUSAL_TEXT);
    return;
  }
  pageElements.errorLine.hidden = true;
  sendInFlight = { newestBroadcastIdBeforeSend: newestShownBroadcastId };
  pageElements.sendButton.disabled = true;
}

export function startOrgMessagesView() {
  pageElements.form.addEventListener("submit", handleUserSubmittedAlert);
}

// From the admin's sync_state: the composer for a sender only, then the report. A new newest alert ends a send in flight.
export function applyOrgBroadcastState(broadcasts, adminRole) {
  pageElements.form.hidden = adminRole !== ADMIN_ROLE.SENDER;
  newestShownBroadcastId = broadcasts.length > 0 ? broadcasts[0].broadcastId : null;
  if (sendInFlight !== null && newestShownBroadcastId !== sendInFlight.newestBroadcastIdBeforeSend) {
    pageElements.textInput.value = "";
    finishSend();
  }
  pageElements.broadcastList.replaceChildren(...broadcasts.map(buildBroadcastElement));
  pageElements.emptyMessage.hidden = broadcasts.length > 0;
}

// A refused action while an alert is in flight is the alert's: the composer says why and keeps the text. Returns whether it
// was handled here.
export function handleOrgBroadcastActionRejected(reason) {
  if (sendInFlight === null) return false;
  finishSend();
  showComposerError(Object.hasOwn(SEND_REFUSAL_TEXTS, reason) ? SEND_REFUSAL_TEXTS[reason] : GENERIC_SEND_REFUSAL_TEXT); // BAD_MESSAGE too
  return true;
}

// The admin's screens are torn down (another role's sync_state): nothing stays in flight.
export function resetOrgMessagesView() {
  finishSend();
  pageElements.errorLine.hidden = true;
}
