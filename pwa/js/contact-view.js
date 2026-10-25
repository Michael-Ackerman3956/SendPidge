// Jimin's screen (CONTACT): one "Was this you?" card per request, newest first. Yes / No go over the socket; a double_tap
// card arms Yes on the first tap and sends on the second; a Yes can be undone while it waits to release; a No can be taken
// back for five minutes.
// summary, senderAddress, subject and receiverName are attacker-influenced: rendered only through textContent / createElement.
import { CLIENT_ACTION, FRICTION, REQUEST_STATUS, VERDICT_ANSWER } from "./constants.js";
import { buildPidgeIconElement, buildSvgIcon, buildTickIcon, createElementWithText, formatRemainingTimeAsMinutesSeconds, parseTimestampMilliseconds } from "./dom-helpers.js";
import { sendClientAction } from "./websocket.js";
import { ACTION_STATUS_MESSAGE, clearActionStatusMessage, showActionStatusMessage } from "./action-status.js";

const COUNTDOWN_TICK_INTERVAL_MS = 1000;
const REVERSE_WINDOW_MS = 5 * 60 * 1000; // mirrors verdict.REVERSE_WINDOW_SECONDS
const KNOWN_REQUEST_STATUSES = new Set(Object.values(REQUEST_STATUS));
const KNOWN_FRICTIONS = new Set(Object.values(FRICTION));
const CLIENT_ACTION_FOR_ANSWER = Object.freeze({
  [VERDICT_ANSWER.UNDO]: CLIENT_ACTION.UNDO,
  [VERDICT_ANSWER.REVERSE]: CLIENT_ACTION.REVERSE,
});

const CONTACT_CARD_STATE = Object.freeze({
  ASKING: "asking",
  LATE: "late",
  SAID_NO: "said_no",
  SAID_YES_PENDING: "said_yes_pending", // waiting to release; Undo available
  SAID_YES: "said_yes",
  REVERSED: "reversed",
});

const requestsById = new Map();
const answersInFlightByRequestId = new Map();
const armedRequestIds = new Set(); // double_tap cards whose first Yes was tapped
let countdownIntervalId = null;

const pageElements = {
  topbarTitle: document.getElementById("topbar-title"),
  contactScreen: document.getElementById("contact-screen"),
  contactCardList: document.getElementById("contact-card-list"),
  contactEmptyMessage: document.getElementById("contact-empty-message"),
};

function isValidContactRequest(request) {
  const isValid = request !== null && typeof request === "object" && typeof request.requestId === "string"
    && typeof request.summary === "string" && typeof request.receiverName === "string"
    && typeof request.senderAddress === "string" && typeof request.subject === "string" && KNOWN_FRICTIONS.has(request.friction)
    && parseTimestampMilliseconds(request.expiresAt) !== null && KNOWN_REQUEST_STATUSES.has(request.status);
  if (!isValid) {
    console.error("SendPidge: verification request has a missing field, unreadable expiresAt or unknown status", request);
  }
  return isValid;
}

function hasTimePassed(isoText) {
  const milliseconds = parseTimestampMilliseconds(isoText);
  return milliseconds === null || milliseconds <= Date.now();
}

function isWithinReverseWindow(request) {
  const resolvedAtMilliseconds = parseTimestampMilliseconds(request.resolvedAt);
  return resolvedAtMilliseconds !== null && resolvedAtMilliseconds + REVERSE_WINDOW_MS > Date.now();
}

// A server TIMEOUT is shown like a locally expired PENDING card: Yes hidden, No still available.
function resolveContactCardState(request) {
  switch (request.status) {
    case REQUEST_STATUS.DENIED: return CONTACT_CARD_STATE.SAID_NO;
    case REQUEST_STATUS.CONFIRMED: return CONTACT_CARD_STATE.SAID_YES;
    case REQUEST_STATUS.CONFIRMED_PENDING: return hasTimePassed(request.releaseAt) ? CONTACT_CARD_STATE.SAID_YES : CONTACT_CARD_STATE.SAID_YES_PENDING;
    case REQUEST_STATUS.REVERSED_NO: return CONTACT_CARD_STATE.REVERSED;
    case REQUEST_STATUS.TIMEOUT: return CONTACT_CARD_STATE.LATE;
    default: return hasTimePassed(request.expiresAt) ? CONTACT_CARD_STATE.LATE : CONTACT_CARD_STATE.ASKING;
  }
}

function buildCardButton(request, answer, labelText, buttonClass, onClick) {
  const answerInFlight = answersInFlightByRequestId.get(request.requestId);
  const buttonElement = createElementWithText("button", buttonClass, answerInFlight === answer ? "Sending…" : labelText);
  buttonElement.type = "button";
  buttonElement.dataset.answer = answer;
  buttonElement.disabled = answerInFlight !== undefined;
  buttonElement.addEventListener("click", onClick);
  return buttonElement;
}

