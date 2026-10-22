// Jimin's screen (CONTACT): one "Was this you?" card per request, newest first, with Yes / No answers sent over the socket.
// summary and receiverName are attacker-influenced: rendered only through textContent / createElement.
import { CLIENT_ACTION, REQUEST_STATUS, VERDICT_ANSWER } from "./constants.js";
import { buildPidgeIconElement, buildTickIcon, createElementWithText, formatRemainingTimeAsMinutesSeconds, parseTimestampMilliseconds } from "./dom-helpers.js";
import { sendClientAction } from "./websocket.js";
import { ACTION_STATUS_MESSAGE, clearActionStatusMessage, showActionStatusMessage } from "./action-status.js";

const COUNTDOWN_TICK_INTERVAL_MS = 1000;
const KNOWN_REQUEST_STATUSES = new Set(Object.values(REQUEST_STATUS));

const CONTACT_CARD_STATE = Object.freeze({
  ASKING: "asking",
  LATE: "late",
  SAID_NO: "said_no",
  SAID_YES: "said_yes",
});

const requestsById = new Map();
const answersInFlightByRequestId = new Map();
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
    && parseTimestampMilliseconds(request.expiresAt) !== null && KNOWN_REQUEST_STATUSES.has(request.status);
  if (!isValid) {
    console.error("SendPidge: verification request has a missing field, unreadable expiresAt or unknown status", request);
  }
  return isValid;
}

// A server TIMEOUT is shown like a locally expired PENDING card: Yes hidden, No still available.
function resolveContactCardState(request) {
  if (request.status === REQUEST_STATUS.DENIED) return CONTACT_CARD_STATE.SAID_NO;
  if (request.status === REQUEST_STATUS.CONFIRMED) return CONTACT_CARD_STATE.SAID_YES;
  const isExpired = parseTimestampMilliseconds(request.expiresAt) <= Date.now();
  return request.status === REQUEST_STATUS.TIMEOUT || isExpired ? CONTACT_CARD_STATE.LATE : CONTACT_CARD_STATE.ASKING;
}

function buildAnswerButton(request, answer, labelText) {
  const answerInFlight = answersInFlightByRequestId.get(request.requestId);
  const buttonClass = answer === VERDICT_ANSWER.YES ? "contact-answer-button is-yes" : "contact-answer-button is-no";
  const answerButton = createElementWithText("button", buttonClass, answerInFlight === answer ? "Sending…" : labelText);
  answerButton.type = "button";
  answerButton.dataset.answer = answer;
  answerButton.disabled = answerInFlight !== undefined;
  answerButton.addEventListener("click", () => handleContactTappedAnswer(request.requestId, answer));
  return answerButton;
}

function buildCountdownElement(request) {
  const countdownElement = createElementWithText("p", "contact-countdown");
  countdownElement.setAttribute("role", "timer");
  countdownElement.dataset.expiresAtMilliseconds = String(parseTimestampMilliseconds(request.expiresAt));
  countdownElement.textContent = formatRemainingTimeAsMinutesSeconds(Number(countdownElement.dataset.expiresAtMilliseconds) - Date.now());
  return countdownElement;
}

function buildQuestionCardChildren(request, contactCardState) {
  const children = [buildPidgeIconElement("contact-card-pidge", ""), createElementWithText("h2", "contact-card-title", "Was this you?"),
    createElementWithText("p", "contact-card-summary", request.summary),
    createElementWithText("p", "contact-card-receiver", `Email to ${request.receiverName}`)];
  const answerRow = createElementWithText("div", "contact-answer-row");
  if (contactCardState === CONTACT_CARD_STATE.ASKING) {
    children.push(buildCountdownElement(request));
    answerRow.append(buildAnswerButton(request, VERDICT_ANSWER.YES, "Yes, it was me"));
  } else {
    children.push(createElementWithText("p", "contact-card-late", "Time's up, but you can still say no."));
  }
  answerRow.append(buildAnswerButton(request, VERDICT_ANSWER.NO, "No, not me"));
  children.push(answerRow);
  return children;
}

function buildAnsweredCardChildren(request, contactCardState) {
  const children = [createElementWithText("p", "contact-card-summary", request.summary)];
  if (contactCardState === CONTACT_CARD_STATE.SAID_YES) {
    const resultLine = createElementWithText("p", "contact-card-result is-yes");
    resultLine.append(buildTickIcon("tick-icon"), createElementWithText("span", "", "You said yes. Thank you."));
    children.push(resultLine);
  } else {
    children.push(createElementWithText("p", "contact-card-result is-no", `You said no. ${request.receiverName}'s email stays held.`));
  }
  return children;
}

function buildContactCardElement(request) {
  const contactCardState = resolveContactCardState(request);
  const cardElement = createElementWithText("li", "contact-card");
  cardElement.dataset.requestId = request.requestId;
  const isAnswered = contactCardState === CONTACT_CARD_STATE.SAID_YES || contactCardState === CONTACT_CARD_STATE.SAID_NO;
  cardElement.append(...(isAnswered ? buildAnsweredCardChildren(request, contactCardState) : buildQuestionCardChildren(request, contactCardState)));
  return cardElement;
}

function sortRequestsNewestFirst(requests) {
  // No createdAt on the wire; every request gets the same timeout, so a later expiresAt means a newer request.
  return [...requests].sort((first, second) => parseTimestampMilliseconds(second.expiresAt) - parseTimestampMilliseconds(first.expiresAt));
}

// Rebuilds the cards and puts focus back on the same answer button if it still exists.
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
    : [...matchingCard.querySelectorAll(".contact-answer-button")].find((answerButton) => answerButton.dataset.answer === focusedAnswer);
  if (matchingButton !== undefined && !matchingButton.disabled) matchingButton.focus();
}

// Updates countdown text in place; rebuilds the cards only when one crosses its expiry.
function tickContactCountdowns() {
  let hasCardExpired = false;
  pageElements.contactCardList.querySelectorAll(".contact-countdown").forEach((countdownElement) => {
    const remainingMilliseconds = Number(countdownElement.dataset.expiresAtMilliseconds) - Date.now();
    if (remainingMilliseconds <= 0) {
      hasCardExpired = true;
    } else {
      countdownElement.textContent = formatRemainingTimeAsMinutesSeconds(remainingMilliseconds);
    }
  });
  if (hasCardExpired) renderContactCards();
}

function handleContactTappedAnswer(requestId, answer) {
  clearActionStatusMessage();
  if (!sendClientAction({ action: CLIENT_ACTION.ANSWER, requestId, answer })) {
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
  requestsById.set(existingRequest.requestId, { ...existingRequest, status: requestUpdate.status });
  answersInFlightByRequestId.delete(existingRequest.requestId);
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
