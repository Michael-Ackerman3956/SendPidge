// Grandma's PWA: access token, inbox, email detail, and live EMAIL_STATE pushes.
// Email fields are attacker-controlled: rendered only through textContent / createElement.
import { CONNECTION_STATUS, POLICY_DECISION, PUSH_MESSAGE_TYPE } from "./constants.js";
import { connectToPushChannel } from "./websocket.js";
import { buildWarnBannerElement, closeHoldOverlay, showHoldOverlay } from "./pidge-overlay.js";

const ACCESS_TOKEN_STORAGE_KEY = "sendpidge.accessToken";
const ACCESS_TOKEN_QUERY_PARAMETER = "token";
const MILLISECONDS_PER_MINUTE = 60000;
const MILLISECONDS_PER_HOUR = 3600000;
const MILLISECONDS_PER_DAY = 86400000;
const KNOWN_POLICY_DECISIONS = new Set(Object.values(POLICY_DECISION));

const emailsById = new Map();
const releasedByUserEmailIds = new Set();
let openEmailId = null;

const pageElements = {
  backButton: document.getElementById("back-button"),
  topbarTitle: document.getElementById("topbar-title"),
  connectionDot: document.getElementById("connection-dot"),
  connectionLabel: document.getElementById("connection-label"),
  tokenError: document.getElementById("token-error"),
  inboxScreen: document.getElementById("inbox-screen"),
  inboxList: document.getElementById("inbox-list"),
  detailScreen: document.getElementById("detail-screen"),
  detailContent: document.getElementById("detail-content"),
};

function buildMockFamilyEmails() {
  const now = Date.now();
  const receivedHoursAgo = (hours) => new Date(now - hours * MILLISECONDS_PER_HOUR).toISOString();
  const mockEmail = (emailId, senderName, senderAddress, subject, hoursAgo, textBody) => ({
    emailId, senderName, senderAddress, subject, receivedAt: receivedHoursAgo(hoursAgo), textBody,
    state: POLICY_DECISION.PASS, warnReason: null, reasons: [], request: null,
  });
  return [
    mockEmail("mock-sunday-lunch", "Minsoo", "minsoo@example.com", "Sunday lunch", 2,
      "Hi Mom, we'll come by around noon on Sunday. I'll bring the kimchi jjigae you like. See you then!"),
    mockEmail("mock-garden-photos", "Young-hee", "younghee@example.com", "Your tomatoes!", 27,
      "The tomatoes from your garden were so sweet. Can you show me how you grow them next time?"),
    mockEmail("mock-church-picnic", "Pastor Kim", "pastor.kim@example.com", "Picnic next Saturday", 75,
      "Just a reminder that the church picnic is next Saturday at the park. Hope to see you there."),
  ];
}

function readAccessTokenFromStorage() {
  try {
    return window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY);
  } catch (storageError) {
    console.error("SendPidge: could not read saved access token", storageError.name);
    return null;
  }
}

function saveAccessTokenToStorage(accessToken) {
  try {
    window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, accessToken);
  } catch (storageError) {
    console.error("SendPidge: could not save access token; it lasts for this visit only", storageError.name);
  }
}

// Takes ?token= from the URL (then strips it from the address bar), else the saved token.
function resolveAccessToken() {
  const pageUrl = new URL(window.location.href);
  const tokenFromUrl = pageUrl.searchParams.get(ACCESS_TOKEN_QUERY_PARAMETER);
  if (tokenFromUrl) {
    saveAccessTokenToStorage(tokenFromUrl);
    pageUrl.searchParams.delete(ACCESS_TOKEN_QUERY_PARAMETER);
    window.history.replaceState(null, "", pageUrl.pathname + pageUrl.search + pageUrl.hash);
    return tokenFromUrl;
  }
  return readAccessTokenFromStorage();
}

function parseTimestampMilliseconds(isoText) {
  const timestampMilliseconds = Date.parse(isoText);
  return Number.isNaN(timestampMilliseconds) ? null : timestampMilliseconds;
}

