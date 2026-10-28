// Grandma's screens (RECEIVER): inbox, email or text detail, hold overlay and verdict states, driven by sync_state, email_state,
// message_state and request_state. A text is held in the same row shape as an email (receiver-payloads.js), keyed by emailId.
// Email and text fields and contactName are attacker-controlled: rendered only through textContent / createElement.
import { CLIENT_ACTION, EMAIL_DISPLAY_STATE, REQUEST_STATUS, ROW_KIND } from "./constants.js";
import { createElementWithText, parseTimestampMilliseconds } from "./dom-helpers.js";
import { closeHoldOverlay, showHoldOverlay } from "./pidge-overlay.js";
import {
  buildCelebrationElement, buildDetailBannerElement, buildRowMarkElement, buildRowStatusChipElement, deriveEmailDisplayState,
  hasReleaseTimePassed, showDeniedVerdictOverlay,
} from "./verdict-view.js";
import {
  buildMockFamilyEmails, isValidRequestStateUpdate, keepsDecidedRow, normalizeCallPromise, normalizeEmailPayloadOrReportError,
  normalizeMessagePayloadOrReportError, normalizeRepliedVia,
} from "./receiver-payloads.js";
import { sendClientAction } from "./websocket.js";
import { ACTION_STATUS_MESSAGE, clearActionStatusMessage, showActionStatusMessage } from "./action-status.js";
import { buildLinkReasonLineElements, buildLinkStripElement } from "./link-strip.js";
import { buildScamListCoverageLineElement, buildSenderReasonLineElements } from "./reason-lines.js";
import { isSplitReadingPane, renderPaneTools, renderReaderBar } from "./reading-pane.js";

const ROW_KIND_LABELS = Object.freeze({
  [ROW_KIND.EMAIL]: "Email",
  [ROW_KIND.SMS]: "SMS",
  [ROW_KIND.WHATSAPP]: "WhatsApp",
  [ROW_KIND.RCS]: "RCS",
});

const MILLISECONDS_PER_MINUTE = 60000;
const MILLISECONDS_PER_HOUR = 3600000;
const MILLISECONDS_PER_DAY = 86400000;

const emailsById = new Map();
const mockEmailIds = new Set();
const releasedByUserEmailIds = new Set();
const reportingRequestIds = new Set();
let openEmailId = null;
let isReceiverViewInitialized = false;
let releaseTimerId = null;
let onUnseenHoldChanged = () => {};

const pageElements = {
  backButton: document.getElementById("back-button"),
  topbarTitle: document.getElementById("topbar-title"),
  inboxScreen: document.getElementById("inbox-screen"),
  inboxList: document.getElementById("inbox-list"),
  detailScreen: document.getElementById("detail-screen"),
  detailContent: document.getElementById("detail-content"),
};

