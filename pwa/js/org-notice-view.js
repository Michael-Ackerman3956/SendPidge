// A resident's alerts from her home on the Pidge tab, above the chat: the alert, the home's name and time, and Got it / Need
// help. Both buttons lock while her answer is in flight and once she has answered (her answer stays marked). Text and names
// come from the server: textContent only. Success has no reply of its own: the answered notice comes back as org_notice.
import { ACK_ANSWER, CLIENT_ACTION } from "./constants.js";
import { createElementWithText, formatTimestampForDisplay, parseTimestampMilliseconds } from "./dom-helpers.js";
import { ACTION_STATUS_MESSAGE, showActionStatusMessage } from "./action-status.js";
import { sendClientAction } from "./websocket.js";

const ANSWER_BUTTONS = Object.freeze([
  { answer: ACK_ANSWER.GOT_IT, label: "Got it" },
  { answer: ACK_ANSWER.NEED_HELP, label: "Need help" },
]);
const KNOWN_ACK_ANSWERS = new Set(Object.values(ACK_ANSWER));

const noticeList = document.getElementById("org-notice-list");
let shownNotices = []; // newest first, as the server sends them
const broadcastIdsWithAnswerInFlight = new Set();

function isValidNotice(notice) {
  const isValid = notice !== null && typeof notice === "object" && typeof notice.broadcastId === "string"
    && typeof notice.orgName === "string" && typeof notice.text === "string" && parseTimestampMilliseconds(notice.sentAt) !== null
    && (notice.answer === null || KNOWN_ACK_ANSWERS.has(notice.answer));
  if (!isValid) console.error("SendPidge: org notice has a missing field or unknown answer", notice);
  return isValid;
}

function handleUserTappedAnswer(notice, answer) {
  if (notice.answer !== null || broadcastIdsWithAnswerInFlight.has(notice.broadcastId)) return;
  if (!sendClientAction({ action: CLIENT_ACTION.ORG_BROADCAST_ACK, broadcastId: notice.broadcastId, answer })) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  broadcastIdsWithAnswerInFlight.add(notice.broadcastId);
  renderNotices();
}

function buildNoticeElement(notice) {
  const isLocked = notice.answer !== null || broadcastIdsWithAnswerInFlight.has(notice.broadcastId);
  const answerRow = createElementWithText("div", "org-notice-answers");
  ANSWER_BUTTONS.forEach(({ answer, label }) => {
    const answerButton = createElementWithText("button", "org-notice-answer", label);
    answerButton.type = "button";
    answerButton.disabled = isLocked;
    answerButton.setAttribute("aria-pressed", String(notice.answer === answer));
    answerButton.addEventListener("click", () => handleUserTappedAnswer(notice, answer));
    answerRow.append(answerButton);
  });
  const noticeElement = createElementWithText("li", "org-notice");
  noticeElement.append(
    createElementWithText("p", "org-notice-text", notice.text),
    createElementWithText("p", "org-notice-meta", `${notice.orgName} · ${formatTimestampForDisplay(notice.sentAt)}`),
    answerRow,
  );
  return noticeElement;
}

function renderNotices() {
  noticeList.replaceChildren(...shownNotices.map(buildNoticeElement));
}

// From the resident's sync_state: her newest alerts replace what is shown.
export function applyOrgNoticeSyncState(notices) {
  if (!Array.isArray(notices)) {
    console.error("SendPidge: receiver sync_state has no orgNotices list", notices);
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  shownNotices = notices.filter(isValidNotice);
  shownNotices.forEach((notice) => { if (notice.answer !== null) broadcastIdsWithAnswerInFlight.delete(notice.broadcastId); });
  renderNotices();
}

// A new alert goes on top; her own answer (from any of her devices) replaces its notice.
export function handleOrgNoticePush(notice) {
  if (!isValidNotice(notice)) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  broadcastIdsWithAnswerInFlight.delete(notice.broadcastId);
  shownNotices = [notice, ...shownNotices.filter((shownNotice) => shownNotice.broadcastId !== notice.broadcastId)]
    .sort((first, second) => parseTimestampMilliseconds(second.sentAt) - parseTimestampMilliseconds(first.sentAt));
  renderNotices();
}

// A refused answer (already answered, not hers, malformed): every notice can be tapped again; a re-sync shows the truth.
export function handleOrgNoticeActionRejected() {
  if (broadcastIdsWithAnswerInFlight.size === 0) return;
  broadcastIdsWithAnswerInFlight.clear();
  renderNotices();
}
