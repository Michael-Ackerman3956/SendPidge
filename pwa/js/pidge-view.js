// Grandma's Pidge tab (RECEIVER): the case agent's notes newest first, each with its recovery pack, and "I already paid".
// noteText and every pack field come from the server: rendered only through textContent / createElement.
import { CLIENT_ACTION, JURISDICTION, PAYMENT_RAIL, REQUEST_STATUS } from "./constants.js";
import { buildPidgeIconElement, createElementWithText, parseTimestampMilliseconds } from "./dom-helpers.js";
import { listReceiverRequests } from "./receiver-view.js";
import { sendClientAction } from "./websocket.js";
import { ACTION_STATUS_MESSAGE, clearActionStatusMessage, showActionStatusMessage } from "./action-status.js";

const REPORTABLE_STATUSES = new Set([REQUEST_STATUS.DENIED, REQUEST_STATUS.TIMEOUT]);
const PAYMENT_RAIL_LABELS = Object.freeze({
  [PAYMENT_RAIL.CARD]: "Card",
  [PAYMENT_RAIL.ZELLE]: "Zelle",
  [PAYMENT_RAIL.WIRE]: "Bank transfer",
  [PAYMENT_RAIL.GIFT_CARD]: "Gift card",
});
const DEFAULT_CURRENCY = "USD";

const notesById = new Map();
let isReportFormOpen = false;
let isReportInFlight = false;

const pageElements = {
  topbarTitle: document.getElementById("topbar-title"),
  pidgeScreen: document.getElementById("pidge-screen"),
  reportHost: document.getElementById("pidge-report-host"),
  noteList: document.getElementById("pidge-note-list"),
  emptyMessage: document.getElementById("pidge-empty-message"),
};

function isValidNote(note) {
  const pack = note?.recoveryPack;
  const isValid = note !== null && typeof note === "object" && typeof note.noteId === "string" && typeof note.noteText === "string"
    && parseTimestampMilliseconds(note.writtenAt) !== null
    && (pack === null || pack === undefined || (typeof pack === "object" && Array.isArray(pack.steps)));
  if (!isValid) console.error("SendPidge: case note push has a missing field or an unreadable pack", note);
  return isValid;
}