function formatRelativeReceivedTime(receivedAtIsoText) {
  const receivedAtMilliseconds = parseTimestampMilliseconds(receivedAtIsoText);
  if (receivedAtMilliseconds === null) {
    console.error("SendPidge: email has an unreadable receivedAt", receivedAtIsoText);
    return "";
  }
  const elapsedMilliseconds = Date.now() - receivedAtMilliseconds;
  if (elapsedMilliseconds < MILLISECONDS_PER_MINUTE) return "now";
  if (elapsedMilliseconds < MILLISECONDS_PER_HOUR) return `${Math.floor(elapsedMilliseconds / MILLISECONDS_PER_MINUTE)}m`;
  if (elapsedMilliseconds < MILLISECONDS_PER_DAY) return `${Math.floor(elapsedMilliseconds / MILLISECONDS_PER_HOUR)}h`;
  if (elapsedMilliseconds < 7 * MILLISECONDS_PER_DAY) return new Date(receivedAtMilliseconds).toLocaleDateString(undefined, { weekday: "short" });
  return new Date(receivedAtMilliseconds).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// The round initial, tinted by channel, with the verdict mark on its corner when the email has one.
function buildAvatarElement(email, emailDisplayState) {
  const avatar = createElementWithText("span", `avatar-initial is-${email.rowKind}`);
  avatar.setAttribute("aria-hidden", "true");
  avatar.append(createElementWithText("span", "", (email.senderName.trim()[0] || "?").toUpperCase()));
  const markElement = buildRowMarkElement(emailDisplayState);
  if (markElement !== null) avatar.append(markElement);
  return avatar;
}

function getEmailDisplayState(email) {
  return deriveEmailDisplayState(email, releasedByUserEmailIds.has(email.emailId));
}

function buildInboxRowElement(email) {
  const listItem = document.createElement("li");
  const emailDisplayState = getEmailDisplayState(email);
  const rowButton = createElementWithText("button", email.emailId === openEmailId && isSplitReadingPane() ? "inbox-row is-selected" : "inbox-row");
  rowButton.type = "button";
  rowButton.dataset.emailId = email.emailId;
  const rowText = createElementWithText("span", "row-text");
  const rowTopLine = createElementWithText("span", "row-top-line");
  rowTopLine.append(createElementWithText("span", "row-sender", email.senderName),
    createElementWithText("span", "row-time", formatRelativeReceivedTime(email.receivedAt)));
  const rowBottomLine = createElementWithText("span", "row-bottom-line");
  rowBottomLine.append(createElementWithText("span", `channel-tag is-${email.rowKind}`, ROW_KIND_LABELS[email.rowKind]));
  const statusChip = buildRowStatusChipElement(email, emailDisplayState);
  if (statusChip !== null) rowBottomLine.append(statusChip);
  rowText.append(rowTopLine);
  if (email.subject !== "") rowText.append(createElementWithText("span", "row-subject", email.subject)); // a text has no subject
  rowText.append(createElementWithText("span", "row-preview", email.textBody), rowBottomLine);
  rowButton.append(buildAvatarElement(email, emailDisplayState), rowText);
  rowButton.addEventListener("click", () => openEmailDetail(email.emailId));
  listItem.append(rowButton);
  return listItem;
}

function sortEmailsNewestFirst(emails) {
  const sortKey = (email) => parseTimestampMilliseconds(email.receivedAt) ?? Number.NEGATIVE_INFINITY;
  return [...emails].sort((first, second) => sortKey(second) - sortKey(first));
}

function listSortedEmailIds() {
  return sortEmailsNewestFirst(emailsById.values()).map((email) => email.emailId);
}

function hasUnseenPendingHold() {
  return [...emailsById.values()].some((email) => email.emailId !== openEmailId && isNewestCandidatePendingHold(email));
}

function renderInbox() {
  const rowElements = sortEmailsNewestFirst(emailsById.values()).map(buildInboxRowElement);
  pageElements.inboxList.replaceChildren(...rowElements);
  renderPaneTools(emailsById.size);
  onUnseenHoldChanged(hasUnseenPendingHold());
}

function buildDetailHeaderElement(email) {
  const headerElement = createElementWithText("div", "detail-header");
  const senderBlock = createElementWithText("div", "detail-sender-block");
  senderBlock.append(createElementWithText("p", "detail-sender-name", email.senderName));
  if (email.senderAddress !== email.senderName) { // a text from an unknown number shows the number once
    senderBlock.append(createElementWithText("p", "detail-sender-address", email.senderAddress));
  }
  headerElement.append(buildAvatarElement(email, getEmailDisplayState(email)),
    senderBlock, createElementWithText("span", "row-time", formatRelativeReceivedTime(email.receivedAt)));
  return headerElement;
}

// One reason line under the banner: the first sender reason, else the first link reason. The rest stay in the link strip.
function buildFirstReasonLineElements(email) {
  const reasonLines = [...buildSenderReasonLineElements(email.reasons), ...buildLinkReasonLineElements(email.links)];
  return reasonLines.slice(0, 1);
}

function showCelebration(email) {
  const existingCelebration = pageElements.detailScreen.querySelector(".celebration");
  if (existingCelebration !== null) existingCelebration.remove();
  pageElements.detailScreen.append(buildCelebrationElement(email.request.contactName, () => {
    pageElements.detailContent.querySelector(".detail-subject, .detail-body")?.focus();
  }));
}

// A releasing email re-renders itself as confirmed (with the celebration) when its releaseAt passes.
function scheduleReleaseRender(email) {
  clearTimeout(releaseTimerId);
  if (getEmailDisplayState(email) !== EMAIL_DISPLAY_STATE.RELEASING) return;
  const delayMilliseconds = Math.max(0, parseTimestampMilliseconds(email.request.releaseAt) - Date.now());
  releaseTimerId = setTimeout(() => {
    const currentEmail = emailsById.get(email.emailId);
    if (currentEmail === undefined || !hasReleaseTimePassed(currentEmail.request)) return;
    renderInbox();
    if (openEmailId === currentEmail.emailId) renderEmailDetail(currentEmail, true);
  }, delayMilliseconds);
}

// shouldCelebrate is true only on the live transition to CONFIRMED, never on a sync rebuild.
function renderEmailDetail(email, shouldCelebrate = false) {
  closeHoldOverlay();
  scheduleReleaseRender(email);
  const emailDisplayState = getEmailDisplayState(email);
  const detailChildren = [buildDetailHeaderElement(email)];
  const bannerElement = buildDetailBannerElement(email, emailDisplayState, shouldCelebrate);
  if (bannerElement !== null) {
    detailChildren.push(bannerElement, ...buildFirstReasonLineElements(email));
  }
  const scamListCoverageLine = buildScamListCoverageLineElement(email); // shown on a PASS too, which has no banner
  if (scamListCoverageLine !== null) detailChildren.push(scamListCoverageLine);
  const subjectHeading = email.subject === "" ? null : createElementWithText("h2", "detail-subject", email.subject);
  const bodyElement = createElementWithText("p", emailDisplayState === EMAIL_DISPLAY_STATE.QUARANTINED ? "detail-body is-muted" : "detail-body", email.textBody);
  const focusTargetElement = subjectHeading === null ? bodyElement : subjectHeading; // a text has no subject: its body takes focus
  focusTargetElement.tabIndex = -1;
  if (subjectHeading !== null) detailChildren.push(subjectHeading);
  detailChildren.push(bodyElement);
  const linkStrip = buildLinkStripElement(email.links);
  if (linkStrip !== null) detailChildren.push(linkStrip);
  pageElements.detailContent.replaceChildren(...detailChildren);
  renderReaderBar({ emailIds: listSortedEmailIds(), openEmailId: email.emailId, onBack: showInboxScreen, onOpen: openEmailDetail });
  if (shouldCelebrate) showCelebration(email);
  if (emailDisplayState === EMAIL_DISPLAY_STATE.AWAITING_VERDICT) {
    showHoldOverlay({ overlayHostElement: pageElements.detailScreen, email, onReleasedByUser: handleUserReleasedHeldEmail });
  } else if (emailDisplayState === EMAIL_DISPLAY_STATE.DENIED) {
    showDeniedVerdictOverlay({ overlayHostElement: pageElements.detailScreen, email,
      isReporting: reportingRequestIds.has(email.request.requestId), onConfirmTapped: handleUserTappedConfirmReport });
  } else {
    focusTargetElement.focus();
  }
}

// In a split reading pane the list stays beside the email; otherwise the email takes the screen (phone, or Gmail's "No split").
function openEmailDetail(emailId) {
  openEmailId = emailId;
  const isSplit = isSplitReadingPane();
  pageElements.inboxScreen.hidden = !isSplit;
  pageElements.detailScreen.hidden = false;
  pageElements.backButton.hidden = isSplit;
  pageElements.topbarTitle.textContent = isSplit ? "Inbox" : ROW_KIND_LABELS[emailsById.get(emailId).rowKind];
  document.body.classList.toggle("is-reading-email", !isSplit); // the phone's tab bar steps aside for the email and the Pidge sheet
  renderEmailDetail(emailsById.get(emailId));
  renderInbox();
}

function showInboxScreen() {
  const previouslyOpenEmailId = openEmailId;
  closeHoldOverlay();
  openEmailId = null;
  pageElements.detailScreen.hidden = true;
  pageElements.backButton.hidden = true;
  pageElements.inboxScreen.hidden = false;
  pageElements.topbarTitle.textContent = "Inbox";
  document.body.classList.remove("is-reading-email");
  renderInbox();
  const previouslyOpenRow = [...pageElements.inboxList.querySelectorAll(".inbox-row")]
    .find((rowButton) => rowButton.dataset.emailId === previouslyOpenEmailId);
  if (previouslyOpenRow) previouslyOpenRow.focus();
}

// Re-applies the current reading-pane choice: in a split the open email stays beside the list, otherwise it fills the screen.
export function applyReadingPaneLayout() {
  if (openEmailId === null) {
    pageElements.detailScreen.hidden = true;
    renderInbox(); // the tools row shows the new choice and closes the menu
    return;
  }
  openEmailDetail(openEmailId);
}

// app.js listens so the Inbox tab can show its dot while a hold waits on another screen.
export function setUnseenHoldListener(listener) {
  onUnseenHoldChanged = listener;
}

// Local only: the server is not told about a long-press release.
function handleUserReleasedHeldEmail(emailId) {
  releasedByUserEmailIds.add(emailId);
  renderInbox();
  if (openEmailId === emailId) {
    renderEmailDetail(emailsById.get(emailId));
  }
}

function handleUserTappedConfirmReport(email) {
  clearActionStatusMessage();
  const requestId = email.request.requestId;
  if (!sendClientAction({ action: CLIENT_ACTION.CONFIRM, requestId })) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  reportingRequestIds.add(requestId);
  if (openEmailId === email.emailId) renderEmailDetail(emailsById.get(email.emailId));
}

function isNewestCandidatePendingHold(email) {
  if (getEmailDisplayState(email) !== EMAIL_DISPLAY_STATE.AWAITING_VERDICT || email.request.status !== REQUEST_STATUS.PENDING) {
    return false;
  }
  const expiresAtMilliseconds = parseTimestampMilliseconds(email.request.expiresAt);
  return expiresAtMilliseconds !== null && expiresAtMilliseconds > Date.now();
}

function rerenderOpenEmailOrReturnToInbox() {
  if (openEmailId === null) return;
  if (emailsById.has(openEmailId)) {
    renderEmailDetail(emailsById.get(openEmailId));
  } else {
    showInboxScreen();
  }
}

// Replaces every live row with the snapshot's emails and texts (mock rows stay), then reopens the overlay only for the newest
// pending hold. A snapshot missing either array is not applied: the rows already shown stay, with the failure line.
export function applyReceiverSyncState(emails, messages) {
  if (!Array.isArray(emails) || !Array.isArray(messages)) {
    console.error("SendPidge: receiver sync_state has no emails or no messages array", emails, messages);
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  const decidedEmailsBeforeSync = new Map([...emailsById].filter(([emailId, email]) => !mockEmailIds.has(emailId) && email.state !== null));
  [...emailsById.keys()].filter((emailId) => !mockEmailIds.has(emailId)).forEach((emailId) => emailsById.delete(emailId));
  [...emails.map(normalizeEmailPayloadOrReportError), ...messages.map(normalizeMessagePayloadOrReportError)]
    .filter((email) => email !== null)
    .forEach((email) => { if (!keepsDecidedRow(decidedEmailsBeforeSync.get(email.emailId), email)) emailsById.set(email.emailId, email); });
  decidedEmailsBeforeSync.forEach((email, emailId) => { if (!emailsById.has(emailId)) emailsById.set(emailId, email); });
  renderInbox();
  const newestPendingHold = sortEmailsNewestFirst(emailsById.values()).find(isNewestCandidatePendingHold);
  if (newestPendingHold !== undefined) {
    openEmailDetail(newestPendingHold.emailId);
  } else {
    rerenderOpenEmailOrReturnToInbox();
  }
}

export function handleEmailStatePush(email) {
  applyInboxRowPush(normalizeEmailPayloadOrReportError(email));
}

export function handleMessageStatePush(message) {
  applyInboxRowPush(normalizeMessagePayloadOrReportError(message));
}

// One pushed row (email or text, null when invalid): a hold awaiting its verdict opens itself; the open row re-renders.
function applyInboxRowPush(normalizedEmail) {
  if (normalizedEmail === null || keepsDecidedRow(emailsById.get(normalizedEmail.emailId), normalizedEmail)) return;
  releasedByUserEmailIds.delete(normalizedEmail.emailId); // a newer server state replaces the local release
  emailsById.set(normalizedEmail.emailId, normalizedEmail);
  renderInbox();
  if (getEmailDisplayState(normalizedEmail) === EMAIL_DISPLAY_STATE.AWAITING_VERDICT) {
    openEmailDetail(normalizedEmail.emailId);
  } else if (openEmailId === normalizedEmail.emailId) {
    renderEmailDetail(normalizedEmail);
  }
}

export function handleReceiverRequestStatePush(requestUpdate) {
  if (!isValidRequestStateUpdate(requestUpdate)) return;
  const email = emailsById.get(requestUpdate.emailId);
  if (email === undefined || email.request === null || email.request.requestId !== requestUpdate.requestId) {
    console.warn("SendPidge: request_state for an email this inbox does not hold", requestUpdate.requestId);
    return;
  }
  const wasConfirmed = getEmailDisplayState(email) === EMAIL_DISPLAY_STATE.CONFIRMED;
  const updatedEmail = { ...email, quarantined: requestUpdate.quarantined, request: {
    ...email.request, status: requestUpdate.status, resolvedAt: requestUpdate.resolvedAt ?? null, releaseAt: requestUpdate.releaseAt ?? null,
    callPromise: normalizeCallPromise(requestUpdate.callPromise), repliedVia: normalizeRepliedVia(requestUpdate.repliedVia),
  } };
  emailsById.set(updatedEmail.emailId, updatedEmail);
  if (requestUpdate.status !== REQUEST_STATUS.DENIED || requestUpdate.quarantined) {
    reportingRequestIds.delete(requestUpdate.requestId);
  }
  renderInbox();
  if (openEmailId === updatedEmail.emailId) {
    const updatedDisplayState = getEmailDisplayState(updatedEmail);
    const shouldCelebrate = !wasConfirmed
      && (updatedDisplayState === EMAIL_DISPLAY_STATE.CONFIRMED || updatedDisplayState === EMAIL_DISPLAY_STATE.REVERSED);
    renderEmailDetail(updatedEmail, shouldCelebrate);
  }
}

// A refused Confirm re-enables the button so Grandma is never stuck on "Reporting…".
export function handleReceiverActionRejected(requestId) {
  if (requestId === null || !reportingRequestIds.delete(requestId)) return;
  if (openEmailId !== null) renderEmailDetail(emailsById.get(openEmailId));
}

// Called on every RECEIVER sync_state; adds the mock rows and wires the back button only once.
export function showReceiverView() {
  if (!isReceiverViewInitialized) {
    isReceiverViewInitialized = true;
    buildMockFamilyEmails().forEach((email) => {
      mockEmailIds.add(email.emailId);
      emailsById.set(email.emailId, email);
    });
    pageElements.backButton.addEventListener("click", showInboxScreen);
  }
  if (openEmailId === null) {
    pageElements.inboxScreen.hidden = false;
    pageElements.topbarTitle.textContent = "Inbox";
  }
}

// Hides every receiver screen (used if a sync names the CONTACT role on this device).
export function hideReceiverView() {
  closeHoldOverlay();
  openEmailId = null;
  document.body.classList.remove("is-reading-email");
  pageElements.inboxScreen.hidden = true;
  pageElements.detailScreen.hidden = true;
  pageElements.backButton.hidden = true;
}

// The requests behind the live rows, newest first, for the Pidge tab's "I already paid" picker.
export function listReceiverRequests() {
  return sortEmailsNewestFirst(emailsById.values())
    .filter((email) => email.request !== null)
    .map((email) => ({ ...email.request, emailId: email.emailId }));
}
