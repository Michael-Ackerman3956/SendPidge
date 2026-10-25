// Onboarding (RECEIVER): the prototype's five steps. The code step is a demo for Oct 28: nothing is sent to anyone; a labelled
// button plays the contact's reply and the server (mock mode) marks the seeded contact verified. A wrong code writes nothing.
import { CLIENT_ACTION, VERIFIED_VIA } from "./constants.js";
import { buildPidgeIconElement, buildTickIcon, createElementWithText } from "./dom-helpers.js";
import { listCircleEntries } from "./circle-view.js";
import { sendClientAction } from "./websocket.js";
import { ACTION_STATUS_MESSAGE, clearActionStatusMessage, showActionStatusMessage } from "./action-status.js";

const DEMO_CONTACT_CODE = "4821"; // CONTACT_VERIFICATION_MOCK_CODE on the server; a real code would travel over RCS or the app
const STEP_COUNT = 5;

const ONBOARDING_STEP = Object.freeze({
  WELCOME: 0,
  WHO_TO_ASK: 1,
  PICK_CIRCLE: 2,
  WAIT_FOR_CODE: 3,
  DONE: 4,
});

// Where onboarding opens: the first run from the welcome, "Add someone" straight at the circle pick.
export const ONBOARDING_START = Object.freeze({
  WELCOME: "welcome",
  PICK_CIRCLE: "pick_circle",
});

let activeStep = ONBOARDING_STEP.WELCOME;
let pickedContactUserId = null;
let isVerifyInFlight = false;
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

function pickedContact() {
  return listCircleEntries().find((entry) => entry.contactUserId === pickedContactUserId) ?? null;
}

function buildPickList() {
  const list = createElementWithText("div", "onboarding-pick-list");
  listCircleEntries().forEach((entry) => {
    const row = createElementWithText("button", entry.contactUserId === pickedContactUserId ? "onboarding-pick-row is-on" : "onboarding-pick-row");
    row.type = "button";
    row.setAttribute("aria-pressed", String(entry.contactUserId === pickedContactUserId));
    const avatar = createElementWithText("span", "avatar-initial", (entry.contactName.trim()[0] || "?").toUpperCase());
    avatar.setAttribute("aria-hidden", "true");
    const box = createElementWithText("span", "onboarding-pick-box");
    if (entry.contactUserId === pickedContactUserId) box.append(buildTickIcon("tick-icon"));
    row.append(avatar, createElementWithText("span", "onboarding-pick-name", entry.contactName), box);
    row.addEventListener("click", () => { pickedContactUserId = entry.contactUserId; renderOnboarding(); });
    list.append(row);
  });
  return list;
}

function handleUserPlayedContactReply() {
  clearActionStatusMessage();
  if (!sendClientAction({ action: CLIENT_ACTION.VERIFY_CONTACT, contactUserId: pickedContactUserId, code: DEMO_CONTACT_CODE })) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  isVerifyInFlight = true;
  renderOnboarding();
}

function buildWaitStep(contact) {
  const codePreview = createElementWithText("div", "onboarding-code-preview");
  codePreview.append(createElementWithText("p", "onboarding-code-to", `To ${contact.contactName}`),
    createElementWithText("p", "", "Grandma is using Pidge to catch fake emails. If something arrives in your name, Pidge will ask you first. Reply with your code: "),
    createElementWithText("b", "", DEMO_CONTACT_CODE));
  const waitRow = createElementWithText("p", "onboarding-wait-row");
  waitRow.append(createElementWithText("span", "checking-pulse"), createElementWithText("span", "", `Waiting for ${contact.contactName}`));
  const demoButton = createElementWithText("button", "contact-secondary-button", isVerifyInFlight ? "Sending…" : `Demo: ${contact.contactName} replies ${DEMO_CONTACT_CODE}`);
  demoButton.type = "button";
  demoButton.disabled = isVerifyInFlight;
  demoButton.addEventListener("click", handleUserPlayedContactReply);
  return buildStepFrame(`Asking ${contact.contactName}`, "A code goes to their number. They reply with it, so a wrong number can never answer for them.",
    codePreview, waitRow, createElementWithText("p", "onboarding-demo-note", "Demo: nothing is sent. The reply is played by the button."), demoButton);
}

function buildStepElement() {
  switch (activeStep) {
    case ONBOARDING_STEP.WELCOME:
      return buildStepFrame("Pidge asks the real person.", "When an email says it's from someone in your circle and asks for money, Pidge checks with them before you act.",
        buildPrimaryButton("Get started", () => advanceToStep(ONBOARDING_STEP.WHO_TO_ASK)));
    case ONBOARDING_STEP.WHO_TO_ASK:
      return buildStepFrame("Who should Pidge ask?", "Pick the people who write to you. Pidge only ever contacts them.",
        buildPrimaryButton("Choose from your circle", () => advanceToStep(ONBOARDING_STEP.PICK_CIRCLE)));
    case ONBOARDING_STEP.PICK_CIRCLE: {
      const nextButton = buildPrimaryButton("Send them a note", () => advanceToStep(ONBOARDING_STEP.WAIT_FOR_CODE));
      nextButton.disabled = pickedContact() === null;
      return buildStepFrame("Your circle", "Pidge will ask these people when an email claims to be them.", buildPickList(), nextButton);
    }
    case ONBOARDING_STEP.WAIT_FOR_CODE:
      return buildWaitStep(pickedContact());
    default: {
      const contact = pickedContact();
      return buildStepFrame(`${contact.contactName} said yes.`, "From now on, an email in their name gets checked with them before it can ask you for anything.",
        buildPrimaryButton("Open my inbox", () => onFinished()));
    }
  }
}

function renderOnboarding() {
  pageElements.onboardingScreen.replaceChildren(buildStepElement());
  pageElements.onboardingScreen.querySelector(".onboarding-title")?.setAttribute("tabindex", "-1");
}

// Called on every receiver sync_state: a verified pick while waiting moves to the done step.
export function handleCircleSyncDuringOnboarding() {
  if (pageElements.onboardingScreen.hidden || activeStep !== ONBOARDING_STEP.WAIT_FOR_CODE || !isVerifyInFlight) return;
  const contact = pickedContact();
  if (contact !== null && contact.verifiedVia === VERIFIED_VIA.MOCK) {
    isVerifyInFlight = false;
    advanceToStep(ONBOARDING_STEP.DONE);
  }
}

// A refused verify (wrong code, mock off) re-enables the demo button.
export function handleOnboardingActionRejected() {
  if (!isVerifyInFlight) return;
  isVerifyInFlight = false;
  renderOnboarding();
}

export function startOnboarding({ startStep, onFinishedCallback }) {
  onFinished = onFinishedCallback;
  activeStep = startStep === ONBOARDING_START.PICK_CIRCLE ? ONBOARDING_STEP.PICK_CIRCLE : ONBOARDING_STEP.WELCOME;
  pickedContactUserId = listCircleEntries()[0]?.contactUserId ?? null;
  isVerifyInFlight = false;
  pageElements.topbarTitle.textContent = "Set up";
  pageElements.onboardingScreen.hidden = false;
  renderOnboarding();
}

export function hideOnboarding() {
  pageElements.onboardingScreen.hidden = true;
}
