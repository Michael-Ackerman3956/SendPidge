// Grandma's home on the Circle tab (RECEIVER), from sync_state.orgPreference and every circle_state push: a radio group named for
// the home (what it may see) and one button that adds the home to her circle or removes it. Hidden when orgPreference is null.
// One set_org_visibility is in flight at a time: picks made meanwhile wait, and when it settles (any payload or refusal) only
// the newest is sent, if it differs from the payload's choice. While a send is in flight the radios keep the user's pick; once
// nothing is in flight they show the payload, as does the button, so a refused or unsent action shows the payload's state again.
// The elements are built once and updated in place, so keyboard focus survives every push. The org name is server text:
// textContent only.
import { CLIENT_ACTION, ORG_VISIBILITY } from "./constants.js";
import { createElementWithText } from "./dom-helpers.js";
import { sendClientAction } from "./websocket.js";
import { ACTION_STATUS_MESSAGE, clearActionStatusMessage, showActionStatusMessage } from "./action-status.js";

const KNOWN_ORG_VISIBILITY = new Set(Object.values(ORG_VISIBILITY));
const ORG_VISIBILITY_LABELS = Object.freeze({
  [ORG_VISIBILITY.NONE]: "Nothing",
  [ORG_VISIBILITY.ALERTS_ONLY]: "Alerts",
  [ORG_VISIBILITY.ALERTS_AND_CASES]: "Alerts and cases",
});
const VISIBILITY_RADIO_GROUP_NAME = "org-visibility";

let lastOrgPreference = null;
let isOrgContactActionInFlight = false;
let isOrgVisibilityActionInFlight = false;
let newestUnsentOrgVisibility = null; // the newest pick made while a set_org_visibility was in flight, else null

const orgPreferenceHost = document.getElementById("org-preference-host");
const orgPreferenceElements = buildOrgPreferenceElements();

function buildVisibilityOption(visibility) {
  const option = createElementWithText("label", "org-visibility-option");
  const radio = createElementWithText("input", "org-visibility-radio");
  radio.type = "radio";
  radio.name = VISIBILITY_RADIO_GROUP_NAME;
  radio.value = visibility;
  radio.addEventListener("change", () => handleUserChoseOrgVisibility(visibility));
  option.append(radio, createElementWithText("span", "org-visibility-label", ORG_VISIBILITY_LABELS[visibility]));
  return { option, radio };
}

function buildOrgPreferenceElements() {
  const visibilityGroup = createElementWithText("fieldset", "org-visibility-group");
  const visibilityLegend = createElementWithText("legend", "section-header org-visibility-legend");
  const visibilityOptions = createElementWithText("div", "org-visibility-options");
  const visibilityRadiosByValue = new Map();
  Object.keys(ORG_VISIBILITY_LABELS).forEach((visibility) => {
    const { option, radio } = buildVisibilityOption(visibility);
    visibilityOptions.append(option);
    visibilityRadiosByValue.set(visibility, radio);
  });
  visibilityGroup.append(visibilityLegend, visibilityOptions);
  const orgContactButton = createElementWithText("button", "contact-secondary-button org-contact-button");
  orgContactButton.type = "button";
  orgContactButton.addEventListener("click", handleUserTappedOrgContactButton);
  orgPreferenceHost.append(visibilityGroup, orgContactButton);
  return { visibilityLegend, visibilityRadiosByValue, orgContactButton };
}

function isValidOrgPreference(orgPreference) {
  const isValid = orgPreference !== null && typeof orgPreference === "object" && typeof orgPreference.orgName === "string"
    && KNOWN_ORG_VISIBILITY.has(orgPreference.visibility) && typeof orgPreference.isOrgContact === "boolean";
  if (!isValid) console.error("SendPidge: orgPreference has a missing field or unknown visibility", orgPreference);
  return isValid;
}

