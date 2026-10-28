// Grandma's composer (RECEIVER): a circle name, one of the channels the server lists for that person (the entry's sendTo), the
// text, Send. The PWA never decides who can be written to: names come from listCircleEntries and chips from sendTo only, and
// pidge-send checks everything again. Names and addresses come from the server: textContent only.
import { ACTION_REJECTED_REASON, CHANNEL, CLIENT_ACTION, PROVIDER_STATUS } from "./constants.js";
import { ACTION_STATUS_MESSAGE } from "./action-status.js";
import { listCircleEntries } from "./circle-view.js";
import { createElementWithText } from "./dom-helpers.js";
import { buildThreadKey } from "./thread-view.js";
import { sendClientAction } from "./websocket.js";

const MAXIMUM_TEXT_LENGTH = 1000; // lambda/shared/messenger_send.py parse_send_request: 1..1000 characters after strip
const CHANNEL_CHIP_LABELS = Object.freeze({
  [CHANNEL.EMAIL]: "Email",
  [CHANNEL.WHATSAPP]: "WhatsApp",
  [CHANNEL.RCS]: "RCS",
  [CHANNEL.SMS]: "SMS",
});
// One short line per reason pidge-send refuses with; any other reason shows the generic line.
const REFUSAL_TEXT = Object.freeze({
  [ACTION_REJECTED_REASON.NOT_A_CONTACT]: "They are not in your circle.",
  [ACTION_REJECTED_REASON.OVER_CAP]: "Too many messages this hour.",
  [ACTION_REJECTED_REASON.CHANNEL_CLOSED]: "WhatsApp opens when they write to you.",
  [ACTION_REJECTED_REASON.THREAD_HELD]: "A message from them is still on hold.",
  [ACTION_REJECTED_REASON.SEND_FAILED]: "Not sent. Try again.",
  [ACTION_REJECTED_REASON.NOT_ALLOWED]: "This can't be sent from here.",
  [ACTION_REJECTED_REASON.BAD_MESSAGE]: "Write 1 to 1,000 characters.",
});
const NOBODY_TO_WRITE_TO_TEXT = "No one to write to yet.";

let isComposerOpen = false;
let selectedContactUserId = null;
let selectedChannel = null;
let draftText = "";
let inFlightSend = null; // { channel, to } from Send until pidge-send's out row or refusal
let refusalText = "";
let sendButton = null;
let isThreadHeld = () => false;

const pageElements = {
  composeScreen: document.getElementById("compose-screen"),
};

// The entry's sendTo as [channel, address] pairs, or [] after logging when it is absent or malformed (never derived from reach).
function listSendToPairs(entry) {
  const sendTo = entry.sendTo;
  const isValid = sendTo !== null && typeof sendTo === "object" && !Array.isArray(sendTo)
    && Object.entries(sendTo).every(([channel, address]) => Object.prototype.hasOwnProperty.call(CHANNEL_CHIP_LABELS, channel)
      && typeof address === "string");
  if (!isValid) {
    console.error("SendPidge: circle entry has no valid sendTo", entry.contactUserId);
    return [];
  }
  return Object.entries(sendTo);
}

function listWritableEntries() {
  return listCircleEntries().filter((entry) => listSendToPairs(entry).length > 0);
}

// { channel, to } for the selected name and chip, or null while either is missing or no longer in sendTo.
function findSelectedSendTarget() {
  const selectedEntry = listCircleEntries().find((entry) => entry.contactUserId === selectedContactUserId);
  if (selectedEntry === undefined) return null;
  const selectedPair = listSendToPairs(selectedEntry).find(([channel]) => channel === selectedChannel);
  return selectedPair === undefined ? null : { channel: selectedPair[0], to: selectedPair[1] };
}

function canSend() {
  const sendTarget = findSelectedSendTarget();
  const trimmedLength = draftText.trim().length;
  return inFlightSend === null && sendTarget !== null && trimmedLength > 0 && trimmedLength <= MAXIMUM_TEXT_LENGTH
    && !isThreadHeld(buildThreadKey(sendTarget.channel, sendTarget.to));
}

function buildChoiceButton(label, isSelected, onTapped) {
  const choiceButton = createElementWithText("button", "compose-choice", label);
  choiceButton.type = "button";
  choiceButton.setAttribute("aria-pressed", String(isSelected));
  choiceButton.addEventListener("click", onTapped);
  return choiceButton;
}

function buildChoiceGroup(groupLabel, choiceButtons) {
  const choiceGroup = createElementWithText("div", "compose-choices");
  choiceGroup.setAttribute("role", "group");
  choiceGroup.setAttribute("aria-label", groupLabel);
  choiceGroup.append(...choiceButtons);
  return choiceGroup;
}

// A tapped name or chip re-renders the composer, so focus moves on to the text instead of being lost with the old button.
function renderComposerAndFocusText() {
  renderComposer();
  pageElements.composeScreen.querySelector(".compose-text").focus();
}

function selectContact(entry) {
  selectedContactUserId = entry.contactUserId;
  selectedChannel = listSendToPairs(entry)[0][0]; // the server's first channel for them
  renderComposerAndFocusText();
}

