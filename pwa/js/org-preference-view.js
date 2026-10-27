// Grandma's home on the Circle tab (RECEIVER), from sync_state.orgPreference and every circle_state push: a radio group named for
// the home (what it may see) and one button that adds the home to her circle or removes it. Hidden when orgPreference is null.
// The checked choice and the button come from the payload; a refused or unsent action shows the payload's state again. The
// elements are built once and updated in place, so keyboard focus survives every push. The org name is server text: textContent only.
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

function renderOrgPreference() {
  orgPreferenceElements.visibilityLegend.textContent = lastOrgPreference.orgName;
  orgPreferenceElements.visibilityRadiosByValue.forEach((radio, visibility) => {
    radio.checked = visibility === lastOrgPreference.visibility;
  });
  renderOrgContactButton();
}

// The browser has already checked the chosen radio; the next push (or a refusal) sets it from the payload.
function handleUserChoseOrgVisibility(visibility) {
  clearActionStatusMessage();
  if (!sendClientAction({ action: CLIENT_ACTION.SET_ORG_VISIBILITY, visibility })) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    renderOrgPreference();
  }
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

// sync_state.orgPreference or circle_state.orgPreference: null hides the section (her org has no profile); a malformed value
// or an unknown visibility is reported and hides it too, never shown as a guessed choice.
export function applyOrgPreferenceState(orgPreference) {
  isOrgContactActionInFlight = false;
  const isOrgPreferenceShown = orgPreference !== null && isValidOrgPreference(orgPreference);
  if (!isOrgPreferenceShown) {
    if (orgPreference !== null) showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    lastOrgPreference = null;
    orgPreferenceHost.hidden = true;
    return;
  }
  lastOrgPreference = orgPreference;
  renderOrgPreference();
  orgPreferenceHost.hidden = false;
}

// Any refused receiver action: the button comes back and the radios show the payload's choice again.
export function handleOrgPreferenceActionRejected() {
  isOrgContactActionInFlight = false;
  if (lastOrgPreference !== null) renderOrgPreference();
}
