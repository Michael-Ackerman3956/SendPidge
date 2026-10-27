// Pidge hold overlay (countdown, 5-second long-press cancel, shield on timeout) and the calm WARN banner.
// All email fields are attacker-controlled: rendered only through textContent.
import { INTENT, WARN_REASON } from "./constants.js";
import {
  SVG_NAMESPACE, buildPidgeIconElement, buildShieldIcon, createElementWithText, formatRemainingTimeAsMinutesSeconds, parseTimestampMilliseconds,
} from "./dom-helpers.js";

const CANCEL_LONG_PRESS_DURATION_MS = 5000;
const COUNTDOWN_TICK_INTERVAL_MS = 1000;
const COUNTDOWN_RING_RADIUS = 38;
const COUNTDOWN_RING_CIRCUMFERENCE = 2 * Math.PI * COUNTDOWN_RING_RADIUS;

const INTENT_PHRASES = Object.freeze({
  [INTENT.MONEY]: "money",
  [INTENT.CODE]: "a code",
  [INTENT.URGENT_ACTION]: "something urgent",
});

let activeOverlayTeardown = null;

function describeIntentForHoldMessage(intent) {
  const intentPhrase = INTENT_PHRASES[intent];
  if (intentPhrase === undefined) {
    console.error("SendPidge: hold request has an unknown intent", intent);
    return "something";
  }
  return intentPhrase;
}

function parseExpiryTimeOrReportError(expiresAtIsoText) {
  const expiresAtMilliseconds = parseTimestampMilliseconds(expiresAtIsoText);
  if (expiresAtMilliseconds === null) {
    console.error("SendPidge: hold request has an unreadable expiresAt; showing it as expired", expiresAtIsoText);
    return Date.now();
  }
  return expiresAtMilliseconds;
}

// The ring around the countdown: a track circle and a fill circle whose dash offset follows the time left.
function buildCountdownRingElement() {
  const ringSvg = document.createElementNS(SVG_NAMESPACE, "svg");
  ringSvg.setAttribute("viewBox", "0 0 86 86");
  ringSvg.setAttribute("class", "countdown-ring");
  ringSvg.setAttribute("aria-hidden", "true");
  ["countdown-ring-track", "countdown-ring-fill"].forEach((className) => {
    const circle = document.createElementNS(SVG_NAMESPACE, "circle");
    circle.setAttribute("class", className);
    circle.setAttribute("cx", "43");
    circle.setAttribute("cy", "43");
    circle.setAttribute("r", String(COUNTDOWN_RING_RADIUS));
    ringSvg.append(circle);
  });
  ringSvg.querySelector(".countdown-ring-fill").style.strokeDasharray = String(COUNTDOWN_RING_CIRCUMFERENCE);
  return ringSvg;
}