function buildTextArea() {
  const textArea = createElementWithText("textarea", "onboarding-input compose-text");
  textArea.maxLength = MAXIMUM_TEXT_LENGTH;
  textArea.rows = 5;
  textArea.value = draftText;
  textArea.setAttribute("aria-label", "Message");
  textArea.addEventListener("input", () => {
    draftText = textArea.value;
    sendButton.disabled = !canSend();
  });
  return textArea;
}

function renderComposer() {
  const writableEntries = listWritableEntries();
  const composerChildren = [buildChoiceGroup("To", writableEntries.map((entry) => buildChoiceButton(entry.contactName,
    entry.contactUserId === selectedContactUserId, () => selectContact(entry))))];
  if (writableEntries.length === 0) composerChildren.push(createElementWithText("p", "onboarding-form-error", NOBODY_TO_WRITE_TO_TEXT));
  const selectedEntry = writableEntries.find((entry) => entry.contactUserId === selectedContactUserId);
  if (selectedEntry !== undefined) {
    composerChildren.push(buildChoiceGroup("Send with", listSendToPairs(selectedEntry).map(([channel]) => buildChoiceButton(
      CHANNEL_CHIP_LABELS[channel], channel === selectedChannel, () => { selectedChannel = channel; renderComposerAndFocusText(); }))));
  }
  const refusalLine = createElementWithText("p", "onboarding-form-error", refusalText);
  refusalLine.setAttribute("role", "alert");
  sendButton = createElementWithText("button", "onboarding-primary-button", "Send");
  sendButton.type = "button";
  sendButton.disabled = !canSend();
  sendButton.addEventListener("click", handleUserTappedSend);
  composerChildren.push(buildTextArea(), refusalLine, sendButton);
  pageElements.composeScreen.replaceChildren(...composerChildren);
}

function handleUserTappedSend() {
  if (!canSend()) return;
  const sendTarget = findSelectedSendTarget();
  refusalText = "";
  if (!sendClientAction({ action: CLIENT_ACTION.SEND_MESSAGE, channel: sendTarget.channel, to: sendTarget.to, text: draftText.trim() })) {
    refusalText = ACTION_STATUS_MESSAGE.GENERIC_FAILURE;
  } else {
    inFlightSend = sendTarget;
  }
  renderComposer();
}

// Opens the composer, with { contactUserId, channel } preselected (Reply) or nothing selected (Compose). An unsent draft stays.
export function openComposer(preselection, isThreadHeldCallback) {
  isThreadHeld = isThreadHeldCallback;
  isComposerOpen = true;
  selectedContactUserId = preselection === null ? null : preselection.contactUserId;
  selectedChannel = preselection === null ? null : preselection.channel;
  refusalText = "";
  pageElements.composeScreen.hidden = false;
  renderComposer();
  const focusTarget = pageElements.composeScreen.querySelector(preselection === null ? ".compose-choice" : ".compose-text");
  if (focusTarget !== null) focusTarget.focus();
}

// Closing forgets a send still in flight: its later refusal shows as the app's generic line instead.
export function closeComposer() {
  isComposerOpen = false;
  inFlightSend = null;
  pageElements.composeScreen.hidden = true;
}

export function isComposerShown() {
  return isComposerOpen;
}

// The rows changed (a hold landed or was answered): only Send's enabled state is refreshed, so typing is never interrupted.
export function refreshComposerSendState() {
  if (isComposerOpen && sendButton !== null) sendButton.disabled = !canSend();
}

// What Reply preselects for a thread: the first circle entry whose sendTo on the thread's channel is the thread's address.
export function findReplyTarget(threadKey, channel) {
  const replyEntry = listWritableEntries().find((entry) => listSendToPairs(entry)
    .some(([sendToChannel, address]) => sendToChannel === channel && buildThreadKey(channel, address) === threadKey));
  return replyEntry === undefined ? null : { contactUserId: replyEntry.contactUserId, channel };
}

// True (consumed) only while this composer's send is in flight: shows the reason's one line and re-enables Send.
export function handleComposerActionRejected(reason) {
  if (inFlightSend === null) return false;
  inFlightSend = null;
  refusalText = Object.prototype.hasOwnProperty.call(REFUSAL_TEXT, reason) ? REFUSAL_TEXT[reason] : ACTION_STATUS_MESSAGE.GENERIC_FAILURE;
  renderComposer(); // a send is in flight only while the composer is open (closing forgets it)
  return true;
}

// An out row arrived: when it is the in-flight send, delivered, the draft is cleared and the composer closes (true). A failed
// row waits for its send_failed refusal, which shows the line.
export function handleComposerOutMessage(outRow) {
  if (inFlightSend === null || outRow.providerStatus !== PROVIDER_STATUS.SENT || outRow.channel !== inFlightSend.channel
    || outRow.threadKey !== buildThreadKey(inFlightSend.channel, inFlightSend.to)) return false;
  draftText = "";
  closeComposer();
  return true;
}