function buildAnswerButton(request, answer, labelText) {
  const isArmed = answer === VERDICT_ANSWER.YES && armedRequestIds.has(request.requestId);
  const buttonClass = answer === VERDICT_ANSWER.YES ? `contact-answer-button is-yes${isArmed ? " is-armed" : ""}` : "contact-answer-button is-no";
  const button = buildCardButton(request, answer, labelText, buttonClass, () => handleContactTappedAnswer(request.requestId, answer));
  button.prepend(answer === VERDICT_ANSWER.YES ? buildTickIcon("tick-icon") : buildCrossIcon());
  return button;
}

function buildCrossIcon() {
  return buildSvgIcon("cross-icon", "M6 6l12 12M18 6 6 18");
}

function buildCountdownElement(targetIsoText, className) {
  const countdownElement = createElementWithText("p", className);
  countdownElement.setAttribute("role", "timer");
  countdownElement.dataset.targetMilliseconds = String(parseTimestampMilliseconds(targetIsoText));
  countdownElement.textContent = formatRemainingTimeAsMinutesSeconds(Number(countdownElement.dataset.targetMilliseconds) - Date.now());
  return countdownElement;
}

// Title, "From <address>", the subject in quotes, then the one-sentence summary. The address is the fact Jimin judges by.
function buildCardHeaderChildren(request) {
  const metaElement = createElementWithText("p", "contact-card-meta");
  metaElement.append(createElementWithText("span", "", "From "), createElementWithText("span", "contact-card-address", request.senderAddress),
    document.createElement("br"), createElementWithText("span", "contact-card-subject", `“${request.subject}”`));
  return [buildPidgeIconElement("contact-card-pidge", ""), createElementWithText("h2", "contact-card-title", "Was this you?"),
    metaElement, createElementWithText("p", "contact-card-summary", request.summary)];
}

function buildQuestionCardChildren(request, contactCardState) {
  const children = buildCardHeaderChildren(request);
  const answerRow = createElementWithText("div", "contact-answer-row");
  if (contactCardState === CONTACT_CARD_STATE.ASKING) {
    const countdownElement = buildCountdownElement(request.expiresAt, "contact-countdown");
    countdownElement.append(createElementWithText("span", "", " left"));
    children.push(countdownElement);
    const isArmed = armedRequestIds.has(request.requestId);
    answerRow.append(buildAnswerButton(request, VERDICT_ANSWER.YES, isArmed ? "Tap again: yes, it was me" : "Yes, it was me"));
  } else {
    children.push(createElementWithText("p", "contact-card-late", "Time's up, but you can still say no."));
  }
  answerRow.append(buildAnswerButton(request, VERDICT_ANSWER.NO, "No, not me"));
  children.push(answerRow);
  return children;
}

function buildAnsweredCardChildren(request, contactCardState) {
  const children = [...buildCardHeaderChildren(request)];
  if (contactCardState === CONTACT_CARD_STATE.SAID_YES_PENDING) {
    const resultLine = createElementWithText("p", "contact-card-result is-yes", "You said yes. Opening in ");
    resultLine.append(buildCountdownElement(request.releaseAt, "contact-release-countdown"));
    children.push(resultLine, buildCardButton(request, VERDICT_ANSWER.UNDO, "Undo", "contact-secondary-button",
      () => handleContactTappedAnswer(request.requestId, VERDICT_ANSWER.UNDO)));
  } else if (contactCardState === CONTACT_CARD_STATE.SAID_YES) {
    const resultLine = createElementWithText("p", "contact-card-result is-yes");
    resultLine.append(buildTickIcon("tick-icon"), createElementWithText("span", "", "You said yes. Thank you."));
    children.push(resultLine);
  } else if (contactCardState === CONTACT_CARD_STATE.REVERSED) {
    const resultLine = createElementWithText("p", "contact-card-result is-yes");
    resultLine.append(buildTickIcon("tick-icon"), createElementWithText("span", "", "You changed your answer. The email was opened."));
    children.push(resultLine);
  } else {
    const resultLine = createElementWithText("p", "contact-card-result is-no");
    resultLine.append(buildCrossIcon(), createElementWithText("span", "", `You said no. ${request.receiverName}'s email stays held.`));
    children.push(resultLine);
    if (isWithinReverseWindow(request)) {
      children.push(buildCardButton(request, VERDICT_ANSWER.REVERSE, "I made a mistake", "contact-secondary-button",
        () => handleContactTappedAnswer(request.requestId, VERDICT_ANSWER.REVERSE)));
    }
  }
  return children;
}

function buildContactCardElement(request) {
  const contactCardState = resolveContactCardState(request);
  const cardElement = createElementWithText("li", contactCardState === CONTACT_CARD_STATE.ASKING ? "contact-card is-open" : "contact-card");
  cardElement.dataset.requestId = request.requestId;
  const isAnswered = contactCardState !== CONTACT_CARD_STATE.ASKING && contactCardState !== CONTACT_CARD_STATE.LATE;
  cardElement.append(...(isAnswered ? buildAnsweredCardChildren(request, contactCardState) : buildQuestionCardChildren(request, contactCardState)));
  return cardElement;
}

