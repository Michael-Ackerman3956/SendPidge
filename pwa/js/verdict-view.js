// Grandma's verdict visuals: one display state per email, the row mark and status chip, the detail banner, the celebration
// card and the red denied-verdict overlay. contactName and every email field are attacker-influenced: textContent only.
import { CALL_PROMISE, CHANNEL, EMAIL_DISPLAY_STATE, POLICY_DECISION, REQUEST_STATUS } from "./constants.js";
import { buildPidgeIconElement, buildTickIcon, createElementWithText, formatRemainingTimeAsMinutesSeconds, parseTimestampMilliseconds } from "./dom-helpers.js";
import { buildWarnBannerElement, replaceActiveOverlay } from "./pidge-overlay.js";

const CONFETTI_PIECE_COUNT = 18;
const CONFETTI_COLORS = Object.freeze(["#4ade80", "#3AA68E", "#F59E0B", "#ffffff"]);
const ANSWER_CHANNEL_LABELS = Object.freeze({
  [CHANNEL.INAPP]: "in the app",
  [CHANNEL.WHATSAPP]: "on WhatsApp",
  [CHANNEL.RCS]: "by RCS",
  [CHANNEL.EMAIL]: "by email",
  [CHANNEL.VOICE]: "by phone",
});

// A confirmed_pending whose releaseAt has passed reads as confirmed (the server releases it on its next read too).
export function hasReleaseTimePassed(request) {
  const releaseAtMilliseconds = parseTimestampMilliseconds(request.releaseAt);
  return releaseAtMilliseconds !== null && releaseAtMilliseconds <= Date.now();
}

