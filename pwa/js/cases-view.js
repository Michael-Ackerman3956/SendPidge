// Grandma's Cases tab (RECEIVER): Open · Closed · Notes sub-tabs over the case cards, "I already paid" and the case agent's notes.
// noteText and every pack field come from the server: rendered only through textContent / createElement.
import { RECOVERY_DRAFT_KIND, CASES_TAB, CASE_STATUS, CLIENT_ACTION, JURISDICTION, PAYMENT_RAIL, REQUEST_STATUS } from "./constants.js";
import { buildCopyButton, buildPidgeIconElement, buildSvgIcon, createElementWithText, parseTimestampMilliseconds } from "./dom-helpers.js";
import { listReceiverRequests } from "./receiver-view.js";
import { sendClientAction } from "./websocket.js";
import { ACTION_STATUS_MESSAGE, clearActionStatusMessage, showActionStatusMessage } from "./action-status.js";

const REPORTABLE_STATUSES = new Set([REQUEST_STATUS.DENIED, REQUEST_STATUS.TIMEOUT]);
const PAYMENT_RAIL_LABELS = Object.freeze({
  [PAYMENT_RAIL.CARD]: "Card",
  [PAYMENT_RAIL.ZELLE]: "Zelle",
  [PAYMENT_RAIL.WIRE]: "Bank transfer",
  [PAYMENT_RAIL.GIFT_CARD]: "Gift card",
  [PAYMENT_RAIL.CRYPTO]: "Crypto",
});
const DEFAULT_CURRENCY = "USD";
const SUBTAB_LABELS = Object.freeze({ [CASES_TAB.OPEN]: "Open", [CASES_TAB.CLOSED]: "Closed", [CASES_TAB.NOTES]: "Notes" });

const PHONE_ICON_PATH = "M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z";
const ENVELOPE_ICON_PATHS = ["M4 5h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z", "m22 7-10 7L2 7"];
const DOCUMENT_ICON_PATHS = ["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z", "M14 2v6h6", "M8 13h8", "M8 17h8"];
const CHEVRON_DOWN_PATH = "m6 9 6 6 6-6";
const COPY_ICON_PATHS = ["M10 8h9a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2z", "M5 16H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v1"];

// Each draft's row: its label, its icon and the class that tints the icon tile (cases.css).
const DRAFT_ROWS = Object.freeze({
  [RECOVERY_DRAFT_KIND.CALL_SCRIPT]: { labelText: "Call script", iconPaths: [PHONE_ICON_PATH], tintClass: "is-call-script" },
  [RECOVERY_DRAFT_KIND.DISPUTE_LETTER]: { labelText: "Dispute letter", iconPaths: ENVELOPE_ICON_PATHS, tintClass: "is-dispute-letter" },
  [RECOVERY_DRAFT_KIND.POLICE_REPORT]: { labelText: "Police report", iconPaths: DOCUMENT_ICON_PATHS, tintClass: "is-police-report" },
});

const notesById = new Map();
const casesById = new Map();
const caseActionsInFlight = new Set(); // case ids with a Send or Done on the wire
let isReportFormOpen = false;
let isReportInFlight = false;
let activeCasesTab = CASES_TAB.OPEN;

