// Onboarding (RECEIVER): welcome → who to ask → the Add-someone form → send them the note → done. The server writes the
// contact PENDING and sends back the note (code + the contact's own Pidge link; an edit's note has the code only) once, to this socket only; Grandma sends it
// from her own phone. Each WhatsApp number is checked when the code comes back from it (the row turns verified on the first),
// or, with the server's mock flag on, when the labelled demo button plays that reply. Nothing typed here is shown to anyone else.
// Edit opens the same form on someone already in the circle, filled from their reachPoints, with no welcome steps and no dots.
import { ACTION_REJECTED_REASON, CHANNEL, CLIENT_ACTION, RELATIONSHIP, RELATIONSHIP_LABELS, VERIFIED_VIA } from "./constants.js";
import {
  CONTACT_ROW_LIST, FORM_ERROR_TEXT, SAVED_ROW_STATE_LABEL, describeSavedRowState, emptyFormDraft, formDraftFromCircleEntry,
  isKnownRelationship, isLastCheckedWhatsAppRow, validateFormDraft,
} from "./contact-form-draft.js";
import { buildCopyButton, buildPidgeIconElement, buildSvgIcon, createElementWithText } from "./dom-helpers.js";
import { getContactVerificationFlags, listCircleEntries } from "./circle-view.js";
import { sendClientAction } from "./websocket.js";
import { ACTION_STATUS_MESSAGE, clearActionStatusMessage, showActionStatusMessage } from "./action-status.js";

const STEP_COUNT = 5;
const BACK_CHEVRON_PATH = "M15 5 8 12l7 7";
const REMOVE_CROSS_PATH = "M6 6l12 12M18 6 6 18";

const ONBOARDING_STEP = Object.freeze({
  WELCOME: 0,
  WHO_TO_ASK: 1,
  ADD_CONTACT: 2,
  WAIT_FOR_CODE: 3,
  DONE: 4,
});

// Back walks one step back; the waiting and done steps have no step before them (the contact is already written).
const PREVIOUS_STEP = Object.freeze({
  [ONBOARDING_STEP.WHO_TO_ASK]: ONBOARDING_STEP.WELCOME,
  [ONBOARDING_STEP.ADD_CONTACT]: ONBOARDING_STEP.WHO_TO_ASK,
});

// Where onboarding opens: the first run from the welcome, "Add someone" straight at the form, a Circle row in edit.
export const ONBOARDING_START = Object.freeze({
  WELCOME: "welcome",
  ADD_CONTACT: "add_contact",
  EDIT_CONTACT: "edit_contact",
});

let activeStep = ONBOARDING_STEP.WELCOME;
let openedAtStep = ONBOARDING_STEP.WELCOME;
let editedContact = null; // the circle entry being edited; null while adding
let contactAwaitingCode = null; // {contactUserId, contactName, noteText, code, waitingNumbers} from contact_added or contact_edited
let isFormInFlight = false;
let isVerifyInFlight = false;
let formErrorText = "";
let formDraft = emptyFormDraft();
let onFinished = () => {};
let onExited = () => {};

const pageElements = {
  topbarTitle: document.getElementById("topbar-title"),
  onboardingScreen: document.getElementById("onboarding-screen"),
};

