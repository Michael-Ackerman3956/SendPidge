// Pidge hold overlay (countdown, 5-second long-press cancel, shield on timeout) and the calm WARN banner.
// All email fields are attacker-controlled: rendered only through textContent.
import { INTENT, WARN_REASON } from "./constants.js";

const CANCEL_LONG_PRESS_DURATION_MS = 5000;
const COUNTDOWN_TICK_INTERVAL_MS = 1000;
const PIDGE_ICON_PATH = "assets/pidge-icon.png";

const INTENT_PHRASES = Object.freeze({
  [INTENT.MONEY]: "money",
  [INTENT.CODE]: "a code",
  [INTENT.URGENT_ACTION]: "something urgent",
});

let activeOverlayTeardown = null;

function createElementWithText(tagName, className, textContent) {
  const element = document.createElement(tagName);
  if (className) {
    element.className = className;
  }
  if (textContent !== undefined) {
    element.textContent = textContent;
  }
  return element;
}

function describeIntentForHoldMessage(intent) {
  const intentPhrase = INTENT_PHRASES[intent];
  if (intentPhrase === undefined) {
    console.error("SendPidge: hold request has an unknown intent", intent);
    return "something";
  }
  return intentPhrase;
}

function formatRemainingTimeAsMinutesSeconds(remainingMilliseconds) {
  const totalSeconds = Math.max(0, Math.ceil(remainingMilliseconds / 1000));
  const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, "0");
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function parseExpiryTimeOrReportError(expiresAtIsoText) {
  const expiresAtMilliseconds = Date.parse(expiresAtIsoText);
  if (Number.isNaN(expiresAtMilliseconds)) {
    console.error("SendPidge: hold request has an unreadable expiresAt; showing it as expired", expiresAtIsoText);
    return Date.now();
  }
  return expiresAtMilliseconds;
}

function buildShieldIcon() {
  const svgNamespace = "http://www.w3.org/2000/svg";
  const shieldSvg = document.createElementNS(svgNamespace, "svg");
  shieldSvg.setAttribute("viewBox", "0 0 24 24");
  shieldSvg.setAttribute("aria-hidden", "true");
  shieldSvg.setAttribute("class", "shield-icon");
  const shieldPath = document.createElementNS(svgNamespace, "path");
  shieldPath.setAttribute("d", "M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5l-8-3Z");
  shieldSvg.append(shieldPath);
  return shieldSvg;
}

// Starts the countdown to expiresAt. At 0 the checking line becomes the shield state.
function startHoldCountdown({ countdownElement, checkingLineElement, statusRegionElement, contactName, expiresAtIsoText }) {
  const expiresAtMilliseconds = parseExpiryTimeOrReportError(expiresAtIsoText);
  let countdownIntervalId = null;

  function showShieldTimeoutState() {
    checkingLineElement.replaceChildren(buildShieldIcon());
    statusRegionElement.textContent = `${contactName} hasn't answered. The email stays held.`;
    countdownElement.hidden = true;
  }

  function updateCountdown() {
    const remainingMilliseconds = expiresAtMilliseconds - Date.now();
    if (remainingMilliseconds <= 0) {
      clearInterval(countdownIntervalId);
      showShieldTimeoutState();
      return;
    }
    countdownElement.textContent = formatRemainingTimeAsMinutesSeconds(remainingMilliseconds);
  }

  countdownIntervalId = setInterval(updateCountdown, COUNTDOWN_TICK_INTERVAL_MS);
  updateCountdown();
  return () => clearInterval(countdownIntervalId);
}

// The cancel button fires onLongPressCompleted only after a continuous 5-second press (pointer or Space/Enter).
function wireCancelLongPress(cancelButton, onLongPressCompleted) {
  let longPressTimeoutId = null;

  function beginPress(event) {
    if (longPressTimeoutId !== null) {
      return;
    }
    event.preventDefault();
    cancelButton.classList.add("is-pressing");
    longPressTimeoutId = setTimeout(() => {
      longPressTimeoutId = null;
      cancelButton.classList.remove("is-pressing");
      onLongPressCompleted();
    }, CANCEL_LONG_PRESS_DURATION_MS);
  }

  function abortPress() {
    clearTimeout(longPressTimeoutId);
    longPressTimeoutId = null;
    cancelButton.classList.remove("is-pressing");
  }

  cancelButton.addEventListener("pointerdown", beginPress);
  ["pointerup", "pointerleave", "pointercancel", "blur"].forEach((eventName) => cancelButton.addEventListener(eventName, abortPress));
  cancelButton.addEventListener("contextmenu", (event) => event.preventDefault());
  cancelButton.addEventListener("keydown", (event) => {
    if ((event.key === " " || event.key === "Enter") && !event.repeat) {
      beginPress(event);
    }
  });
  cancelButton.addEventListener("keyup", (event) => {
    if (event.key === " " || event.key === "Enter") {
      abortPress();
    }
  });
  return abortPress;
}

