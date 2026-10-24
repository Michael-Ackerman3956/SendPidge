// Grandma's questions to Pidge on the Pidge tab: a thread of bubbles and a composer. Sends ask_pidge; renders pidge_reply.
// The thread lives in this page only (slice 10 adds SYNC_STATE.chat so it survives a reload).
import { CHAT_CARD_KIND, CLIENT_ACTION } from "./constants.js";
import { createElementWithText } from "./dom-helpers.js";
import { sendClientAction } from "./websocket.js";
import { ACTION_STATUS_MESSAGE, showActionStatusMessage } from "./action-status.js";

const QUESTION_MAX_CHARACTERS = 1000;

const pageElements = {
  chatThread: document.getElementById("pidge-chat-thread"),
  chatForm: document.getElementById("pidge-chat-form"),
  chatInput: document.getElementById("pidge-chat-input"),
};

function appendBubble(text, bubbleClassName) {
  const bubbleElement = createElementWithText("li", `pidge-chat-bubble ${bubbleClassName}`, text);
  pageElements.chatThread.append(bubbleElement);
  pageElements.chatThread.hidden = false;
  bubbleElement.scrollIntoView({ block: "nearest" });
}

function handleUserSubmittedQuestion(submitEvent) {
  submitEvent.preventDefault();
  const question = pageElements.chatInput.value.trim();
  if (!question) return;
  if (!sendClientAction({ action: CLIENT_ACTION.ASK_PIDGE, text: question })) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  appendBubble(question, "is-grandma");
  pageElements.chatInput.value = "";
}

export function startPidgeChat() {
  pageElements.chatInput.maxLength = QUESTION_MAX_CHARACTERS;
  pageElements.chatForm.addEventListener("submit", handleUserSubmittedQuestion);
}

export function handlePidgeReplyPush(pushMessage) {
  if (typeof pushMessage.text !== "string") {
    console.error("SendPidge: pidge_reply push without text", pushMessage);
    return;
  }
  const isRefusal = pushMessage.card?.kind === CHAT_CARD_KIND.REFUSAL;
  appendBubble(pushMessage.text, isRefusal ? "is-pidge is-refusal" : "is-pidge");
}
