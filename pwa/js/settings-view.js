// Grandma's Settings tab (RECEIVER): her protected address and her personal email, where her recovery pack is mailed. A new
// personal email takes two steps: set_personal_email mails a 6-digit code to it, and confirm_personal_email with that code
// saves it. The state comes from sync_state.personalEmail and each personal_email_state push; a refusal of the action in
// flight shows its reason under the row. Addresses are server or user text: textContent and input values only.
import { ACTION_REJECTED_REASON, CLIENT_ACTION } from "./constants.js";
import { createElementWithText } from "./dom-helpers.js";
import { ACTION_STATUS_MESSAGE, showActionStatusMessage } from "./action-status.js";
import { sendClientAction } from "./websocket.js";

const REFUSAL_TEXT = Object.freeze({
  [CLIENT_ACTION.SET_PERSONAL_EMAIL]: Object.freeze({
    [ACTION_REJECTED_REASON.BAD_MESSAGE]: "Enter a full email address outside SendPidge.",
    [ACTION_REJECTED_REASON.OVER_CAP]: "Too many codes this hour. Try again later.",
    [ACTION_REJECTED_REASON.SEND_FAILED]: "The code email could not be sent. Try again.",
  }),
  [CLIENT_ACTION.CONFIRM_PERSONAL_EMAIL]: Object.freeze({
    [ACTION_REJECTED_REASON.BAD_MESSAGE]: "Enter the 6-digit code.",
    [ACTION_REJECTED_REASON.WRONG_CODE]: "That code is not right.",
    [ACTION_REJECTED_REASON.CODE_EXPIRED]: "That code expired. Send a new one.",
    [ACTION_REJECTED_REASON.NOT_APPLIED]: "Send a new code.",
  }),
});
const LOOKS_LIKE_EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const LOOKS_LIKE_CODE = /^\d{6}$/;

let personalEmailState = null; // { protectedEmail, personalEmail, pendingPersonalEmail } from the server
let isEditingAddress = false;
let actionInFlight = null; // CLIENT_ACTION.SET_PERSONAL_EMAIL or CONFIRM_PERSONAL_EMAIL while it waits for its answer
let errorText = "";
let addressDraft = ""; // what she typed, kept while it sends and after a refusal

const pageElements = {
  topbarTitle: document.getElementById("topbar-title"),
  settingsScreen: document.getElementById("settings-screen"),
  protectedEmail: document.getElementById("settings-protected-email"),
  personalEmailRow: document.getElementById("settings-personal-email"),
};

function isValidPersonalEmailState(state) {
  const isTextOrNull = (value) => value === null || typeof value === "string";
  const isValid = state !== null && typeof state === "object" && typeof state.protectedEmail === "string"
    && isTextOrNull(state.personalEmail) && isTextOrNull(state.pendingPersonalEmail);
  if (!isValid) console.error("SendPidge: personalEmail has a missing field", state);
  return isValid;
}

function buildButton(className, label, onClick) {
  const button = createElementWithText("button", className, label);
  button.type = "button";
  button.disabled = actionInFlight !== null;
  button.addEventListener("click", onClick);
  return button;
}

function sendSettingsAction(action, fieldName, value) {
  errorText = "";
  if (!sendClientAction({ action, [fieldName]: value })) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  actionInFlight = action;
  renderPersonalEmailRow();
}

function handleUserSubmittedAddress(addressInput) {
  const address = addressInput.value.trim();
  addressDraft = address;
  if (!LOOKS_LIKE_EMAIL.test(address)) {
    errorText = REFUSAL_TEXT[CLIENT_ACTION.SET_PERSONAL_EMAIL][ACTION_REJECTED_REASON.BAD_MESSAGE];
    renderPersonalEmailRow();
    return;
  }
  sendSettingsAction(CLIENT_ACTION.SET_PERSONAL_EMAIL, "address", address);
}

function handleUserSubmittedCode(codeInput) {
  const code = codeInput.value.trim();
  if (!LOOKS_LIKE_CODE.test(code)) {
    errorText = REFUSAL_TEXT[CLIENT_ACTION.CONFIRM_PERSONAL_EMAIL][ACTION_REJECTED_REASON.BAD_MESSAGE];
    renderPersonalEmailRow();
    return;
  }
  sendSettingsAction(CLIENT_ACTION.CONFIRM_PERSONAL_EMAIL, "code", code);
}

function buildInputForm({ inputType, label, autocomplete, inputMode, buttonLabel, onSubmit }) {
  const form = createElementWithText("form", "settings-form");
  form.noValidate = true; // the row's own error line explains a bad entry, not the browser's popup
  const input = createElementWithText("input", "onboarding-input settings-input");
  input.type = inputType;
  input.setAttribute("aria-label", label);
  if (inputMode === "numeric") input.placeholder = label;
  input.autocomplete = autocomplete;
  if (inputMode) input.inputMode = inputMode;
  input.disabled = actionInFlight !== null;
  const submitButton = createElementWithText("button", "onboarding-primary-button settings-submit", buttonLabel);
  submitButton.type = "submit";
  submitButton.disabled = actionInFlight !== null;
  form.addEventListener("submit", (event) => { event.preventDefault(); onSubmit(input); });
  form.append(input, submitButton);
  return { form, input };
}