// aria-disabled (not disabled) while an add or remove is in flight, so the focused button keeps keyboard focus.
function renderOrgContactButton() {
  const { orgName, isOrgContact } = lastOrgPreference;
  const button = orgPreferenceElements.orgContactButton;
  button.textContent = isOrgContact ? "Remove" : `Add ${orgName}`;
  button.setAttribute("aria-label", isOrgContact ? `Remove ${orgName}` : `Add ${orgName}`);
  button.setAttribute("aria-disabled", String(isOrgContactActionInFlight));
}

// The radios are left as the user picked them while a send is in flight; the payload sets them once it settles.
function renderOrgPreference() {
  orgPreferenceElements.visibilityLegend.textContent = lastOrgPreference.orgName;
  if (!isOrgVisibilityActionInFlight) {
    orgPreferenceElements.visibilityRadiosByValue.forEach((radio, visibility) => {
      radio.checked = visibility === lastOrgPreference.visibility;
    });
  }
  renderOrgContactButton();
}

// A send that fails (socket not open) shows the failure, and nothing is in flight, so the next render shows the payload.
function sendOrgVisibility(visibility) {
  isOrgVisibilityActionInFlight = sendClientAction({ action: CLIENT_ACTION.SET_ORG_VISIBILITY, visibility });
  if (!isOrgVisibilityActionInFlight) showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
}

// The browser has already checked the chosen radio. While a send is in flight the pick waits; a newer pick replaces it.
function handleUserChoseOrgVisibility(visibility) {
  clearActionStatusMessage();
  if (isOrgVisibilityActionInFlight) {
    newestUnsentOrgVisibility = visibility;
    return;
  }
  sendOrgVisibility(visibility);
  if (!isOrgVisibilityActionInFlight) renderOrgPreference();
}

// A payload or a refusal settles the send in flight: the waiting pick is sent when it differs from the payload's choice, else
// dropped. A pick already sent is never sent again here, so a refused choice is not retried.
function settleOrgVisibilityAction() {
  const waitingOrgVisibility = newestUnsentOrgVisibility;
  isOrgVisibilityActionInFlight = false;
  newestUnsentOrgVisibility = null;
  if (waitingOrgVisibility !== null && waitingOrgVisibility !== lastOrgPreference.visibility) sendOrgVisibility(waitingOrgVisibility);
}

function handleUserTappedOrgContactButton() {
  if (isOrgContactActionInFlight || lastOrgPreference === null) return;
  clearActionStatusMessage();
  const clientAction = lastOrgPreference.isOrgContact ? CLIENT_ACTION.REMOVE_ORG_CONTACT : CLIENT_ACTION.ADD_ORG_CONTACT;
  if (!sendClientAction({ action: clientAction })) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  isOrgContactActionInFlight = true;
  renderOrgContactButton();
}

// sync_state.orgPreference or circle_state.orgPreference: null hides the section (her org has no profile) and drops any
// waiting pick; a malformed value or an unknown visibility is reported and hides it too, never shown as a guessed choice.
export function applyOrgPreferenceState(orgPreference) {
  isOrgContactActionInFlight = false;
  const isOrgPreferenceShown = orgPreference !== null && isValidOrgPreference(orgPreference);
  if (!isOrgPreferenceShown) {
    if (orgPreference !== null) showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    lastOrgPreference = null;
    isOrgVisibilityActionInFlight = false;
    newestUnsentOrgVisibility = null;
    orgPreferenceHost.hidden = true;
    return;
  }
  lastOrgPreference = orgPreference;
  settleOrgVisibilityAction();
  renderOrgPreference();
  orgPreferenceHost.hidden = false;
}

// Any refused receiver action (it names no action): the button comes back and the visibility send in flight is settled, so
// the radios show the payload's choice again unless a newer waiting pick is sent.
export function handleOrgPreferenceActionRejected() {
  isOrgContactActionInFlight = false;
  if (lastOrgPreference === null) return;
  settleOrgVisibilityAction();
  renderOrgPreference();
}