function sortRequestsNewestFirst(requests) {
  // No createdAt on the wire; every request gets the same timeout, so a later expiresAt means a newer request.
  return [...requests].sort((first, second) => parseTimestampMilliseconds(second.expiresAt) - parseTimestampMilliseconds(first.expiresAt));
}

// Rebuilds the cards and puts focus back on the same button if it still exists.
function renderContactCards() {
  const focusedElement = document.activeElement;
  const focusedRequestId = focusedElement?.closest?.(".contact-card")?.dataset.requestId;
  const focusedAnswer = focusedElement?.dataset?.answer;
  const cardElements = sortRequestsNewestFirst(requestsById.values()).map(buildContactCardElement);
  pageElements.contactCardList.replaceChildren(...cardElements);
  pageElements.contactEmptyMessage.hidden = cardElements.length > 0;
  if (focusedRequestId === undefined || focusedAnswer === undefined) return;
  const matchingCard = cardElements.find((cardElement) => cardElement.dataset.requestId === focusedRequestId);
  const matchingButton = matchingCard === undefined ? undefined
    : [...matchingCard.querySelectorAll("button")].find((buttonElement) => buttonElement.dataset.answer === focusedAnswer);
  if (matchingButton !== undefined && !matchingButton.disabled) matchingButton.focus();
}

// Updates countdown text in place; rebuilds the cards only when a hold, a release or a reverse window crosses its edge.
function tickContactCountdowns() {
  let hasCountdownEnded = false;
  pageElements.contactCardList.querySelectorAll("[role='timer']").forEach((countdownElement) => {
    const remainingMilliseconds = Number(countdownElement.dataset.targetMilliseconds) - Date.now();
    if (remainingMilliseconds <= 0) {
      hasCountdownEnded = true;
    } else {
      countdownElement.textContent = formatRemainingTimeAsMinutesSeconds(remainingMilliseconds);
    }
  });
  const hasReverseWindowClosed = [...requestsById.values()].some((request) => request.status === REQUEST_STATUS.DENIED
    && !isWithinReverseWindow(request) && pageElements.contactCardList.querySelector(`[data-request-id="${request.requestId}"] [data-answer="${VERDICT_ANSWER.REVERSE}"]`));
  if (hasCountdownEnded || hasReverseWindowClosed) renderContactCards();
}

function handleContactTappedAnswer(requestId, answer) {
  clearActionStatusMessage();
  const request = requestsById.get(requestId);
  const needsSecondTap = answer === VERDICT_ANSWER.YES && request.friction === FRICTION.DOUBLE_TAP && !armedRequestIds.has(requestId);
  if (needsSecondTap) {
    armedRequestIds.add(requestId);
    renderContactCards();
    return;
  }
  const clientAction = CLIENT_ACTION_FOR_ANSWER[answer] === undefined
    ? { action: CLIENT_ACTION.ANSWER, requestId, answer }
    : { action: CLIENT_ACTION_FOR_ANSWER[answer], requestId };
  if (!sendClientAction(clientAction)) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  answersInFlightByRequestId.set(requestId, answer);
  renderContactCards();
}

export function applyContactSyncState(requests) {
  if (!Array.isArray(requests)) {
    console.error("SendPidge: contact sync_state has no requests array", requests);
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  requestsById.clear();
  requests.filter(isValidContactRequest).forEach((request) => requestsById.set(request.requestId, request));
  renderContactCards();
}

export function handleVerificationCardPush(request) {
  if (!isValidContactRequest(request)) return;
  requestsById.set(request.requestId, request);
  renderContactCards();
}

export function handleContactRequestStatePush(requestUpdate) {
  const existingRequest = requestUpdate === null || typeof requestUpdate !== "object" ? undefined : requestsById.get(requestUpdate.requestId);
  if (existingRequest === undefined || !KNOWN_REQUEST_STATUSES.has(requestUpdate.status)) {
    console.warn("SendPidge: request_state for an unknown card or with an unknown status", requestUpdate);
    return;
  }
  requestsById.set(existingRequest.requestId, { ...existingRequest, status: requestUpdate.status,
    resolvedAt: requestUpdate.resolvedAt ?? null, releaseAt: requestUpdate.releaseAt ?? null });
  answersInFlightByRequestId.delete(existingRequest.requestId);
  armedRequestIds.delete(existingRequest.requestId);
  renderContactCards();
}

// A refused answer re-enables the buttons for that card.
export function handleContactActionRejected(requestId) {
  if (requestId !== null && answersInFlightByRequestId.delete(requestId)) renderContactCards();
}

export function showContactView() {
  pageElements.topbarTitle.textContent = "Pidge";
  pageElements.contactScreen.hidden = false;
  if (countdownIntervalId === null) {
    countdownIntervalId = setInterval(tickContactCountdowns, COUNTDOWN_TICK_INTERVAL_MS);
  }
}

export function hideContactView() {
  pageElements.contactScreen.hidden = true;
  clearInterval(countdownIntervalId);
  countdownIntervalId = null;
}
