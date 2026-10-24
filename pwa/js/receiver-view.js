// Grandma's screens (RECEIVER): inbox, email detail, hold overlay and verdict states, driven by sync_state, email_state and request_state.
// Email fields and contactName are attacker-controlled: rendered only through textContent / createElement.
import { CLIENT_ACTION, EMAIL_DISPLAY_STATE, REQUEST_STATUS } from "./constants.js";
import { createElementWithText, parseTimestampMilliseconds } from "./dom-helpers.js";
import { closeHoldOverlay, showHoldOverlay } from "./pidge-overlay.js";
import { buildDetailBannerElement, buildRowStatusElement, deriveEmailDisplayState, hasReleaseTimePassed, showDeniedVerdictOverlay } from "./verdict-view.js";
import { buildMockFamilyEmails, isValidRequestStateUpdate, normalizeEmailPayloadOrReportError } from "./receiver-payloads.js";
import { sendClientAction } from "./websocket.js";
import { ACTION_STATUS_MESSAGE, clearActionStatusMessage, showActionStatusMessage } from "./action-status.js";
import { buildLinkReasonLineElements, buildLinkStripElement } from "./link-strip.js";

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

function buildAvatarInitialElement(senderName) {
  const avatar = createElementWithText("span", "avatar-initial", (senderName.trim()[0] || "?").toUpperCase());
  avatar.setAttribute("aria-hidden", "true");
  return avatar;
}

function getEmailDisplayState(email) {
  return deriveEmailDisplayState(email, releasedByUserEmailIds.has(email.emailId));
}

function buildInboxRowElement(email) {
  const listItem = document.createElement("li");
  const rowButton = createElementWithText("button", "inbox-row");
  rowButton.type = "button";
  rowButton.dataset.emailId = email.emailId;
  const rowText = createElementWithText("span", "row-text");
  const rowTopLine = createElementWithText("span", "row-top-line");
  rowTopLine.append(createElementWithText("span", "row-sender", email.senderName),
    createElementWithText("span", "row-time", formatRelativeReceivedTime(email.receivedAt)));
  const rowBottomLine = createElementWithText("span", "row-bottom-line");
  rowBottomLine.append(createElementWithText("span", "channel-tag", "Email"));
  const rowStatus = buildRowStatusElement(getEmailDisplayState(email));
  if (rowStatus !== null) rowBottomLine.append(rowStatus);
  rowText.append(rowTopLine, createElementWithText("span", "row-subject", email.subject),
    createElementWithText("span", "row-preview", email.textBody), rowBottomLine);
  rowButton.append(buildAvatarInitialElement(email.senderName), rowText);
  rowButton.addEventListener("click", () => openEmailDetail(email.emailId));
  listItem.append(rowButton);
  return listItem;
}

function sortEmailsNewestFirst(emails) {
  const sortKey = (email) => parseTimestampMilliseconds(email.receivedAt) ?? Number.NEGATIVE_INFINITY;
  return [...emails].sort((first, second) => sortKey(second) - sortKey(first));
}

function renderInbox() {
  const rowElements = sortEmailsNewestFirst(emailsById.values()).map(buildInboxRowElement);
  pageElements.inboxList.replaceChildren(...rowElements);
}

function buildDetailHeaderElement(email) {
  const headerElement = createElementWithText("div", "detail-header");
  const senderBlock = createElementWithText("div", "detail-sender-block");
  senderBlock.append(createElementWithText("p", "detail-sender-name", email.senderName),
    createElementWithText("p", "detail-sender-address", email.senderAddress));
  headerElement.append(buildAvatarInitialElement(email.senderName),
    senderBlock, createElementWithText("span", "row-time", formatRelativeReceivedTime(email.receivedAt)));
  return headerElement;
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
  if (bannerElement !== null) detailChildren.push(bannerElement, ...buildLinkReasonLineElements(email.links));
  const subjectHeading = createElementWithText("h2", "detail-subject", email.subject);
  subjectHeading.tabIndex = -1;
  detailChildren.push(subjectHeading, createElementWithText("p", "detail-body", email.textBody));
  const linkStrip = buildLinkStripElement(email.links);
  if (linkStrip !== null) detailChildren.push(linkStrip);
  pageElements.detailContent.replaceChildren(...detailChildren);
  if (emailDisplayState === EMAIL_DISPLAY_STATE.AWAITING_VERDICT) {
    showHoldOverlay({ overlayHostElement: pageElements.detailScreen, email, onReleasedByUser: handleUserReleasedHeldEmail });
  } else if (emailDisplayState === EMAIL_DISPLAY_STATE.DENIED) {
    showDeniedVerdictOverlay({ overlayHostElement: pageElements.detailScreen, email,
      isReporting: reportingRequestIds.has(email.request.requestId), onConfirmTapped: handleUserTappedConfirmReport });
  } else {
    subjectHeading.focus();
  }
}

function openEmailDetail(emailId) {
  openEmailId = emailId;
  pageElements.inboxScreen.hidden = true;
  pageElements.detailScreen.hidden = false;
  pageElements.backButton.hidden = false;
  pageElements.topbarTitle.textContent = "Email";
  renderEmailDetail(emailsById.get(emailId));
}

function showInboxScreen() {
  const previouslyOpenEmailId = openEmailId;
  closeHoldOverlay();
  openEmailId = null;
  pageElements.detailScreen.hidden = true;
  pageElements.backButton.hidden = true;
  pageElements.inboxScreen.hidden = false;
  pageElements.topbarTitle.textContent = "Inbox";
  const previouslyOpenRow = [...pageElements.inboxList.querySelectorAll(".inbox-row")]
    .find((rowButton) => rowButton.dataset.emailId === previouslyOpenEmailId);
  if (previouslyOpenRow) previouslyOpenRow.focus();
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

// Replaces every live row with the snapshot (mock rows stay), then reopens the overlay only for the newest pending hold.
export function applyReceiverSyncState(emails) {
  if (!Array.isArray(emails)) {
    console.error("SendPidge: receiver sync_state has no emails array", emails);
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  [...emailsById.keys()].filter((emailId) => !mockEmailIds.has(emailId)).forEach((emailId) => emailsById.delete(emailId));
  emails.map(normalizeEmailPayloadOrReportError).filter((email) => email !== null)
    .forEach((email) => emailsById.set(email.emailId, email));
  renderInbox();
  const newestPendingHold = sortEmailsNewestFirst(emailsById.values()).find(isNewestCandidatePendingHold);
  if (newestPendingHold !== undefined) {
    openEmailDetail(newestPendingHold.emailId);
  } else {
    rerenderOpenEmailOrReturnToInbox();
  }
}

export function handleEmailStatePush(email) {
  const normalizedEmail = normalizeEmailPayloadOrReportError(email);
  if (normalizedEmail === null) return;
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