// Priority: reading > quarantined > denied > confirmed / releasing / reversed > released by you > awaiting verdict > flagged > plain.
// DENIED outranks a local release so Jimin's No still reaches Grandma after she cancelled the hold.
export function deriveEmailDisplayState(email, isReleasedByUser) {
  const request = email.request;
  if (email.state === null) return EMAIL_DISPLAY_STATE.READING;
  if (email.quarantined) return EMAIL_DISPLAY_STATE.QUARANTINED;
  if (request !== null && request.status === REQUEST_STATUS.DENIED) return EMAIL_DISPLAY_STATE.DENIED;
  if (request !== null && request.status === REQUEST_STATUS.CONFIRMED) return EMAIL_DISPLAY_STATE.CONFIRMED;
  if (request !== null && request.status === REQUEST_STATUS.CONFIRMED_PENDING) {
    return hasReleaseTimePassed(request) ? EMAIL_DISPLAY_STATE.CONFIRMED : EMAIL_DISPLAY_STATE.RELEASING;
  }
  if (request !== null && request.status === REQUEST_STATUS.REVERSED_NO) return EMAIL_DISPLAY_STATE.REVERSED;
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

// The small mark on the row's avatar, or null for a plain row. The ! turns red only when Grandma confirms (quarantined).
export function buildRowMarkElement(emailDisplayState) {
  switch (emailDisplayState) {
    case EMAIL_DISPLAY_STATE.QUARANTINED:
      return buildFlagMarkElement(true);
    case EMAIL_DISPLAY_STATE.CONFIRMED:
    case EMAIL_DISPLAY_STATE.REVERSED:
      return buildVerifiedTickMarkElement();
    case EMAIL_DISPLAY_STATE.DENIED:
    case EMAIL_DISPLAY_STATE.RELEASING:
    case EMAIL_DISPLAY_STATE.AWAITING_VERDICT:
    case EMAIL_DISPLAY_STATE.FLAGGED:
      return buildFlagMarkElement(false);
    default:
      return null;
  }
}

function buildStatusChipElement(chipClassName, chipText) {
  return createElementWithText("span", `status-chip ${chipClassName}`, chipText);
}

// The text chip beside the channel tag on an inbox row ("Held · checking with Jimin"), or null for a plain row.
export function buildRowStatusChipElement(email, emailDisplayState) {
  const contactName = email.request === null ? "" : email.request.contactName;
  switch (emailDisplayState) {
    case EMAIL_DISPLAY_STATE.READING:
      return buildStatusChipElement("is-hold", "Pidge is reading…");
    case EMAIL_DISPLAY_STATE.QUARANTINED:
      return buildStatusChipElement("is-threat", "Reported");
    case EMAIL_DISPLAY_STATE.DENIED:
      return buildStatusChipElement("is-threat", `Not from ${contactName}`);
    case EMAIL_DISPLAY_STATE.CONFIRMED:
    case EMAIL_DISPLAY_STATE.REVERSED:
      return buildStatusChipElement("is-verified", `${contactName} confirmed`);
    case EMAIL_DISPLAY_STATE.RELEASING:
      return buildStatusChipElement("is-hold", "Opening…");
    case EMAIL_DISPLAY_STATE.RELEASED_BY_USER:
      return buildStatusChipElement("", "Released by you");
    case EMAIL_DISPLAY_STATE.AWAITING_VERDICT:
      return buildStatusChipElement("is-hold", `Held · checking with ${contactName}`);
    case EMAIL_DISPLAY_STATE.FLAGGED:
      return buildStatusChipElement("is-warn", email.state === POLICY_DECISION.HOLD ? "Held" : "Be careful");
    default:
      return null;
  }
}

function buildVerifiedBannerElement(bannerText, shouldCelebrate) {
  const bannerElement = createElementWithText("div", shouldCelebrate ? "verified-banner is-celebrating" : "verified-banner");
  bannerElement.setAttribute("role", "status");
  const iconGroup = createElementWithText("span", "verified-icon-group");
  iconGroup.append(buildPidgeIconElement("verified-pidge", ""), buildTickIcon("tick-icon verified-tick"));
  bannerElement.append(iconGroup, createElementWithText("span", "verified-text", bannerText));
  return bannerElement;
}

// "Jimin answered on WhatsApp.", or null until the request is answered.
function buildAnswerChannelLineElement(request) {
  if (request === null || request.repliedVia === null || request.repliedVia === undefined) return null;
  return createElementWithText("p", "answer-channel-line", `${request.contactName} answered ${ANSWER_CHANNEL_LABELS[request.repliedVia]}.`);
}

// "{contactName} will call you today." / "… tomorrow.", or null when the contact made no call promise.
function buildCallPromiseLineElement(request) {
  if (request === null || request.callPromise === null || request.callPromise === undefined) return null;
  const dayText = request.callPromise === CALL_PROMISE.TODAY ? "today" : "tomorrow";
  return createElementWithText("p", "call-promise-line", `${request.contactName} will call you ${dayText}.`);
}

// The verdict banner followed by the answer-channel and call-promise lines, wrapped only when there is one to show.
function withVerdictFollowUpLines(bannerElement, request) {
  const followingLines = [buildAnswerChannelLineElement(request), buildCallPromiseLineElement(request)].filter((line) => line !== null);
  if (followingLines.length === 0) return bannerElement;
  const bannerGroup = createElementWithText("div", "verdict-banner-group");
  bannerGroup.append(bannerElement, ...followingLines);
  return bannerGroup;
}

// The banner at the top of the detail, or null when the state has none.
export function buildDetailBannerElement(email, emailDisplayState, shouldCelebrate) {
  if (emailDisplayState === EMAIL_DISPLAY_STATE.READING) {
    const readingBanner = createElementWithText("p", "reading-banner", "Pidge is reading this one…");
    readingBanner.setAttribute("role", "status");
    return readingBanner;
  }
  if (emailDisplayState === EMAIL_DISPLAY_STATE.QUARANTINED) {
    const quarantineBanner = createElementWithText("p", "threat-banner",
      email.request === null ? "Reported and quarantined." : `Reported and quarantined. ${email.request.contactName} did not send this.`);
    quarantineBanner.setAttribute("role", "status");
    return withVerdictFollowUpLines(quarantineBanner, email.request);
  }
  if (emailDisplayState === EMAIL_DISPLAY_STATE.CONFIRMED) {
    return withVerdictFollowUpLines(
      buildVerifiedBannerElement(`${email.request.contactName} confirmed they sent this.`, shouldCelebrate), email.request);
  }
  if (emailDisplayState === EMAIL_DISPLAY_STATE.REVERSED) {
    return withVerdictFollowUpLines(
      buildVerifiedBannerElement(`${email.request.contactName} changed their answer: they sent this.`, shouldCelebrate), email.request);
  }
  if (emailDisplayState === EMAIL_DISPLAY_STATE.RELEASING) {
    return withVerdictFollowUpLines(buildReleasingBannerElement(email.request), email.request);
  }
  if (emailDisplayState === EMAIL_DISPLAY_STATE.RELEASED_BY_USER) {
    const releasedBanner = createElementWithText("p", "released-banner", `You released this one yourself. Pidge still hasn't heard from ${email.request.contactName}.`);
    releasedBanner.setAttribute("role", "status");
    return releasedBanner;
  }
  if (email.state === POLICY_DECISION.WARN) {
    return buildWarnBannerElement(email);
  }
  return null;
}

// "Jimin said yes. Opening in 7 s." with the seconds ticking until releaseAt; the caller re-renders when it passes.
function buildReleasingBannerElement(request) {
  const releasingBanner = createElementWithText("p", "warn-banner", `${request.contactName} said yes. Opening in `);
  releasingBanner.setAttribute("role", "status");
  const secondsElement = createElementWithText("span", "releasing-seconds");
  secondsElement.setAttribute("role", "timer");
  const releaseAtMilliseconds = parseTimestampMilliseconds(request.releaseAt) ?? Date.now();
  const updateSeconds = () => {
    const remainingMilliseconds = releaseAtMilliseconds - Date.now();
    secondsElement.textContent = `${Math.max(0, Math.ceil(remainingMilliseconds / 1000))} s.`;
    if (remainingMilliseconds <= 0 || !secondsElement.isConnected) clearInterval(tickIntervalId);
  };
  const tickIntervalId = setInterval(updateSeconds, 1000);
  updateSeconds();
  releasingBanner.append(secondsElement);
  return releasingBanner;
}

// The confetti card over the detail after the real person said yes; any tap closes it.
export function buildCelebrationElement(contactName, onClosed) {
  const celebration = createElementWithText("div", "celebration");
  celebration.setAttribute("role", "dialog");
  celebration.setAttribute("aria-label", `It really was ${contactName}`);
  for (let pieceIndex = 0; pieceIndex < CONFETTI_PIECE_COUNT; pieceIndex += 1) {
    const piece = createElementWithText("i", "confetti-piece");
    piece.style.left = `${8 + pieceIndex * 5}%`;
    piece.style.background = CONFETTI_COLORS[pieceIndex % CONFETTI_COLORS.length];
    piece.style.animationDelay = `${(pieceIndex % 6) * 0.12}s`;
    celebration.append(piece);
  }
  const card = createElementWithText("div", "celebration-card");
  const iconGroup = createElementWithText("span", "verified-icon-group celebration-icon-group");
  iconGroup.append(buildPidgeIconElement("verified-pidge celebration-pidge", ""), buildTickIcon("tick-icon verified-tick celebration-tick"));
  const openButton = createElementWithText("button", "celebration-open-button", "Open the email");
  openButton.type = "button";
  card.append(iconGroup, createElementWithText("p", "celebration-title", `It really was ${contactName}.`),
    createElementWithText("p", "celebration-subtitle", "Verified by the real person, not by a filter."), openButton);
  celebration.append(card);
  celebration.addEventListener("click", () => { celebration.remove(); onClosed(); });
  openButton.focus();
  return celebration;
}

function buildConfirmReportButton(isReporting, onConfirmTapped) {
  const confirmButton = createElementWithText("button", "confirm-report-button", isReporting ? "Reporting…" : "Confirm and report");
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
    `Real ${email.request.contactName} replied: they did not send this.`);
  messageElement.id = "denied-verdict-message";
  const confirmButton = buildConfirmReportButton(isReporting, () => onConfirmTapped(email));
  const pidgeFace = createElementWithText("span", "hold-overlay-face is-threat");
  pidgeFace.append(buildPidgeIconElement("hold-overlay-pidge", "Pidge"), createElementWithText("span", "hold-overlay-face-badge", "✕"));
  overlayElement.append(createElementWithText("span", "sheet-grab"), pidgeFace, messageElement,
    createElementWithText("p", "hold-overlay-status", "Do not send money or share anything from this email."));
  [buildAnswerChannelLineElement(email.request), buildCallPromiseLineElement(email.request)]
    .filter((line) => line !== null).forEach((line) => overlayElement.append(line));
  overlayElement.append(confirmButton);
  replaceActiveOverlay(() => overlayElement.remove());
  overlayHostElement.append(overlayElement);
  if (isReporting) {
    messageElement.tabIndex = -1;
    messageElement.focus();
  } else {
    confirmButton.focus();
  }
}
