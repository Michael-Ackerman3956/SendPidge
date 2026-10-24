// Grandma's verdict visuals: one display state per email, the row mark, the detail banner and the red denied-verdict overlay.
// contactName and every email field are attacker-influenced: rendered only through textContent.
import { EMAIL_DISPLAY_STATE, POLICY_DECISION, REQUEST_STATUS } from "./constants.js";
import { buildPidgeIconElement, buildTickIcon, createElementWithText } from "./dom-helpers.js";
import { buildWarnBannerElement, replaceActiveOverlay } from "./pidge-overlay.js";

// Priority: quarantined > denied > confirmed > released by you > awaiting verdict > flagged > plain.
// DENIED outranks a local release so Jimin's No still reaches Grandma after she cancelled the hold.
export function deriveEmailDisplayState(email, isReleasedByUser) {
  const request = email.request;
  if (email.quarantined) return EMAIL_DISPLAY_STATE.QUARANTINED;
  if (request !== null && request.status === REQUEST_STATUS.DENIED) return EMAIL_DISPLAY_STATE.DENIED;
  if (request !== null && request.status === REQUEST_STATUS.CONFIRMED) return EMAIL_DISPLAY_STATE.CONFIRMED;
  if (isReleasedByUser) return EMAIL_DISPLAY_STATE.RELEASED_BY_USER;
  if (email.state === POLICY_DECISION.HOLD && request !== null) return EMAIL_DISPLAY_STATE.AWAITING_VERDICT;
  if (email.state === POLICY_DECISION.HOLD || email.state === POLICY_DECISION.WARN) return EMAIL_DISPLAY_STATE.FLAGGED;
  return EMAIL_DISPLAY_STATE.PLAIN;
}

function buildFlagMarkElement(isThreat) {
  const flagMark = createElementWithText("span", isThreat ? "row-flag-mark is-threat" : "row-flag-mark", "!");
  flagMark.setAttribute("role", "img");
  flagMark.setAttribute("aria-label", isThreat ? "Scam" : "Flagged");
  return flagMark;
}

function buildVerifiedTickMarkElement() {
  const tickMark = createElementWithText("span", "row-verified-mark");
  tickMark.setAttribute("role", "img");
  tickMark.setAttribute("aria-label", "Verified");
  tickMark.append(buildTickIcon("tick-icon"));
  return tickMark;
}

// The small mark at the end of an inbox row, or null for a plain row.
export function buildRowStatusElement(emailDisplayState) {
  switch (emailDisplayState) {
    case EMAIL_DISPLAY_STATE.QUARANTINED:
    case EMAIL_DISPLAY_STATE.DENIED:
      return buildFlagMarkElement(true);
    case EMAIL_DISPLAY_STATE.CONFIRMED:
      return buildVerifiedTickMarkElement();
    case EMAIL_DISPLAY_STATE.RELEASED_BY_USER:
      return createElementWithText("span", "row-released-tag", "Released by you");
    case EMAIL_DISPLAY_STATE.AWAITING_VERDICT:
    case EMAIL_DISPLAY_STATE.FLAGGED:
      return buildFlagMarkElement(false);
    default:
      return null;
  }
}

function buildVerifiedBannerElement(email, shouldCelebrate) {
  const bannerElement = createElementWithText("div", shouldCelebrate ? "verified-banner is-celebrating" : "verified-banner");
  bannerElement.setAttribute("role", "status");
  const iconGroup = createElementWithText("span", "verified-icon-group");
  iconGroup.append(buildPidgeIconElement("verified-pidge", ""), buildTickIcon("tick-icon verified-tick"));
  bannerElement.append(iconGroup, createElementWithText("span", "verified-text", `${email.request.contactName} confirmed they sent this.`));
  return bannerElement;
}

// The banner at the top of the detail, or null when the state has none.
export function buildDetailBannerElement(email, emailDisplayState, shouldCelebrate) {
  if (emailDisplayState === EMAIL_DISPLAY_STATE.QUARANTINED) {
    const quarantineBanner = createElementWithText("p", "threat-banner", "Reported and quarantined.");
    quarantineBanner.setAttribute("role", "status");
    return quarantineBanner;
  }
  if (emailDisplayState === EMAIL_DISPLAY_STATE.CONFIRMED) {
    return buildVerifiedBannerElement(email, shouldCelebrate);
  }
  if (email.state === POLICY_DECISION.WARN) {
    return buildWarnBannerElement(email);
  }
  return null;
}

function buildConfirmReportButton(isReporting, onConfirmTapped) {
  const confirmButton = createElementWithText("button", "confirm-report-button", isReporting ? "Reporting…" : "Report and block");
  confirmButton.type = "button";
  confirmButton.disabled = isReporting;
  confirmButton.addEventListener("click", onConfirmTapped);
  return confirmButton;
}

// Red verdict over the detail after the real contact said No. Report and block runs onConfirmTapped; while isReporting it is disabled.
export function showDeniedVerdictOverlay({ overlayHostElement, email, isReporting, onConfirmTapped }) {
  const overlayElement = createElementWithText("div", "hold-overlay is-denied");
  overlayElement.setAttribute("role", "alertdialog");
  overlayElement.setAttribute("aria-modal", "true");
  overlayElement.setAttribute("aria-labelledby", "denied-verdict-message");
  const messageElement = createElementWithText("p", "hold-overlay-message denied-verdict-message",
    `Real ${email.request.contactName} replied: they did not send this. Do not send money.`);
  messageElement.id = "denied-verdict-message";
  const confirmButton = buildConfirmReportButton(isReporting, () => onConfirmTapped(email));
  overlayElement.append(buildPidgeIconElement("hold-overlay-pidge", "Pidge"), messageElement, confirmButton);
  replaceActiveOverlay(() => overlayElement.remove());
  overlayHostElement.append(overlayElement);
  if (isReporting) {
    messageElement.tabIndex = -1;
    messageElement.focus();
  } else {
    confirmButton.focus();
  }
}