function formatNoteTime(writtenAt) {
  return new Date(parseTimestampMilliseconds(writtenAt)).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function describeDeadline(step) {
  if (step.deadlineHours) return `within ${step.deadlineHours} hours`;
  if (step.deadlineDays) return `within ${step.deadlineDays} days`;
  return "";
}

function buildPackElement(pack) {
  if (pack.ruleGap) return createElementWithText("p", "pidge-pack-gap", "Pidge has no steps for this country and payment yet.");
  const stepsList = createElementWithText("ol", "pidge-pack-steps");
  [...pack.steps].sort((first, second) => first.order - second.order).forEach((step) => {
    const stepItem = createElementWithText("li", "", `${step.action} `);
    stepItem.append(createElementWithText("span", "pidge-pack-contact", String(step.contact)));
    const deadlineText = describeDeadline(step);
    if (deadlineText) stepItem.append(" ", createElementWithText("span", "pidge-pack-deadline", deadlineText));
    stepsList.append(stepItem);
  });
  return stepsList;
}

function buildNoteElement(note) {
  const noteElement = createElementWithText("li", "pidge-note");
  const header = createElementWithText("div", "pidge-note-header");
  header.append(buildPidgeIconElement("pidge-note-pidge", ""), createElementWithText("span", "pidge-note-time", formatNoteTime(note.writtenAt)));
  noteElement.append(header, createElementWithText("p", "pidge-note-text", note.noteText));
  if (note.recoveryPack) noteElement.append(buildPackElement(note.recoveryPack));
  return noteElement;
}

function listReportableRequests() {
  return listReceiverRequests().filter((request) => REPORTABLE_STATUSES.has(request.status));
}

function buildSelect(name, options, selectedValue) {
  const select = document.createElement("select");
  select.name = name;
  options.forEach(([value, label]) => {
    const option = createElementWithText("option", "", label);
    option.value = value;
    option.selected = value === selectedValue;
    select.append(option);
  });
  return select;
}

function buildLabeledField(labelText, fieldElement) {
  const label = createElementWithText("label", "", labelText);
  label.append(fieldElement);
  return label;
}

function buildInput(name, type, placeholder, value = "") {
  const input = document.createElement("input");
  Object.assign(input, { name, type, placeholder, value, required: type !== "text" || name === "bank" });
  if (type === "number") { input.min = "0.01"; input.step = "0.01"; }
  return input;
}

function buildReportForm(reportableRequests) {
  const form = createElementWithText("form", "pidge-report-form");
  const today = new Date().toISOString().slice(0, 10);
  form.append(
    buildLabeledField("Which email", buildSelect("requestId", reportableRequests.map((request) => [request.requestId, `${request.contactName}: ${request.summary}`]), reportableRequests[0].requestId)),
    buildLabeledField("How you paid", buildSelect("paymentRail", Object.entries(PAYMENT_RAIL_LABELS), PAYMENT_RAIL.CARD)),
    buildLabeledField("Country", buildSelect("jurisdiction", Object.values(JURISDICTION).map((value) => [value, value]), JURISDICTION.US)),
    buildLabeledField("Bank or card", buildInput("bank", "text", "Chase")),
    buildLabeledField("Amount", buildInput("amount", "number", "500")),
    buildLabeledField("Paid on", buildInput("paidOn", "date", "", today)),
    buildLabeledField("Who received it (optional)", buildInput("payeeHint", "text", "last 4 digits or a name")),
  );
  const actions = createElementWithText("div", "pidge-report-actions");
  const sendButton = createElementWithText("button", "pidge-report-send", isReportInFlight ? "Sending…" : "Send to Pidge");
  sendButton.type = "submit";
  sendButton.disabled = isReportInFlight;
  const cancelButton = createElementWithText("button", "pidge-report-cancel", "Cancel");
  cancelButton.type = "button";
  cancelButton.addEventListener("click", () => { isReportFormOpen = false; renderReportArea(); });
  actions.append(sendButton, cancelButton);
  form.append(actions);
  form.addEventListener("submit", handleReportSubmitted);
  return form;
}

function handleReportSubmitted(submitEvent) {
  submitEvent.preventDefault();
  clearActionStatusMessage();
  const fields = new FormData(submitEvent.currentTarget);
  const report = {
    action: CLIENT_ACTION.REPORT_PAYMENT,
    requestId: fields.get("requestId"),
    jurisdiction: fields.get("jurisdiction"),
    paymentRail: fields.get("paymentRail"),
    bank: String(fields.get("bank")).trim(),
    amount: String(fields.get("amount")),
    currency: DEFAULT_CURRENCY,
    paidAt: `${fields.get("paidOn")}T12:00:00Z`,
    payeeHint: String(fields.get("payeeHint") ?? "").trim(),
  };
  if (!sendClientAction(report)) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  isReportInFlight = true;
  renderReportArea();
}

function renderReportArea() {
  const reportableRequests = listReportableRequests();
  if (reportableRequests.length === 0) {
    pageElements.reportHost.replaceChildren();
    return;
  }
  if (isReportFormOpen) {
    pageElements.reportHost.replaceChildren(buildReportForm(reportableRequests));
    return;
  }
  const openButton = createElementWithText("button", "pidge-report-button", "I already paid");
  openButton.type = "button";
  openButton.addEventListener("click", () => { isReportFormOpen = true; renderReportArea(); });
  pageElements.reportHost.replaceChildren(openButton);
}

function renderNotes() {
  const noteElements = [...notesById.values()]
    .sort((first, second) => parseTimestampMilliseconds(second.writtenAt) - parseTimestampMilliseconds(first.writtenAt))
    .map(buildNoteElement);
  pageElements.noteList.replaceChildren(...noteElements);
  pageElements.emptyMessage.hidden = noteElements.length > 0;
}

export function applyPidgeSyncState(notes) {
  if (!Array.isArray(notes)) {
    console.error("SendPidge: receiver sync_state has no notes array", notes);
    return;
  }
  notesById.clear();
  notes.filter(isValidNote).forEach((note) => notesById.set(note.noteId, note));
  renderNotes();
  renderReportArea();
}

export function handleCaseNotePush(note) {
  if (!isValidNote(note)) return;
  notesById.set(note.noteId, note);
  if (note.recoveryPack) {
    isReportInFlight = false;
    isReportFormOpen = false;
  }
  renderNotes();
  renderReportArea();
}

// A refused report re-enables the form so Grandma is never stuck on "Sending…".
export function handlePidgeActionRejected() {
  if (!isReportInFlight) return;
  isReportInFlight = false;
  renderReportArea();
}

export function showPidgeView() {
  pageElements.topbarTitle.textContent = "Pidge";
  pageElements.pidgeScreen.hidden = false;
  renderReportArea();
}

export function hidePidgeView() {
  pageElements.pidgeScreen.hidden = true;
}