const pageElements = {
  topbarTitle: document.getElementById("topbar-title"),
  casesScreen: document.getElementById("cases-screen"),
  subtabs: document.getElementById("cases-subtabs"),
  casesTabCount: document.getElementById("tab-cases-count"),
  reportHost: document.getElementById("pidge-report-host"),
  caseList: document.getElementById("pidge-case-list"),
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

// One collapsible row: tinted icon, label and the draft's first line; open, the server's text (textContent only) and Copy for her mail app.
function buildDraftElement(draftRow, draftText) {
  const draftElement = createElementWithText("details", `pidge-pack-draft ${draftRow.tintClass}`);
  const iconTile = createElementWithText("span", "pidge-pack-draft-icon");
  iconTile.append(buildSvgIcon("", draftRow.iconPaths));
  const titles = createElementWithText("span", "pidge-pack-draft-titles");
  titles.append(createElementWithText("span", "pidge-pack-draft-label", draftRow.labelText),
    createElementWithText("span", "pidge-pack-draft-preview", draftText.split("\n", 1)[0]));
  const summary = createElementWithText("summary", "pidge-pack-draft-summary");
  summary.append(iconTile, titles, buildSvgIcon("pidge-pack-draft-chevron", CHEVRON_DOWN_PATH));
  const copyButton = buildCopyButton(draftText);
  copyButton.prepend(buildSvgIcon("copy-button-icon", COPY_ICON_PATHS));
  const body = createElementWithText("div", "pidge-pack-draft-body");
  body.append(createElementWithText("pre", "pidge-pack-draft-text", draftText), copyButton);
  draftElement.append(summary, body);
  return draftElement;
}

// "Drafts for you to send" with the call script, dispute letter and police report, or null when the pack carries none.
function buildDraftsElement(pack, packMailedAt) {
  const drafts = pack.drafts;
  if (drafts === null || typeof drafts !== "object") return null;
  const draftsElement = createElementWithText("div", "pidge-pack-drafts");
  const draftGroup = createElementWithText("div", "pidge-pack-draft-group");
  Object.entries(DRAFT_ROWS).forEach(([draftKey, draftRow]) => {
    if (typeof drafts[draftKey] === "string") draftGroup.append(buildDraftElement(draftRow, drafts[draftKey]));
  });
  draftsElement.append(createElementWithText("p", "section-header", "Drafts for you to send"), draftGroup);
  const mailedText = packMailedAt ? "A copy was sent to your own inbox. Pidge sends nothing to anyone else." : "Pidge sends nothing to anyone else.";
  draftsElement.append(createElementWithText("p", "pidge-pack-mailed", mailedText));
  return draftsElement;
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
  if (note.recoveryPack) {
    noteElement.append(buildPackElement(note.recoveryPack));
    const draftsElement = buildDraftsElement(note.recoveryPack, note.packMailedAt);
    if (draftsElement !== null) noteElement.append(draftsElement);
  }
  return noteElement;
}

// A request with an open or reported case already has its card; the picker offers the others.
function listReportableRequests() {
  const requestIdsWithCases = new Set([...casesById.values()].filter((caseRow) => caseRow.status !== CASE_STATUS.CLOSED).map((caseRow) => caseRow.requestId));
  return listReceiverRequests().filter((request) => REPORTABLE_STATUSES.has(request.status) && !requestIdsWithCases.has(request.requestId));
}

function isValidCase(caseRow) {
  const isValid = caseRow !== null && typeof caseRow === "object" && typeof caseRow.caseId === "string"
    && Object.values(CASE_STATUS).includes(caseRow.status) && Array.isArray(caseRow.replies);
  if (!isValid) console.error("SendPidge: case push has a missing field or unknown status", caseRow);
  return isValid;
}

function buildCaseButton(caseRow, labelText, className, clientAction) {
  const isInFlight = caseActionsInFlight.has(caseRow.caseId);
  const button = createElementWithText("button", className, isInFlight ? "Sending…" : labelText);
  button.type = "button";
  button.disabled = isInFlight;
  button.addEventListener("click", () => {
    clearActionStatusMessage();
    if (!sendClientAction({ action: clientAction, caseId: caseRow.caseId })) {
      showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
      return;
    }
    caseActionsInFlight.add(caseRow.caseId);
    renderCases();
  });
  return button;
}

function describeCaseStatus(caseRow) {
  if (caseRow.status === CASE_STATUS.OPEN) return "Not sent yet.";
  if (caseRow.status === CASE_STATUS.CLOSED) return "Closed.";
  if (caseRow.replies.length > 0) return "Your bank replied.";
  return caseRow.followUpSentAt ? "Reminder sent. Waiting for your bank." : "Sent. Waiting for your bank.";
}

// Display only: the bank's new words, without the quoted Pidge report that mail clients append ("On … wrote: > …").
function stripQuotedHistory(replyText) {
  const quoteStart = replyText.search(/\s*On [^\n]{5,200}?wrote:/);
  const newText = (quoteStart >= 0 ? replyText.slice(0, quoteStart) : replyText).trim();
  return newText || replyText;
}

function buildReplyElement(reply) {
  const replyElement = createElementWithText("blockquote", "pidge-case-reply");
  const headerElement = document.createElement("div");
  headerElement.className = "pidge-case-reply-header";
  headerElement.append(createElementWithText("span", "pidge-case-reply-sender", reply.from),
    createElementWithText("span", "pidge-note-time", formatNoteTime(reply.receivedAt)));
  replyElement.append(headerElement, createElementWithText("p", "pidge-case-reply-text", stripQuotedHistory(reply.textContent)));
  return replyElement;
}

function buildCaseElement(caseRow) {
  const caseElement = createElementWithText("li", "pidge-case");
  const title = `${PAYMENT_RAIL_LABELS[caseRow.paymentRail] ?? caseRow.paymentRail} · ${caseRow.bank} · ${caseRow.amount} ${caseRow.currency}`;
  caseElement.append(createElementWithText("p", "pidge-case-title", title), createElementWithText("p", "pidge-case-status", describeCaseStatus(caseRow)));
  caseRow.replies.forEach((reply) => caseElement.append(buildReplyElement(reply)));
  if (caseRow.status === CASE_STATUS.OPEN) {
    caseElement.append(buildCaseButton(caseRow, "Send to my bank (simulated)", "pidge-report-button", CLIENT_ACTION.SEND_REPORT));
  } else if (caseRow.status === CASE_STATUS.REPORTED) {
    caseElement.append(buildCaseButton(caseRow, "Done", "pidge-case-done-button", CLIENT_ACTION.CLOSE_CASE));
  }
  return caseElement;
}

function isCaseOpen(caseRow) { return caseRow.status !== CASE_STATUS.CLOSED; }

// Open shows open and reported cases; Closed the rest; Notes shows none.
function listCasesForActiveTab() {
  if (activeCasesTab === CASES_TAB.NOTES) return [];
  return [...casesById.values()]
    .filter((caseRow) => isCaseOpen(caseRow) === (activeCasesTab === CASES_TAB.OPEN))
    .sort((first, second) => parseTimestampMilliseconds(second.createdAt) - parseTimestampMilliseconds(first.createdAt));
}

function renderCases() {
  pageElements.caseList.replaceChildren(...listCasesForActiveTab().map(buildCaseElement));
  renderSubtabs();
  renderEmptyMessage();
}

function renderEmptyMessage() {
  const isNotesTab = activeCasesTab === CASES_TAB.NOTES;
  pageElements.emptyMessage.hidden = isNotesTab ? notesById.size > 0 : listCasesForActiveTab().length > 0 || (activeCasesTab === CASES_TAB.OPEN && listReportableRequests().length > 0);
}

function buildSubtabButton(tab) {
  const button = createElementWithText("button", "subtab", SUBTAB_LABELS[tab]);
  button.type = "button";
  button.setAttribute("role", "tab");
  button.setAttribute("aria-selected", String(tab === activeCasesTab));
  if (tab === CASES_TAB.OPEN && openCaseCount() > 0) button.append(createElementWithText("i", "count-badge", String(openCaseCount())));
  button.addEventListener("click", () => { activeCasesTab = tab; renderCasesScreen(); });
  return button;
}

function openCaseCount() { return [...casesById.values()].filter(isCaseOpen).length; }

function renderSubtabs() {
  pageElements.subtabs.replaceChildren(...Object.values(CASES_TAB).map(buildSubtabButton));
  pageElements.casesTabCount.textContent = String(openCaseCount());
  pageElements.casesTabCount.hidden = openCaseCount() === 0;
}

// Everything on the Cases tab at once: the strip, the report button or form, the cards, the notes.
function renderCasesScreen() {
  renderCases();
  renderReportArea();
  renderNotes();
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
  if (reportableRequests.length === 0 || activeCasesTab !== CASES_TAB.OPEN) {
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
  const noteElements = activeCasesTab !== CASES_TAB.NOTES ? [] : [...notesById.values()]
    .sort((first, second) => parseTimestampMilliseconds(second.writtenAt) - parseTimestampMilliseconds(first.writtenAt))
    .map(buildNoteElement);
  pageElements.noteList.replaceChildren(...noteElements);
  renderEmptyMessage();
}

// The chat's "I already paid" chip lands here: the Open sub-tab with the report form open when there is a request to report
// against (otherwise the empty message shows and the form stays closed, so it cannot pop open later by itself).
export function openReportPaymentForm() {
  activeCasesTab = CASES_TAB.OPEN;
  isReportFormOpen = listReportableRequests().length > 0;
  renderCasesScreen();
}

export function applyPidgeSyncState(notes, cases) {
  if (!Array.isArray(notes) || !Array.isArray(cases)) {
    console.error("SendPidge: receiver sync_state has no notes or cases array", notes, cases);
    return;
  }
  notesById.clear();
  notes.filter(isValidNote).forEach((note) => notesById.set(note.noteId, note));
  casesById.clear();
  cases.filter(isValidCase).forEach((caseRow) => casesById.set(caseRow.caseId, caseRow));
  caseActionsInFlight.clear();
  renderCasesScreen();
}

export function handleCaseStatePush(caseRow) {
  if (!isValidCase(caseRow)) return;
  casesById.set(caseRow.caseId, caseRow);
  caseActionsInFlight.delete(caseRow.caseId);
  isReportInFlight = false;
  isReportFormOpen = false;
  renderCases();
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

// A refused report, Send or Done re-enables its button so Grandma is never stuck on "Sending…".
export function handlePidgeActionRejected() {
  if (caseActionsInFlight.size > 0) {
    caseActionsInFlight.clear();
    renderCases();
  }
  if (!isReportInFlight) return;
  isReportInFlight = false;
  renderReportArea();
}

export function showCasesView() {
  pageElements.topbarTitle.textContent = "Cases";
  pageElements.casesScreen.hidden = false;
  renderCasesScreen();
}

export function hideCasesView() {
  pageElements.casesScreen.hidden = true;
}
