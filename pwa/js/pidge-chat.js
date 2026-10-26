// Grandma's questions to Pidge on the Pidge tab: a greeting bubble with suggestion chips, the thread from sync_state.chat (so a
// reload keeps it), the composer with dictation, and live pidge_reply bubbles. Text from the server goes in through textContent.
import { DICTATION_ENABLED } from "./config.js";
import { CHAT_CARD_KIND, CHAT_ROLE, CLIENT_ACTION } from "./constants.js";
import { createElementWithText } from "./dom-helpers.js";
import { sendClientAction } from "./websocket.js";
import { ACTION_STATUS_MESSAGE, showActionStatusMessage } from "./action-status.js";

const QUESTION_MAX_CHARACTERS = 1000;
const GREETING_TEXT = "Ask me about an email I held, or what to do next.";
const SUGGESTION_CHIPS = Object.freeze(["Was that Jimin?", "What did it say?", "I already paid", "Write back to Jimin"]);
const KNOWN_CHAT_ROLES = new Set(Object.values(CHAT_ROLE));

const pageElements = {
  topbarTitle: document.getElementById("topbar-title"),
  pidgeScreen: document.getElementById("pidge-screen"),
  chatThread: document.getElementById("pidge-chat-thread"),
  chatForm: document.getElementById("pidge-chat-form"),
  chatInput: document.getElementById("pidge-chat-input"),
  micButton: document.getElementById("pidge-chat-mic"),
};

function buildBubbleElement({ role, text, isRefusal, chips }) {
  const classNames = ["pidge-chat-bubble", role === CHAT_ROLE.GRANDMA ? "is-grandma" : "is-pidge", isRefusal ? "is-refusal" : ""];
  const bubble = createElementWithText("li", classNames.filter(Boolean).join(" "));
  if (role === CHAT_ROLE.PIDGE) bubble.append(createElementWithText("span", "pidge-chat-label", "Pidge"));
  bubble.append(createElementWithText("span", "", text));
  if (chips !== undefined) {
    const chipRow = createElementWithText("div", "pidge-chat-chips");
    chips.forEach((chipText) => {
      const chip = createElementWithText("button", "pidge-chat-chip", chipText);
      chip.type = "button";
      chip.addEventListener("click", () => askPidge(chipText));
      chipRow.append(chip);
    });
    bubble.append(chipRow);
  }
  return bubble;
}

function appendBubble(bubbleFields) {
  const bubble = buildBubbleElement(bubbleFields);
  pageElements.chatThread.append(bubble);
  bubble.scrollIntoView({ block: "nearest" });
}

function askPidge(question) {
  const trimmedQuestion = question.trim();
  if (!trimmedQuestion) return;
  if (!sendClientAction({ action: CLIENT_ACTION.ASK_PIDGE, text: trimmedQuestion })) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  appendBubble({ role: CHAT_ROLE.GRANDMA, text: trimmedQuestion, isRefusal: false });
  pageElements.chatInput.value = "";
}

function handleUserSubmittedQuestion(submitEvent) {
  submitEvent.preventDefault();
  askPidge(pageElements.chatInput.value);
}

function isValidChatMessage(message) {
  const isValid = message !== null && typeof message === "object" && KNOWN_CHAT_ROLES.has(message.role) && typeof message.text === "string";
  if (!isValid) console.error("SendPidge: chat message in sync_state has an unknown role or no text", message);
  return isValid;
}

function speechRecognitionClass() {
  return window.SpeechRecognition ?? window.webkitSpeechRecognition ?? null;
}

// Browser dictation fills the composer; an error leaves it unchanged. The mic is absent without the API or the flag.
function handleUserTappedMic() {
  const Recognition = speechRecognitionClass();
  const recognizer = new Recognition();
  recognizer.lang = navigator.language || "en-US";
  recognizer.interimResults = false;
  pageElements.micButton.classList.add("is-listening");
  recognizer.addEventListener("result", (resultEvent) => { pageElements.chatInput.value = resultEvent.results[0][0].transcript; });
  recognizer.addEventListener("end", () => pageElements.micButton.classList.remove("is-listening"));
  recognizer.addEventListener("error", (errorEvent) => {
    console.error("SendPidge: dictation failed", errorEvent.error);
    pageElements.micButton.classList.remove("is-listening");
  });
  recognizer.start();
}

export function showPidgeView() {
  pageElements.topbarTitle.textContent = "Pidge";
  pageElements.pidgeScreen.hidden = false;
}

export function hidePidgeView() {
  pageElements.pidgeScreen.hidden = true;
}

export function startPidgeChat() {
  pageElements.chatInput.maxLength = QUESTION_MAX_CHARACTERS;
  pageElements.chatForm.addEventListener("submit", handleUserSubmittedQuestion);
  pageElements.micButton.hidden = !(DICTATION_ENABLED && speechRecognitionClass() !== null);
  pageElements.micButton.addEventListener("click", handleUserTappedMic);
  pageElements.chatThread.replaceChildren(buildBubbleElement({ role: CHAT_ROLE.PIDGE, text: GREETING_TEXT, isRefusal: false, chips: SUGGESTION_CHIPS }));
}

// Rebuilds the thread from the server's rows (oldest first) under the greeting; hides the composer when chat is off.
export function applyChatSyncState(chatMessages, isChatEnabled) {
  if (!Array.isArray(chatMessages) || typeof isChatEnabled !== "boolean") {
    console.error("SendPidge: receiver sync_state has no chat array or chatEnabled flag", chatMessages, isChatEnabled);
    return;
  }
  pageElements.chatForm.hidden = !isChatEnabled;
  const greetingChips = isChatEnabled ? SUGGESTION_CHIPS : undefined;
  const bubbles = [buildBubbleElement({ role: CHAT_ROLE.PIDGE, text: GREETING_TEXT, isRefusal: false, chips: greetingChips })];
  chatMessages.filter(isValidChatMessage).forEach((message) => {
    bubbles.push(buildBubbleElement({ role: message.role, text: message.text, isRefusal: message.card?.kind === CHAT_CARD_KIND.REFUSAL }));
  });
  pageElements.chatThread.replaceChildren(...bubbles);
  bubbles[bubbles.length - 1].scrollIntoView({ block: "nearest" });
}

export function handlePidgeReplyPush(pushMessage) {
  if (typeof pushMessage.text !== "string") {
    console.error("SendPidge: pidge_reply push without text", pushMessage);
    return;
  }
  appendBubble({ role: CHAT_ROLE.PIDGE, text: pushMessage.text, isRefusal: pushMessage.card?.kind === CHAT_CARD_KIND.REFUSAL });
}