function buildCancelButton() {
  const cancelButton = createElementWithText("button", "cancel-long-press-button");
  cancelButton.type = "button";
  cancelButton.append(createElementWithText("span", "cancel-fill"), createElementWithText("span", "cancel-label", "Hold to cancel"));
  return cancelButton;
}

export function closeHoldOverlay() {
  if (activeOverlayTeardown !== null) {
    activeOverlayTeardown();
    activeOverlayTeardown = null;
  }
}

// Shows the hold overlay inside overlayHostElement for one held email. onReleasedByUser runs after the 5-second press.
export function showHoldOverlay({ overlayHostElement, email, onReleasedByUser }) {
  closeHoldOverlay();
  const { contactName, intent, expiresAt } = email.request;
  const overlayElement = createElementWithText("div", "hold-overlay");
  overlayElement.setAttribute("role", "alertdialog");
  overlayElement.setAttribute("aria-modal", "true");
  overlayElement.setAttribute("aria-labelledby", "hold-overlay-message");

  const pidgeIcon = createElementWithText("img", "hold-overlay-pidge");
  pidgeIcon.src = PIDGE_ICON_PATH;
  pidgeIcon.alt = "Pidge";
  const messageElement = createElementWithText("p", "hold-overlay-message",
    `This email says it is from ${contactName} and asks for ${describeIntentForHoldMessage(intent)}. I will check with ${contactName}.`);
  messageElement.id = "hold-overlay-message";
  const checkingLineElement = createElementWithText("div", "hold-overlay-checking");
  checkingLineElement.append(createElementWithText("span", "checking-pulse"));
  const statusRegionElement = createElementWithText("p", "hold-overlay-status", `Pidge is checking with ${contactName}`);
  statusRegionElement.setAttribute("aria-live", "polite");
  const countdownElement = createElementWithText("p", "hold-overlay-countdown");
  countdownElement.setAttribute("role", "timer");
  const cancelButton = buildCancelButton();

  overlayElement.append(pidgeIcon, messageElement, checkingLineElement, statusRegionElement, countdownElement, cancelButton);
  overlayHostElement.append(overlayElement);

  const stopCountdown = startHoldCountdown({ countdownElement, checkingLineElement, statusRegionElement, contactName, expiresAtIsoText: expiresAt });
  const abortCancelPress = wireCancelLongPress(cancelButton, () => {
    closeHoldOverlay();
    onReleasedByUser(email.emailId);
  });
  activeOverlayTeardown = () => {
    stopCountdown();
    abortCancelPress();
    overlayElement.remove();
  };
  cancelButton.focus();
}

function describeWarnReason(email) {
  switch (email.warnReason) {
    case WARN_REASON.UNREADABLE:
      return "Pidge couldn't check this one.";
    case WARN_REASON.UNKNOWN_NAME:
      return "This email asks for something and the sender isn't in your family list.";
    case WARN_REASON.LOW_CONFIDENCE:
      return "This email might be asking for something. Be careful.";
    case WARN_REASON.LISTED_SENDER:
      return "This sender has warning signs.";
    case WARN_REASON.UNREACHABLE:
      return describeUnreachableWarning(email);
    default:
      console.error("SendPidge: WARN email has an unknown warnReason", email.warnReason);
      return "Pidge has a warning about this email.";
  }
}

function describeUnreachableWarning(email) {
  if (email.request === null || email.request === undefined) {
    console.error("SendPidge: unreachable warning arrived without a request", email.emailId);
    return "Pidge couldn't reach your family. The email stays held.";
  }
  return `Pidge couldn't reach ${email.request.contactName}. The email stays held.`;
}

// One calm line shown at the top of a WARN email's detail.
export function buildWarnBannerElement(email) {
  const bannerElement = createElementWithText("p", "warn-banner", describeWarnReason(email));
  bannerElement.setAttribute("role", "status");
  return bannerElement;
}
