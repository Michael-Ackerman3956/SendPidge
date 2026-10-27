// Onboarding (RECEIVER): welcome → who to ask → the Add-someone form → send them the note → done. The server writes the
// contact PENDING and sends back the note (code + the contact's own Pidge link) once, to this socket only; Grandma sends it
// from her own phone. The row turns verified when the code comes back from the contact's WhatsApp number, or, with the
// server's mock flag on, when the labelled demo button plays that reply. Nothing typed here is ever shown to anyone else.
import { ACTION_REJECTED_REASON, CLIENT_ACTION, RELATIONSHIP, RELATIONSHIP_LABELS, VERIFIED_VIA } from "./constants.js";
import { buildCopyButton, buildPidgeIconElement, createElementWithText } from "./dom-helpers.js";
import { getContactVerificationFlags, listCircleEntries } from "./circle-view.js";
import { sendClientAction } from "./websocket.js";
import { ACTION_STATUS_MESSAGE, clearActionStatusMessage, showActionStatusMessage } from "./action-status.js";

const STEP_COUNT = 5;
const CONTACT_NAME_MAXIMUM_LENGTH = 40;
const E164_PATTERN = /^\+[1-9]\d{6,14}$/;
const EMAIL_ADDRESS_PATTERN = /^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$/;
const EXTRA_ADDRESSES_MAXIMUM_COUNT = 5;

const ONBOARDING_STEP = Object.freeze({
  WELCOME: 0,
  WHO_TO_ASK: 1,
  ADD_CONTACT: 2,
  WAIT_FOR_CODE: 3,
  DONE: 4,
});

// Where onboarding opens: the first run from the welcome, "Add someone" straight at the form.
export const ONBOARDING_START = Object.freeze({
  WELCOME: "welcome",
  ADD_CONTACT: "add_contact",
});

const FORM_ERROR_TEXT = Object.freeze({
  NAME: "Enter their name (up to 40 letters).",
  WHATSAPP: "Enter their WhatsApp number with the country code, like +82 10 1234 5678.",
  EMAIL: "Enter the email address they write from.",
  EXTRA: "Up to 5 other addresses, one per line.",
  DUPLICATE_NAME: "Someone with that name is already in your circle.",
  CIRCLE_FULL: "Your circle is full.",
});

let activeStep = ONBOARDING_STEP.WELCOME;
let addedContact = null; // {contactUserId, contactName, noteText, code} from the contact_added push
let isAddInFlight = false;
let isVerifyInFlight = false;
let formErrorText = "";
function emptyFormDraft() {
  return { contactName: "", relationship: RELATIONSHIP.GRANDSON, whatsappNumber: "", emailAddress: "", extraAddressesText: "" };
}

let formDraft = emptyFormDraft();
let onFinished = () => {};

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

function buildStepFrame(titleText, subtitleText, ...bodyChildren) {
  const frame = createElementWithText("div", "onboarding-step");
  const hero = createElementWithText("div", "onboarding-hero");
  hero.append(buildPidgeIconElement("onboarding-pidge", ""));
  frame.append(hero, createElementWithText("h2", "onboarding-title", titleText));
  if (subtitleText) frame.append(createElementWithText("p", "onboarding-subtitle", subtitleText));
  frame.append(...bodyChildren, createElementWithText("div", "onboarding-grow"), buildStepDots());
  return frame;
}