function buildRowHead(trailingElement) {
  const head = createElementWithText("div", "settings-row-head");
  head.append(createElementWithText("span", "settings-label", "Personal email"));
  if (trailingElement) head.append(trailingElement);
  return head;
}

function buildSavedRow() {
  const changeButton = buildButton("settings-link-button", "Change", () => { isEditingAddress = true; errorText = ""; renderPersonalEmailRow(); });
  const value = createElementWithText("div", "settings-sub");
  value.append(createElementWithText("span", "settings-value", personalEmailState.personalEmail),
    createElementWithText("span", "org-chip is-good", "Verified"));
  return [buildRowHead(changeButton), value, createElementWithText("div", "settings-sub", "Recovery packs are sent here.")];
}

function buildAddressRow() {
  const cancelButton = personalEmailState.personalEmail !== null
    ? buildButton("settings-link-button", "Cancel", () => { isEditingAddress = false; errorText = ""; renderPersonalEmailRow(); })
    : null;
  const { form, input } = buildInputForm({
    inputType: "email", label: "Personal email", autocomplete: "email", inputMode: null, buttonLabel: "Send code", onSubmit: handleUserSubmittedAddress,
  });
  input.value = addressDraft;
  return { elements: [buildRowHead(cancelButton), form], focusTarget: input };
}

function buildCodeRow() {
  const pendingAddress = personalEmailState.pendingPersonalEmail;
  const sub = createElementWithText("div", "settings-sub");
  sub.append(createElementWithText("span", "settings-value", pendingAddress), createElementWithText("span", "org-chip is-held", "Check your inbox"));
  const { form, input } = buildInputForm({
    inputType: "text", label: "6-digit code", autocomplete: "one-time-code", inputMode: "numeric", buttonLabel: "Save", onSubmit: handleUserSubmittedCode,
  });
  const actions = createElementWithText("div", "settings-actions");
  actions.append(
    buildButton("settings-link-button", "Send again", () => sendSettingsAction(CLIENT_ACTION.SET_PERSONAL_EMAIL, "address", pendingAddress)),
    buildButton("settings-link-button", "Use a different email", () => { isEditingAddress = true; errorText = ""; renderPersonalEmailRow(); }),
  );
  return { elements: [buildRowHead(null), sub, form, actions], focusTarget: input };
}

function renderPersonalEmailRow() {
  if (personalEmailState === null) return;
  const hadFocusInRow = pageElements.personalEmailRow.contains(document.activeElement);
  let built;
  if (isEditingAddress || (personalEmailState.personalEmail === null && personalEmailState.pendingPersonalEmail === null)) {
    built = buildAddressRow();
  } else if (personalEmailState.pendingPersonalEmail !== null) {
    built = buildCodeRow();
  } else {
    built = { elements: buildSavedRow(), focusTarget: null };
  }
  const children = [...built.elements];
  if (errorText) {
    const errorLine = createElementWithText("p", "onboarding-form-error settings-error", errorText);
    errorLine.setAttribute("role", "alert");
    children.push(errorLine);
  }
  pageElements.personalEmailRow.replaceChildren(...children);
  if (hadFocusInRow && built.focusTarget !== null && actionInFlight === null) built.focusTarget.focus();
}

// From sync_state and every personal_email_state push: the server's rows replace what is shown; an answered action ends.
export function applyPersonalEmailState(state) {
  if (!isValidPersonalEmailState(state)) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  const wasSendingAddress = actionInFlight === CLIENT_ACTION.SET_PERSONAL_EMAIL;
  personalEmailState = state;
  if (wasSendingAddress || state.pendingPersonalEmail === null) isEditingAddress = false;
  if (wasSendingAddress) addressDraft = "";
  actionInFlight = null;
  pageElements.protectedEmail.textContent = state.protectedEmail;
  renderPersonalEmailRow();
}

// A refusal while a Settings action waits: its reason shows under the row (true). Any other refusal is not this view's (false).
export function handleSettingsActionRejected(reason) {
  if (actionInFlight === null) return false;
  const refusals = REFUSAL_TEXT[actionInFlight];
  errorText = Object.prototype.hasOwnProperty.call(refusals, reason) ? refusals[reason] : ACTION_STATUS_MESSAGE.GENERIC_FAILURE;
  actionInFlight = null;
  renderPersonalEmailRow();
  return true;
}

export function showSettingsView() {
  pageElements.topbarTitle.textContent = "Settings";
  pageElements.settingsScreen.hidden = false;
}

export function hideSettingsView() {
  pageElements.settingsScreen.hidden = true;
}