function buildPrimaryButton(labelText, onClick) {
  const button = createElementWithText("button", "onboarding-primary-button", labelText);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function buildSecondaryButton(labelText, onClick) {
  const button = createElementWithText("button", "contact-secondary-button", labelText);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function buildStepDots() {
  const dots = createElementWithText("div", "onboarding-dots");
  for (let stepIndex = 0; stepIndex < STEP_COUNT; stepIndex += 1) {
    dots.append(createElementWithText("i", stepIndex === activeStep ? "is-on" : ""));
  }
  return dots;
}

// From the step setup opened on, or a step with none before it, Back leaves setup. It waits while the form is in flight,
// because the note is taken only on the form step.
function handleUserTappedBack() {
  const previousStep = PREVIOUS_STEP[activeStep];
  if (activeStep === openedAtStep || previousStep === undefined) {
    onExited();
    return;
  }
  formErrorText = "";
  advanceToStep(previousStep);
}

function buildBackButton() {
  const backButton = createElementWithText("button", "onboarding-back-button");
  backButton.type = "button";
  backButton.disabled = isFormInFlight;
  backButton.append(buildSvgIcon("onboarding-back-icon", BACK_CHEVRON_PATH), createElementWithText("span", "", "Back"));
  backButton.addEventListener("click", handleUserTappedBack);
  return backButton;
}

function buildStepFrame(titleText, subtitleText, ...bodyChildren) {
  const frame = createElementWithText("div", "onboarding-step");
  const hero = createElementWithText("div", "onboarding-hero");
  hero.append(buildPidgeIconElement("onboarding-pidge", ""));
  frame.append(buildBackButton(), hero, createElementWithText("h2", "onboarding-title", titleText));
  if (subtitleText) frame.append(createElementWithText("p", "onboarding-subtitle", subtitleText));
  frame.append(...bodyChildren, createElementWithText("div", "onboarding-grow"));
  if (editedContact === null) frame.append(buildStepDots());
  return frame;
}

// A new step moves focus to its title (a re-render of the same step leaves focus where it is).
function advanceToStep(step) {
  activeStep = step;
  renderOnboarding();
  pageElements.onboardingScreen.querySelector(".onboarding-title")?.focus();
}

// ---- the form ----

function buildField(labelText, inputElement) {
  const field = createElementWithText("label", "onboarding-field");
  field.append(createElementWithText("span", "onboarding-field-label", labelText), inputElement);
  return field;
}

function buildTextInput(name, inputType, autocomplete, placeholder) {
  const input = document.createElement("input");
  input.type = inputType;
  input.name = name;
  input.autocomplete = autocomplete;
  input.placeholder = placeholder;
  input.className = "onboarding-input";
  input.value = formDraft[name];
  input.addEventListener("input", () => { formDraft[name] = input.value; });
  return input;
}

// A seeded person has no relationship yet: "Choose" shows until she picks one.
function buildRelationshipSelect() {
  const select = document.createElement("select");
  select.name = "relationship";
  select.className = "onboarding-input";
  if (!isKnownRelationship(formDraft.relationship)) {
    const choosePrompt = createElementWithText("option", "", "Choose");
    choosePrompt.value = "";
    choosePrompt.selected = true;
    choosePrompt.disabled = true;
    select.append(choosePrompt);
  }
  Object.values(RELATIONSHIP).forEach((relationship) => {
    const option = createElementWithText("option", "", RELATIONSHIP_LABELS[relationship]);
    option.value = relationship;
    option.selected = relationship === formDraft.relationship;
    select.append(option);
  });
  select.addEventListener("change", () => { formDraft.relationship = select.value; });
  return select;
}

function renderSavedRowState(stateElement, rowList, typedValue) {
  const stateLabel = describeSavedRowState(editedContact, rowList, typedValue, Date.now());
  stateElement.hidden = stateLabel === null;
  stateElement.textContent = stateLabel === null ? "" : stateLabel; // the input's description: never a stale label
  stateElement.classList.toggle("is-checked", stateLabel === SAVED_ROW_STATE_LABEL.CHECKED);
}

function rowInputName(rowList, rowIndex) {
  return `${rowList.draftKey}${rowIndex}`;
}

function focusRowInput(rowList, rowIndex) {
  pageElements.onboardingScreen.querySelector(`input[name="${rowInputName(rowList, rowIndex)}"]`)?.focus();
}

function buildRowInput(rowList, rowIndex, stateElement) {
  const input = document.createElement("input");
  input.type = rowList.inputType;
  input.name = rowInputName(rowList, rowIndex);
  input.autocomplete = rowIndex === 0 ? rowList.autocomplete : "off";
  input.placeholder = rowIndex === 0 ? rowList.placeholder : "";
  input.className = "onboarding-input";
  input.setAttribute("aria-label", `${rowList.label} ${rowIndex + 1}`);
  input.setAttribute("aria-describedby", stateElement.id);
  input.value = formDraft[rowList.draftKey][rowIndex];
  input.addEventListener("input", () => {
    formDraft[rowList.draftKey][rowIndex] = input.value;
    renderSavedRowState(stateElement, rowList, input.value);
  });
  return input;
}

function buildRemoveRowButton(rowList, rowIndex, stateElement) {
  const removeButton = createElementWithText("button", "onboarding-remove-row-button");
  removeButton.type = "button";
  removeButton.disabled = isLastCheckedWhatsAppRow(formDraft, editedContact, rowList, rowIndex);
  removeButton.setAttribute("aria-label", `Remove ${rowList.label} ${rowIndex + 1}`);
  removeButton.setAttribute("aria-describedby", stateElement.id);
  removeButton.append(buildSvgIcon("onboarding-remove-row-icon", REMOVE_CROSS_PATH));
  removeButton.addEventListener("click", () => {
    formDraft[rowList.draftKey].splice(rowIndex, 1);
    renderOnboarding();
    focusRowInput(rowList, rowIndex - 1);
  });
  return removeButton;
}

function handleUserAddedRow(rowList) {
  formDraft[rowList.draftKey].push("");
  renderOnboarding();
  focusRowInput(rowList, formDraft[rowList.draftKey].length - 1);
}

// One group of rows: the first row stays, the rest can be removed, and "+ Add" stops at the server's maximum.
function buildRepeatableRows(rowList) {
  const rowValues = formDraft[rowList.draftKey];
  const group = createElementWithText("div", "onboarding-field");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", rowList.label);
  group.append(createElementWithText("span", "onboarding-field-label", rowList.label));
  rowValues.forEach((_, rowIndex) => {
    const row = createElementWithText("div", "onboarding-repeat-row");
    const stateElement = createElementWithText("span", "onboarding-row-state");
    stateElement.id = `${rowInputName(rowList, rowIndex)}-state`;
    renderSavedRowState(stateElement, rowList, rowValues[rowIndex]);
    row.append(buildRowInput(rowList, rowIndex, stateElement), stateElement);
    if (rowIndex > 0) row.append(buildRemoveRowButton(rowList, rowIndex, stateElement));
    group.append(row);
  });
  if (rowValues.length < rowList.maximumRows) {
    const addRowButton = createElementWithText("button", "onboarding-add-row-button", rowList.addLabel);
    addRowButton.type = "button";
    addRowButton.addEventListener("click", () => handleUserAddedRow(rowList));
    group.append(addRowButton);
  }
  return group;
}

function handleUserSubmittedContactForm() {
  clearActionStatusMessage();
  const { errorText, form } = validateFormDraft(formDraft);
  if (errorText) { formErrorText = errorText; renderOnboarding(); return; }
  const action = editedContact === null
    ? { action: CLIENT_ACTION.ADD_CONTACT, ...form }
    : { action: CLIENT_ACTION.EDIT_CONTACT, contactUserId: editedContact.contactUserId, ...form };
  if (!sendClientAction(action)) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  formErrorText = "";
  isFormInFlight = true;
  renderOnboarding();
}

// Chrome on Android has the Contact Picker API; everywhere else the form is typed (spec: manual entry fallback).
async function handleUserChoseFromContacts() {
  try {
    const [picked] = await navigator.contacts.select(["name", "email", "tel"], { multiple: false });
    if (!picked) return;
    if (typeof picked.name?.[0] === "string") formDraft.contactName = picked.name[0];
    const pickedEmailAddresses = Array.isArray(picked.email) ? picked.email.filter((address) => typeof address === "string") : [];
    if (pickedEmailAddresses.length > 0) formDraft.emailAddresses = pickedEmailAddresses.slice(0, CONTACT_ROW_LIST.EMAIL.maximumRows);
    const pickedNumbers = Array.isArray(picked.tel) ? picked.tel.filter((number) => typeof number === "string") : [];
    if (pickedNumbers.length > 0) { // the card cannot say which number has WhatsApp: the first goes there, the rest to Phone
      formDraft.whatsappNumbers = [pickedNumbers[0]];
      formDraft.phoneNumbers = pickedNumbers.length > 1 ? pickedNumbers.slice(1, 1 + CONTACT_ROW_LIST.PHONE.maximumRows) : [""];
    }
    renderOnboarding();
  } catch (error) {
    console.warn("SendPidge: the contact picker was closed or refused", error);
  }
}

function buildContactForm() {
  const form = createElementWithText("div", "onboarding-form");
  if (editedContact === null && typeof navigator.contacts?.select === "function") {
    form.append(buildSecondaryButton("Choose from contacts", handleUserChoseFromContacts));
  }
  form.append(
    buildField("Name", buildTextInput("contactName", "text", "name", "")),
    buildField("Who they are", buildRelationshipSelect()),
    buildRepeatableRows(CONTACT_ROW_LIST.WHATSAPP),
    buildRepeatableRows(CONTACT_ROW_LIST.PHONE),
    buildRepeatableRows(CONTACT_ROW_LIST.EMAIL),
  );
  if (formErrorText) {
    const formError = createElementWithText("p", "onboarding-form-error", formErrorText);
    formError.setAttribute("role", "alert");
    form.append(formError);
  }
  const submitLabel = editedContact === null ? (isFormInFlight ? "Adding…" : "Add to my circle") : (isFormInFlight ? "Saving…" : "Save");
  const submitButton = buildPrimaryButton(submitLabel, handleUserSubmittedContactForm);
  submitButton.disabled = isFormInFlight;
  form.append(submitButton);
  return form;
}

// ---- the note and the wait ----

function handleUserPlayedContactReply() {
  clearActionStatusMessage();
  const { contactUserId, code } = contactAwaitingCode;
  if (!sendClientAction({ action: CLIENT_ACTION.VERIFY_CONTACT, contactUserId, code })) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  isVerifyInFlight = true;
  renderOnboarding();
}

function buildShareButton(noteText) {
  return buildSecondaryButton("Share", () => {
    navigator.share({ text: noteText }).catch((error) => console.warn("SendPidge: share sheet closed", error));
  });
}

function buildWaitStep() {
  const notePreview = createElementWithText("div", "onboarding-code-preview");
  notePreview.append(createElementWithText("p", "onboarding-code-to", `To ${contactAwaitingCode.contactName}`),
    createElementWithText("p", "onboarding-note-text", contactAwaitingCode.noteText));
  const noteActions = createElementWithText("div", "onboarding-note-actions");
  if (typeof navigator.share === "function") noteActions.append(buildShareButton(contactAwaitingCode.noteText));
  noteActions.append(buildCopyButton(contactAwaitingCode.noteText));
  const waitRow = createElementWithText("p", "onboarding-wait-row");
  waitRow.append(createElementWithText("span", "checking-pulse"), createElementWithText("span", "", `Waiting for ${contactAwaitingCode.contactName}`));
  const children = [notePreview, noteActions, waitRow];
  if (getContactVerificationFlags().mock) {
    const demoButton = buildSecondaryButton(isVerifyInFlight ? "Sending…" : `Demo: ${contactAwaitingCode.contactName} replies ${contactAwaitingCode.code}`, handleUserPlayedContactReply);
    demoButton.disabled = isVerifyInFlight;
    children.push(createElementWithText("p", "onboarding-demo-note", "Demo: nothing is sent. The reply is played by the button."), demoButton);
  }
  const subtitleText = getContactVerificationFlags().whatsapp
    ? "Send it from your own phone. When they reply to Pidge with the code, they are in."
    : "Send it from your own phone. This stack cannot take their reply on WhatsApp yet.";
  return buildStepFrame(`Send ${contactAwaitingCode.contactName} this note`, subtitleText, ...children);
}

function buildStepElement() {
  switch (activeStep) {
    case ONBOARDING_STEP.WELCOME:
      return buildStepFrame("Pidge asks the real person.", "When an email says it's from someone in your circle and asks for money, Pidge checks with them before you act.",
        buildPrimaryButton("Get started", () => advanceToStep(ONBOARDING_STEP.WHO_TO_ASK)));
    case ONBOARDING_STEP.WHO_TO_ASK:
      return buildStepFrame("Who should Pidge ask?", "The people who write to you. Pidge only ever contacts them.",
        buildPrimaryButton("Add someone", () => advanceToStep(ONBOARDING_STEP.ADD_CONTACT)));
    case ONBOARDING_STEP.ADD_CONTACT:
      return buildStepFrame(editedContact === null ? "Add someone" : `Edit ${editedContact.contactName}`, "", buildContactForm());
    case ONBOARDING_STEP.WAIT_FOR_CODE:
      return buildWaitStep();
    default:
      return buildStepFrame(`${contactAwaitingCode.contactName} said yes.`, "From now on, an email in their name gets checked with them before it can ask you for anything.",
        buildPrimaryButton("Open my inbox", () => onFinished()));
  }
}

function renderOnboarding() {
  pageElements.onboardingScreen.replaceChildren(buildStepElement());
  pageElements.onboardingScreen.querySelector(".onboarding-title")?.setAttribute("tabindex", "-1");
}

function isFormReplyInFlight(pushMessageType, isEditReply) {
  const isInFlight = !pageElements.onboardingScreen.hidden && activeStep === ONBOARDING_STEP.ADD_CONTACT && isFormInFlight
    && (editedContact !== null) === isEditReply;
  if (!isInFlight) console.warn(`SendPidge: ${pushMessageType} arrived with nothing in flight (reload or late reply)`);
  return isInFlight;
}

// A push missing a field is reported, and the form comes back so she can save again.
function hasContactNoteFields(pushMessage) {
  const contact = pushMessage.contact;
  const hasFields = typeof contact?.contactUserId === "string" && typeof contact.contactName === "string"
    && Array.isArray(contact.reachPoints) && typeof pushMessage.noteText === "string" && typeof pushMessage.code === "string";
  if (!hasFields) {
    console.error(`SendPidge: ${pushMessage.type} has a missing field`, Object.keys(pushMessage)); // never the note or the code
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    renderOnboarding();
  }
  return hasFields;
}

// The note to send, shown once; the wait ends when one of the numbers waiting for this code sends it back.
function showContactNote(pushMessage) {
  const { contactUserId, contactName, reachPoints } = pushMessage.contact;
  const waitingNumbers = reachPoints
    .filter((point) => point.channel === CHANNEL.WHATSAPP && point.verifiedVia === VERIFIED_VIA.PENDING).map((point) => point.address);
  contactAwaitingCode = { contactUserId, contactName, noteText: pushMessage.noteText, code: pushMessage.code, waitingNumbers };
  formDraft = emptyFormDraft();
  advanceToStep(ONBOARDING_STEP.WAIT_FOR_CODE);
}

// The contact_added reply to this socket's add_contact.
export function handleContactAddedPush(pushMessage) {
  if (!isFormReplyInFlight(pushMessage.type, false)) return;
  isFormInFlight = false;
  if (hasContactNoteFields(pushMessage)) showContactNote(pushMessage);
}

// The contact_edited reply to this socket's edit_contact: with a note (a number was added, or an expired code renewed) the
// wait step shows it; without one the edit is saved and setup closes.
export function handleContactEditedPush(pushMessage) {
  if (!isFormReplyInFlight(pushMessage.type, true)) return;
  isFormInFlight = false;
  if (pushMessage.noteText === null) onExited();
  else if (hasContactNoteFields(pushMessage)) showContactNote(pushMessage);
}

// Called on every receiver sync_state and circle_state: once a number that waited for this code is checked, the done step.
export function handleCircleSyncDuringOnboarding() {
  if (pageElements.onboardingScreen.hidden || activeStep !== ONBOARDING_STEP.WAIT_FOR_CODE || contactAwaitingCode === null) return;
  const contact = listCircleEntries().find((entry) => entry.contactUserId === contactAwaitingCode.contactUserId);
  const isWaitingNumberChecked = contact?.reachPoints.some((point) => point.channel === CHANNEL.WHATSAPP
    && contactAwaitingCode.waitingNumbers.includes(point.address) && point.verifiedVia !== VERIFIED_VIA.PENDING);
  if (isWaitingNumberChecked) {
    isVerifyInFlight = false;
    advanceToStep(ONBOARDING_STEP.DONE);
  }
}

// A refused add or edit shows why on the form (true: the rejection is consumed here); a refused demo reply re-enables the button.
export function handleOnboardingActionRejected(reason) {
  if (isFormInFlight) {
    isFormInFlight = false;
    if (reason === ACTION_REJECTED_REASON.DUPLICATE_NAME) formErrorText = FORM_ERROR_TEXT.DUPLICATE_NAME;
    else if (reason === ACTION_REJECTED_REASON.CIRCLE_FULL) formErrorText = FORM_ERROR_TEXT.CIRCLE_FULL;
    else if (reason === ACTION_REJECTED_REASON.LAST_CHECKED_NUMBER) formErrorText = FORM_ERROR_TEXT.LAST_CHECKED_NUMBER;
    else formErrorText = ACTION_STATUS_MESSAGE.GENERIC_FAILURE;
    renderOnboarding();
    return true;
  }
  if (!isVerifyInFlight) return false;
  isVerifyInFlight = false;
  renderOnboarding();
  return false;
}

// editedEntry: the circle entry for ONBOARDING_START.EDIT_CONTACT, else null.
export function startOnboarding({ startStep, editedEntry, onFinishedCallback, onExitedCallback }) {
  onFinished = onFinishedCallback;
  onExited = onExitedCallback;
  openedAtStep = startStep === ONBOARDING_START.WELCOME ? ONBOARDING_STEP.WELCOME : ONBOARDING_STEP.ADD_CONTACT;
  const wasEditing = editedContact !== null;
  editedContact = startStep === ONBOARDING_START.EDIT_CONTACT ? editedEntry : null;
  if (editedContact !== null) formDraft = formDraftFromCircleEntry(editedContact);
  else if (wasEditing) formDraft = emptyFormDraft(); // an edit's values never carry into an add; an unsent add draft is kept until an edit opens
  contactAwaitingCode = null;
  isFormInFlight = false;
  isVerifyInFlight = false;
  formErrorText = "";
  pageElements.topbarTitle.textContent = editedContact === null ? "Set up" : "Circle";
  pageElements.onboardingScreen.hidden = false;
  advanceToStep(openedAtStep);
}

// A reply that lands after this is dropped (isFormReplyInFlight), so nothing stays in flight to swallow a later rejection.
export function hideOnboarding() {
  pageElements.onboardingScreen.hidden = true;
  isFormInFlight = false;
  isVerifyInFlight = false;
}