function formatRelativeReceivedTime(receivedAtIsoText) {
  const receivedAtMilliseconds = parseTimestampMilliseconds(receivedAtIsoText);
  if (receivedAtMilliseconds === null) {
    console.error("SendPidge: email has an unreadable receivedAt", receivedAtIsoText);
    return "";
  }
  const elapsedMilliseconds = Date.now() - receivedAtMilliseconds;
  if (elapsedMilliseconds < MILLISECONDS_PER_MINUTE) return "now";
  if (elapsedMilliseconds < MILLISECONDS_PER_HOUR) return `${Math.floor(elapsedMilliseconds / MILLISECONDS_PER_MINUTE)}m`;
  if (elapsedMilliseconds < MILLISECONDS_PER_DAY) return `${Math.floor(elapsedMilliseconds / MILLISECONDS_PER_HOUR)}h`;
  if (elapsedMilliseconds < 7 * MILLISECONDS_PER_DAY) return new Date(receivedAtMilliseconds).toLocaleDateString(undefined, { weekday: "short" });
  return new Date(receivedAtMilliseconds).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function createElementWithText(tagName, className, textContent) {
  const element = document.createElement(tagName);
  element.className = className;
  if (textContent !== undefined) {
    element.textContent = textContent;
  }
  return element;
}

function buildAvatarInitialElement(senderName) {
  const avatar = createElementWithText("span", "avatar-initial", (senderName.trim()[0] || "?").toUpperCase());
  avatar.setAttribute("aria-hidden", "true");
  return avatar;
}

function isEmailFlagged(email) {
  return email.state === POLICY_DECISION.HOLD || email.state === POLICY_DECISION.WARN;
}

function isEmailAwaitingVerdict(email) {
  return email.state === POLICY_DECISION.HOLD && !releasedByUserEmailIds.has(email.emailId) && email.request !== null;
}

function buildRowStatusElement(email) {
  if (releasedByUserEmailIds.has(email.emailId)) {
    return createElementWithText("span", "row-released-tag", "Released by you");
  }
  if (isEmailFlagged(email)) {
    const flagMark = createElementWithText("span", "row-flag-mark", "!");
    flagMark.setAttribute("aria-label", "Flagged");
    return flagMark;
  }
  return null;
}

function buildInboxRowElement(email) {
  const listItem = document.createElement("li");
  const rowButton = createElementWithText("button", "inbox-row");
  rowButton.type = "button";
  rowButton.dataset.emailId = email.emailId;
  const avatar = buildAvatarInitialElement(email.senderName);
  const rowText = createElementWithText("span", "row-text");
  const rowTopLine = createElementWithText("span", "row-top-line");
  rowTopLine.append(createElementWithText("span", "row-sender", email.senderName),
    createElementWithText("span", "row-time", formatRelativeReceivedTime(email.receivedAt)));
  const rowBottomLine = createElementWithText("span", "row-bottom-line");
  rowBottomLine.append(createElementWithText("span", "channel-tag", "Email"));
  const rowStatus = buildRowStatusElement(email);
  if (rowStatus !== null) rowBottomLine.append(rowStatus);
  rowText.append(rowTopLine, createElementWithText("span", "row-subject", email.subject),
    createElementWithText("span", "row-preview", email.textBody), rowBottomLine);
  rowButton.append(avatar, rowText);
  rowButton.addEventListener("click", () => openEmailDetail(email.emailId));
  listItem.append(rowButton);
  return listItem;
}

function sortEmailsNewestFirst(emails) {
  const sortKey = (email) => parseTimestampMilliseconds(email.receivedAt) ?? Number.NEGATIVE_INFINITY;
  return [...emails].sort((first, second) => sortKey(second) - sortKey(first));
}

function renderInbox() {
  const rowElements = sortEmailsNewestFirst(emailsById.values()).map(buildInboxRowElement);
  pageElements.inboxList.replaceChildren(...rowElements);
}

function buildDetailHeaderElement(email) {
  const headerElement = createElementWithText("div", "detail-header");
  const senderBlock = createElementWithText("div", "detail-sender-block");
  senderBlock.append(createElementWithText("p", "detail-sender-name", email.senderName),
    createElementWithText("p", "detail-sender-address", email.senderAddress));
  headerElement.append(buildAvatarInitialElement(email.senderName),
    senderBlock, createElementWithText("span", "row-time", formatRelativeReceivedTime(email.receivedAt)));
  return headerElement;
}

function renderEmailDetail(email) {
  closeHoldOverlay();
  const detailChildren = [buildDetailHeaderElement(email)];
  if (email.state === POLICY_DECISION.WARN) {
    detailChildren.push(buildWarnBannerElement(email));
  }
  const subjectHeading = createElementWithText("h2", "detail-subject", email.subject);
  subjectHeading.tabIndex = -1;
  detailChildren.push(subjectHeading, createElementWithText("p", "detail-body", email.textBody));
  pageElements.detailContent.replaceChildren(...detailChildren);
  if (isEmailAwaitingVerdict(email)) {
    showHoldOverlay({ overlayHostElement: pageElements.detailScreen, email, onReleasedByUser: handleUserReleasedHeldEmail });
  } else {
    subjectHeading.focus();
  }
}

function openEmailDetail(emailId) {
  openEmailId = emailId;
  pageElements.inboxScreen.hidden = true;
  pageElements.detailScreen.hidden = false;
  pageElements.backButton.hidden = false;
  pageElements.topbarTitle.textContent = "Email";
  renderEmailDetail(emailsById.get(emailId));
}

function showInboxScreen() {
  const previouslyOpenEmailId = openEmailId;
  closeHoldOverlay();
  openEmailId = null;
  pageElements.detailScreen.hidden = true;
  pageElements.backButton.hidden = true;
  pageElements.inboxScreen.hidden = false;
  pageElements.topbarTitle.textContent = "Inbox";
  const previouslyOpenRow = [...pageElements.inboxList.querySelectorAll(".inbox-row")]
    .find((rowButton) => rowButton.dataset.emailId === previouslyOpenEmailId);
  if (previouslyOpenRow) previouslyOpenRow.focus();
}

// Local only in this slice: no server call is made.
function handleUserReleasedHeldEmail(emailId) {
  releasedByUserEmailIds.add(emailId);
  renderInbox();
  if (openEmailId === emailId) {
    renderEmailDetail(emailsById.get(emailId));
  }
}

function isValidEmailStatePayload(email) {
  const hasRequiredFields = email !== null && typeof email === "object" && typeof email.emailId === "string"
    && typeof email.senderName === "string" && typeof email.subject === "string" && typeof email.textBody === "string";
  if (!hasRequiredFields || !KNOWN_POLICY_DECISIONS.has(email.state)) {
    console.error("SendPidge: email_state push has a missing field or unknown state", email);
    return false;
  }
  if (email.state === POLICY_DECISION.HOLD && (email.request === null || email.request === undefined)) {
    console.error("SendPidge: HOLD push arrived without a request; showing the row flagged without the overlay", email.emailId);
  }
  return true;
}

function handleEmailStatePush(email) {
  if (!isValidEmailStatePayload(email)) return;
  const normalizedEmail = { ...email, request: email.request ?? null };
  releasedByUserEmailIds.delete(normalizedEmail.emailId); // a newer server state replaces the local release
  emailsById.set(normalizedEmail.emailId, normalizedEmail);
  renderInbox();
  if (isEmailAwaitingVerdict(normalizedEmail)) {
    openEmailDetail(normalizedEmail.emailId);
  } else if (openEmailId === normalizedEmail.emailId) {
    renderEmailDetail(normalizedEmail);
  }
}

function handlePushMessage(pushMessage) {
  switch (pushMessage?.type) {
    case PUSH_MESSAGE_TYPE.EMAIL_STATE:
      handleEmailStatePush(pushMessage.email);
      break;
    default:
      console.warn("SendPidge: ignoring push of unknown type", pushMessage?.type);
  }
}

function updateConnectionDot(connectionStatus) {
  const isConnected = connectionStatus === CONNECTION_STATUS.CONNECTED;
  pageElements.connectionDot.classList.toggle("is-connected", isConnected);
  pageElements.connectionLabel.textContent = isConnected ? "Connected" : "Reconnecting";
}

function startApp() {
  const accessToken = resolveAccessToken();
  if (!accessToken) {
    pageElements.tokenError.hidden = false;
    return;
  }
  buildMockFamilyEmails().forEach((email) => emailsById.set(email.emailId, email));
  pageElements.backButton.addEventListener("click", showInboxScreen);
  pageElements.inboxScreen.hidden = false;
  renderInbox();
  connectToPushChannel({ accessToken, onPushMessage: handlePushMessage, onConnectionStatusChange: updateConnectionDot });
}

startApp();