function advanceToStep(step) {
  activeStep = step;
  renderOnboarding();
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

function buildRelationshipSelect() {
  const select = document.createElement("select");
  select.name = "relationship";
  select.className = "onboarding-input";
  Object.values(RELATIONSHIP).forEach((relationship) => {
    const option = createElementWithText("option", "", RELATIONSHIP_LABELS[relationship]);
    option.value = relationship;
    option.selected = relationship === formDraft.relationship;
    select.append(option);
  });
  select.addEventListener("change", () => { formDraft.relationship = select.value; });
  return select;
}

function buildExtraAddressesInput() {
  const textarea = document.createElement("textarea");
  textarea.name = "extraAddressesText";
  textarea.className = "onboarding-input";
  textarea.rows = 2;
  textarea.placeholder = "Optional, one per line";
  textarea.value = formDraft.extraAddressesText;
  textarea.addEventListener("input", () => { formDraft.extraAddressesText = textarea.value; });
  return textarea;
}

function normalizeWhatsAppNumber(typedNumber) {
  return typedNumber.replace(/[\s().-]/g, "");
}

function parseExtraAddresses(typedText) {
  return typedText.split(/[\n,]/).map((address) => address.trim().toLowerCase()).filter((address) => address !== "");
}

// Mirrors shared/circle.py's parse_contact_form; the server is the judge, this only saves a round trip.
function validateFormDraft() {
  const contactName = formDraft.contactName.trim().replace(/\s+/g, " ");
  if (contactName === "" || contactName.length > CONTACT_NAME_MAXIMUM_LENGTH) return { errorText: FORM_ERROR_TEXT.NAME };
  const whatsappNumber = normalizeWhatsAppNumber(formDraft.whatsappNumber);
  if (!E164_PATTERN.test(whatsappNumber)) return { errorText: FORM_ERROR_TEXT.WHATSAPP };
  const emailAddress = formDraft.emailAddress.trim().toLowerCase();
  if (!EMAIL_ADDRESS_PATTERN.test(emailAddress)) return { errorText: FORM_ERROR_TEXT.EMAIL };
  const extraAddresses = parseExtraAddresses(formDraft.extraAddressesText);
  if (extraAddresses.length > EXTRA_ADDRESSES_MAXIMUM_COUNT || !extraAddresses.every((address) => EMAIL_ADDRESS_PATTERN.test(address))) {
    return { errorText: FORM_ERROR_TEXT.EXTRA };
  }
  return { form: { contactName, relationship: formDraft.relationship, whatsappNumber, emailAddress, extraAddresses } };
}

function handleUserSubmittedContactForm() {
  clearActionStatusMessage();
  const { errorText, form } = validateFormDraft();
  if (errorText) { formErrorText = errorText; renderOnboarding(); return; }
  if (!sendClientAction({ action: CLIENT_ACTION.ADD_CONTACT, ...form })) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  formErrorText = "";
  isAddInFlight = true;
  renderOnboarding();
}

// Chrome on Android has the Contact Picker API; everywhere else the form is typed (spec: manual entry fallback).
async function handleUserChoseFromContacts() {
  try {
    const [picked] = await navigator.contacts.select(["name", "email", "tel"], { multiple: false });
    if (!picked) return;
    if (typeof picked.name?.[0] === "string") formDraft.contactName = picked.name[0];
    if (typeof picked.email?.[0] === "string") formDraft.emailAddress = picked.email[0];
    if (typeof picked.tel?.[0] === "string") formDraft.whatsappNumber = picked.tel[0];
    renderOnboarding();
  } catch (error) {
    console.warn("SendPidge: the contact picker was closed or refused", error);
  }
}

function buildContactForm() {
  const form = createElementWithText("div", "onboarding-form");
  if (typeof navigator.contacts?.select === "function") form.append(buildSecondaryButton("Choose from contacts", handleUserChoseFromContacts));
  form.append(
    buildField("Name", buildTextInput("contactName", "text", "name", "")),
    buildField("Who they are", buildRelationshipSelect()),
    buildField("WhatsApp number", buildTextInput("whatsappNumber", "tel", "tel", "+82 10 1234 5678")),
    buildField("Email they write from", buildTextInput("emailAddress", "email", "email", "")),
    buildField("Other addresses", buildExtraAddressesInput()),
  );
  if (formErrorText) form.append(createElementWithText("p", "onboarding-form-error", formErrorText));
  const submitButton = buildPrimaryButton(isAddInFlight ? "Adding…" : "Add to my circle", handleUserSubmittedContactForm);
  submitButton.disabled = isAddInFlight;
  form.append(submitButton);
  return form;
}

// ---- the note and the wait ----

function handleUserPlayedContactReply() {
  clearActionStatusMessage();
  if (!sendClientAction({ action: CLIENT_ACTION.VERIFY_CONTACT, contactUserId: addedContact.contactUserId, code: addedContact.code })) {
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
  notePreview.append(createElementWithText("p", "onboarding-code-to", `To ${addedContact.contactName}`),
    createElementWithText("p", "onboarding-note-text", addedContact.noteText));
  const noteActions = createElementWithText("div", "onboarding-note-actions");
  if (typeof navigator.share === "function") noteActions.append(buildShareButton(addedContact.noteText));
  noteActions.append(buildCopyButton(addedContact.noteText));
  const waitRow = createElementWithText("p", "onboarding-wait-row");
  waitRow.append(createElementWithText("span", "checking-pulse"), createElementWithText("span", "", `Waiting for ${addedContact.contactName}`));
  const children = [notePreview, noteActions, waitRow];
  if (getContactVerificationFlags().mock) {
    const demoButton = buildSecondaryButton(isVerifyInFlight ? "Sending…" : `Demo: ${addedContact.contactName} replies ${addedContact.code}`, handleUserPlayedContactReply);
    demoButton.disabled = isVerifyInFlight;
    children.push(createElementWithText("p", "onboarding-demo-note", "Demo: nothing is sent. The reply is played by the button."), demoButton);
  }
  const subtitleText = getContactVerificationFlags().whatsapp
    ? "Send it from your own phone. When they reply to Pidge with the code, they are in."
    : "Send it from your own phone. This stack cannot take their reply on WhatsApp yet.";
  return buildStepFrame(`Send ${addedContact.contactName} this note`, subtitleText, ...children);
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
      return buildStepFrame("Add someone", "", buildContactForm());
    case ONBOARDING_STEP.WAIT_FOR_CODE:
      return buildWaitStep();
    default:
      return buildStepFrame(`${addedContact.contactName} said yes.`, "From now on, an email in their name gets checked with them before it can ask you for anything.",
        buildPrimaryButton("Open my inbox", () => onFinished()));
  }
}

function renderOnboarding() {
  pageElements.onboardingScreen.replaceChildren(buildStepElement());
  pageElements.onboardingScreen.querySelector(".onboarding-title")?.setAttribute("tabindex", "-1");
}

// The contact_added reply to this socket's add_contact: the note to send, shown once.
export function handleContactAddedPush(pushMessage) {
  if (pageElements.onboardingScreen.hidden || activeStep !== ONBOARDING_STEP.ADD_CONTACT || !isAddInFlight) {
    console.warn("SendPidge: contact_added arrived with no add in flight (reload or late reply); add the person again for a new note");
    return;
  }
  const contact = pushMessage.contact;
  if (typeof contact?.contactUserId !== "string" || typeof contact.contactName !== "string"
    || typeof pushMessage.noteText !== "string" || typeof pushMessage.code !== "string") {
    console.error("SendPidge: contact_added has a missing field", Object.keys(pushMessage)); // never the note or the code
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  isAddInFlight = false;
  addedContact = { contactUserId: contact.contactUserId, contactName: contact.contactName, noteText: pushMessage.noteText, code: pushMessage.code };
  formDraft = emptyFormDraft();
  advanceToStep(ONBOARDING_STEP.WAIT_FOR_CODE);
}

// Called on every receiver sync_state and circle_state: the added contact no longer pending moves to the done step.
export function handleCircleSyncDuringOnboarding() {
  if (pageElements.onboardingScreen.hidden || activeStep !== ONBOARDING_STEP.WAIT_FOR_CODE || addedContact === null) return;
  const contact = listCircleEntries().find((entry) => entry.contactUserId === addedContact.contactUserId);
  if (contact !== undefined && contact.verifiedVia !== VERIFIED_VIA.PENDING) {
    isVerifyInFlight = false;
    advanceToStep(ONBOARDING_STEP.DONE);
  }
}

// A refused add shows why on the form (true: the rejection is consumed here); a refused demo reply re-enables the button.
export function handleOnboardingActionRejected(reason) {
  if (isAddInFlight) {
    isAddInFlight = false;
    if (reason === ACTION_REJECTED_REASON.DUPLICATE_NAME) formErrorText = FORM_ERROR_TEXT.DUPLICATE_NAME;
    else if (reason === ACTION_REJECTED_REASON.CIRCLE_FULL) formErrorText = FORM_ERROR_TEXT.CIRCLE_FULL;
    else formErrorText = ACTION_STATUS_MESSAGE.GENERIC_FAILURE;
    renderOnboarding();
    return true;
  }
  if (!isVerifyInFlight) return false;
  isVerifyInFlight = false;
  renderOnboarding();
  return false;
}

export function startOnboarding({ startStep, onFinishedCallback }) {
  onFinished = onFinishedCallback;
  activeStep = startStep === ONBOARDING_START.ADD_CONTACT ? ONBOARDING_STEP.ADD_CONTACT : ONBOARDING_STEP.WELCOME;
  addedContact = null;
  isAddInFlight = false;
  isVerifyInFlight = false;
  formErrorText = "";
  pageElements.topbarTitle.textContent = "Set up";
  pageElements.onboardingScreen.hidden = false;
  renderOnboarding();
}

export function hideOnboarding() {
  pageElements.onboardingScreen.hidden = true;
}