// Starts the countdown to expiresAt. At 0 the checking line becomes the shield state.
function startHoldCountdown({ countdownElement, ringElement, checkingLineElement, statusRegionElement, contactName, expiresAtIsoText }) {
  const expiresAtMilliseconds = parseExpiryTimeOrReportError(expiresAtIsoText);
  const totalMilliseconds = Math.max(1, expiresAtMilliseconds - Date.now());
  const ringFill = ringElement.querySelector(".countdown-ring-fill");
  let countdownIntervalId = null;

  function showShieldTimeoutState() {
    checkingLineElement.replaceChildren(buildShieldIcon());
    statusRegionElement.textContent = `${contactName} hasn't answered. The email stays held.`;
    countdownElement.hidden = true;
    ringElement.hidden = true;
  }

  function updateCountdown() {
    const remainingMilliseconds = expiresAtMilliseconds - Date.now();
    if (remainingMilliseconds <= 0) {
      clearInterval(countdownIntervalId);
      showShieldTimeoutState();
      return;
    }
    countdownElement.textContent = formatRemainingTimeAsMinutesSeconds(remainingMilliseconds);
    ringFill.style.strokeDashoffset = String(COUNTDOWN_RING_CIRCUMFERENCE * (1 - remainingMilliseconds / totalMilliseconds));
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
  const label = createElementWithText("span", "cancel-label", "Hold to cancel");
  label.append(createElementWithText("small", "", "press and hold for 5 seconds"));
  cancelButton.append(createElementWithText("span", "cancel-fill"), label);
  return cancelButton;
}

// Closes whichever overlay holds the single slot (hold overlay or denied verdict overlay).
export function closeHoldOverlay() {
  if (activeOverlayTeardown !== null) {
    activeOverlayTeardown();
    activeOverlayTeardown = null;
  }
}

// Lets another overlay (the denied verdict) take the single slot, closing the current one first.
export function replaceActiveOverlay(overlayTeardown) {
  closeHoldOverlay();
  activeOverlayTeardown = overlayTeardown;
}

// Shows the hold overlay inside overlayHostElement for one held email. onReleasedByUser runs after the 5-second press.
export function showHoldOverlay({ overlayHostElement, email, onReleasedByUser }) {
  closeHoldOverlay();
  const { contactName, intent, expiresAt } = email.request;
  const overlayElement = createElementWithText("div", "hold-overlay");
  overlayElement.setAttribute("role", "alertdialog");
  overlayElement.setAttribute("aria-modal", "true");
  overlayElement.setAttribute("aria-labelledby", "hold-overlay-message");

  const pidgeFace = createElementWithText("span", "hold-overlay-face");
  pidgeFace.append(buildPidgeIconElement("hold-overlay-pidge", "Pidge"));
  const messageElement = createElementWithText("p", "hold-overlay-message");
  messageElement.append(`This email says it is from `, createElementWithText("strong", "", contactName),
    ` and asks for ${describeIntentForHoldMessage(intent)}. I will check with ${contactName}.`);
  messageElement.id = "hold-overlay-message";
  const checkingLineElement = createElementWithText("div", "hold-overlay-checking");
  checkingLineElement.append(createElementWithText("span", "checking-pulse"));
  const statusRegionElement = createElementWithText("p", "hold-overlay-status", `Pidge is checking with ${contactName}`);
  statusRegionElement.setAttribute("aria-live", "polite");
  const countdownWrap = createElementWithText("div", "hold-overlay-countdown-wrap");
  const ringElement = buildCountdownRingElement();
  const countdownElement = createElementWithText("p", "hold-overlay-countdown");
  countdownElement.setAttribute("role", "timer");
  countdownWrap.append(ringElement, countdownElement);
  const cancelButton = buildCancelButton();

  overlayElement.append(createElementWithText("span", "sheet-grab"), pidgeFace, messageElement, checkingLineElement, statusRegionElement, countdownWrap, cancelButton);
  overlayHostElement.append(overlayElement);

  const stopCountdown = startHoldCountdown({ countdownElement, ringElement, checkingLineElement, statusRegionElement, contactName, expiresAtIsoText: expiresAt });
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
      return "Pidge couldn't check this one. Read it slowly.";
    case WARN_REASON.UNKNOWN_NAME:
      return "This asks for something, and the sender is not in your circle.";
    case WARN_REASON.LOW_CONFIDENCE:
      return "This might be asking for something. Be careful.";
    case WARN_REASON.LISTED_SENDER:
      return "This sender is on a warning list.";
    case WARN_REASON.UNREACHABLE:
      return describeUnreachableWarning(email);
    case WARN_REASON.ESCALATION_CLEARED:
      return "Pidge checked twice; a ! mark, not a hold.";
    case WARN_REASON.LINK_REASONS:
      return "A link in this email is not what it says.";
    case WARN_REASON.ASK_CAPPED:
      return describeAskCappedWarning(email);
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

function describeAskCappedWarning(email) {
  if (email.request === null || email.request === undefined) {
    console.error("SendPidge: ask_capped warning arrived without a request", email.emailId);
    return "Pidge has already asked your family a lot this hour. The email stays held.";
  }
  return `Pidge has already asked ${email.request.contactName} a lot this hour. The email stays held.`;
}

// One calm line shown at the top of a WARN email's detail.
export function buildWarnBannerElement(email) {
  const bannerElement = createElementWithText("p", "warn-banner", describeWarnReason(email));
  bannerElement.setAttribute("role", "status");
  return bannerElement;
}
