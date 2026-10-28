// SendPidge PWA entry: sign-in or access token, socket, role routing from sync_state, and push dispatch to the receiver,
// contact or org admin view (an org admin's cards are a contact's). The role is never stored: every sync_state names it again.
import { ACTION_REJECTED_REASON, CLIENT_ACTION, CONNECTION_STATUS, PUSH_MESSAGE_TYPE, USER_ROLE } from "./constants.js";
import { connectToPushChannel, sendClientAction } from "./websocket.js";
import { completeSignInFromRedirect, hasSignInSession, readSignInConfig, resolveIdToken, startSignIn } from "./auth.js";
import { refreshPushSubscriptionAfterSync, startPushNotifications } from "./push-notifications.js";
import { ACTION_STATUS_MESSAGE, showActionStatusMessage } from "./action-status.js";
import {
  applyReadingPaneLayout, applyReceiverSyncState, handleEmailStatePush, handleMessageStatePush, handleReceiverActionRejected,
  handleReceiverRequestStatePush, hideReceiverView, setUnseenHoldListener, showReceiverView,
} from "./receiver-view.js";
import {
  applyContactSyncState, handleContactActionRejected, handleContactRequestStatePush, handleVerificationCardPush,
  hideContactView, showContactView,
} from "./contact-view.js";
import { applyChatSyncState, handlePidgeReplyPush, hidePidgeView, showPidgeView, startPidgeChat } from "./pidge-chat.js";
import {
  applyPidgeSyncState, handleCaseNotePush, handleCaseStatePush, handlePidgeActionRejected, hideCasesView, openReportPaymentForm, showCasesView,
} from "./cases-view.js";
import { applyCircleStatePush, applyCircleSyncState, hideCircleView, showCircleView, startCircleView } from "./circle-view.js";
import {
  ONBOARDING_START, handleCircleSyncDuringOnboarding, handleContactAddedPush, handleContactEditedPush, handleOnboardingActionRejected,
  hideOnboarding, startOnboarding,
} from "./onboarding.js";
import { startReadingPane } from "./reading-pane.js";
import { applyOrgAdminSyncState, handleOrgActionRejected, hideOrgView } from "./org-view.js";
import { applyOrgPreferenceState, handleOrgPreferenceActionRejected } from "./org-preference-view.js";

const ACCESS_TOKEN_STORAGE_KEY = "sendpidge.accessToken";
const ACCESS_TOKEN_QUERY_PARAMETER = "token";
const KNOWN_ACTION_REJECTED_REASONS = new Set(Object.values(ACTION_REJECTED_REASON));
const ORG_CHANGED_RESYNC_DELAY_MS = 2000;

let activeUserRole = null;
let orgResyncTimeoutId = null;

const RECEIVER_TAB = Object.freeze({ INBOX: "inbox", CASES: "cases", CIRCLE: "circle", PIDGE: "pidge" });
let activeReceiverTab = RECEIVER_TAB.INBOX;
let isOnboardingOpen = false;
let hasUnseenHold = false;

const pageElements = {
  connectionDots: document.querySelectorAll(".connection-dot"), // one in the top bar (phone), one on the desktop rail
  connectionLabel: document.getElementById("connection-label"),
  tokenError: document.getElementById("token-error"),
  signInButton: document.getElementById("sign-in-button"),
  loadingState: document.getElementById("loading-state"),
  receiverTabs: document.getElementById("receiver-tabs"),
  inboxTabDot: document.getElementById("tab-inbox-dot"),
  railMeInitial: document.getElementById("rail-me-initial"),
  railMeName: document.getElementById("rail-me-name"),
};
const tabButtons = Object.freeze({
  [RECEIVER_TAB.INBOX]: document.getElementById("tab-inbox"),
  [RECEIVER_TAB.CASES]: document.getElementById("tab-cases"),
  [RECEIVER_TAB.CIRCLE]: document.getElementById("tab-circle"),
  [RECEIVER_TAB.PIDGE]: document.getElementById("tab-pidge"),
});
const tabViews = Object.freeze({
  [RECEIVER_TAB.INBOX]: { show: showReceiverView, hide: hideReceiverView },
  [RECEIVER_TAB.CASES]: { show: showCasesView, hide: hideCasesView },
  [RECEIVER_TAB.CIRCLE]: { show: showCircleView, hide: hideCircleView },
  [RECEIVER_TAB.PIDGE]: { show: showPidgeView, hide: hidePidgeView },
});

function renderInboxTabDot() {
  pageElements.inboxTabDot.hidden = !hasUnseenHold || activeReceiverTab === RECEIVER_TAB.INBOX;
}

function showReceiverTab(tab) {
  activeReceiverTab = tab;
  if (isOnboardingOpen) {
    isOnboardingOpen = false;
    hideOnboarding();
    pageElements.receiverTabs.hidden = false;
  }
  Object.entries(tabButtons).forEach(([tabName, button]) => {
    button.classList.toggle("is-active", tabName === tab);
    button.setAttribute("aria-pressed", String(tabName === tab));
  });
  Object.entries(tabViews).forEach(([tabName, view]) => (tabName === tab ? view.show() : view.hide()));
  renderInboxTabDot();
}

// Onboarding takes the whole screen; the tab bar comes back when it finishes, Back leaves it, or a new email arrives.
// editedEntry: the Circle row being edited (ONBOARDING_START.EDIT_CONTACT), else null.
function openOnboarding(startStep, editedEntry = null) {
  const tabBeforeOnboarding = activeReceiverTab;
  isOnboardingOpen = true;
  Object.values(tabViews).forEach((view) => view.hide());
  pageElements.receiverTabs.hidden = true;
  startOnboarding({
    startStep,
    editedEntry,
    onFinishedCallback: () => showReceiverTab(RECEIVER_TAB.INBOX),
    onExitedCallback: () => showReceiverTab(tabBeforeOnboarding),
  });
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

// The signed-in receiver's name at the bottom of the desktop rail (receiverName is server-set, rendered via textContent only).
function showReceiverNameInRail(receiverName) {
  if (typeof receiverName !== "string" || receiverName.length === 0) return;
  pageElements.railMeInitial.textContent = receiverName[0].toUpperCase();
  pageElements.railMeName.textContent = receiverName;
}

function hideReceiverScreens() {
  Object.values(tabViews).forEach((view) => view.hide());
  hideOnboarding();
  pageElements.receiverTabs.hidden = true;
}

function handleSyncStatePush(pushMessage) {
  switch (pushMessage.role) {
    case USER_ROLE.RECEIVER:
      if (activeUserRole === USER_ROLE.CONTACT || activeUserRole === USER_ROLE.ORG_ADMIN) { hideContactView(); hideOrgView(); }
      activeUserRole = USER_ROLE.RECEIVER;
      pageElements.loadingState.hidden = true;
      pageElements.receiverTabs.hidden = isOnboardingOpen;
      if (activeReceiverTab === RECEIVER_TAB.INBOX && !isOnboardingOpen) showReceiverView();
      showReceiverNameInRail(pushMessage.receiverName);
      applyCircleSyncState(pushMessage.circle, pushMessage.contactVerification);
      applyOrgPreferenceState(pushMessage.orgPreference);
      applyReceiverSyncState(pushMessage.emails, pushMessage.messages);
      applyPidgeSyncState(pushMessage.notes ?? [], pushMessage.cases ?? []);
      applyChatSyncState(pushMessage.chat, pushMessage.chatEnabled);
      handleCircleSyncDuringOnboarding();
      refreshPushSubscriptionAfterSync();
      break;
    case USER_ROLE.CONTACT:
      if (activeUserRole === USER_ROLE.RECEIVER) hideReceiverScreens();
      if (activeUserRole === USER_ROLE.ORG_ADMIN) hideOrgView();
      activeUserRole = USER_ROLE.CONTACT;
      pageElements.loadingState.hidden = true;
      showContactView();
      applyContactSyncState(pushMessage.requests);
      break;
    case USER_ROLE.ORG_ADMIN:
      if (activeUserRole === USER_ROLE.RECEIVER) hideReceiverScreens();
      activeUserRole = USER_ROLE.ORG_ADMIN;
      pageElements.loadingState.hidden = true;
      applyOrgAdminSyncState(pushMessage);
      break;
    default:
      console.error("SendPidge: sync_state has an unknown role", pushMessage.role);
      showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
  }
}

function handleRequestStatePush(requestUpdate) {
  if (activeUserRole === USER_ROLE.RECEIVER) {
    handleReceiverRequestStatePush(requestUpdate);
  } else {
    handleContactRequestStatePush(requestUpdate);
  }
}

function handleActionRejectedPush(pushMessage) {
  const requestId = typeof pushMessage.requestId === "string" ? pushMessage.requestId : null;
  console.warn("SendPidge: client action rejected", pushMessage.reason, requestId);
  if (!KNOWN_ACTION_REJECTED_REASONS.has(pushMessage.reason)) {
    console.error("SendPidge: action_rejected has an unknown reason", pushMessage.reason);
  }
  if (activeUserRole === USER_ROLE.RECEIVER && handleOnboardingActionRejected(pushMessage.reason)) return; // the form shows why
  const isAlreadyRecorded = pushMessage.reason === ACTION_REJECTED_REASON.NOT_APPLIED;
  showActionStatusMessage(isAlreadyRecorded ? ACTION_STATUS_MESSAGE.ALREADY_RECORDED : ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
  if (activeUserRole === USER_ROLE.RECEIVER) {
    handleReceiverActionRejected(requestId);
    handlePidgeActionRejected();
    handleOrgPreferenceActionRejected();
  } else {
    handleContactActionRejected(requestId);
    if (activeUserRole === USER_ROLE.ORG_ADMIN) handleOrgActionRejected();
  }
}

// org_changed carries no data: the first one starts a 2 s wait and every org_changed inside it joins that one re-sync (each sync
// is an audited member view). A closed socket drops it; the reconnect sends its own sync.
function scheduleOrgDashboardResync() {
  if (orgResyncTimeoutId !== null) return;
  orgResyncTimeoutId = setTimeout(() => {
    orgResyncTimeoutId = null;
    sendClientAction({ action: CLIENT_ACTION.SYNC });
  }, ORG_CHANGED_RESYNC_DELAY_MS);
}

// Which role each role-specific push belongs to; a push for the other role (or before sync_state) is ignored with a warning.
const PUSH_MESSAGE_ROLES = Object.freeze({
  [PUSH_MESSAGE_TYPE.EMAIL_STATE]: [USER_ROLE.RECEIVER],
  [PUSH_MESSAGE_TYPE.MESSAGE_STATE]: [USER_ROLE.RECEIVER],
  [PUSH_MESSAGE_TYPE.VERIFICATION_CARD]: [USER_ROLE.CONTACT, USER_ROLE.ORG_ADMIN],
  [PUSH_MESSAGE_TYPE.REQUEST_STATE]: [USER_ROLE.RECEIVER, USER_ROLE.CONTACT, USER_ROLE.ORG_ADMIN],
  [PUSH_MESSAGE_TYPE.ACTION_REJECTED]: [USER_ROLE.RECEIVER, USER_ROLE.CONTACT, USER_ROLE.ORG_ADMIN],
  [PUSH_MESSAGE_TYPE.CASE_NOTE]: [USER_ROLE.RECEIVER],
  [PUSH_MESSAGE_TYPE.CASE_STATE]: [USER_ROLE.RECEIVER],
  [PUSH_MESSAGE_TYPE.CONTACT_NOTICE]: [USER_ROLE.CONTACT],
  [PUSH_MESSAGE_TYPE.PIDGE_REPLY]: [USER_ROLE.RECEIVER],
  [PUSH_MESSAGE_TYPE.CONTACT_ADDED]: [USER_ROLE.RECEIVER],
  [PUSH_MESSAGE_TYPE.CONTACT_EDITED]: [USER_ROLE.RECEIVER],
  [PUSH_MESSAGE_TYPE.CIRCLE_STATE]: [USER_ROLE.RECEIVER],
  [PUSH_MESSAGE_TYPE.ORG_CHANGED]: [USER_ROLE.ORG_ADMIN],
});

function isPushForActiveRole(pushMessageType) {
  const allowedRoles = PUSH_MESSAGE_ROLES[pushMessageType];
  if (allowedRoles === undefined) return true;
  if (allowedRoles.includes(activeUserRole)) return true;
  console.warn("SendPidge: ignoring push for another role or before sync_state", pushMessageType, activeUserRole);
  return false;
}

function handlePushMessage(pushMessage) {
  const pushMessageType = pushMessage?.type;
  if (!isPushForActiveRole(pushMessageType)) return;
  switch (pushMessageType) {
    case PUSH_MESSAGE_TYPE.SYNC_STATE:
      handleSyncStatePush(pushMessage);
      break;
    case PUSH_MESSAGE_TYPE.EMAIL_STATE:
      if (activeReceiverTab !== RECEIVER_TAB.INBOX || isOnboardingOpen) showReceiverTab(RECEIVER_TAB.INBOX); // a new hold must be seen
      handleEmailStatePush(pushMessage.email);
      break;
    case PUSH_MESSAGE_TYPE.MESSAGE_STATE:
      if (activeReceiverTab !== RECEIVER_TAB.INBOX || isOnboardingOpen) showReceiverTab(RECEIVER_TAB.INBOX); // a new hold must be seen
      handleMessageStatePush(pushMessage.message);
      break;
    case PUSH_MESSAGE_TYPE.VERIFICATION_CARD:
      handleVerificationCardPush(pushMessage.request);
      break;
    case PUSH_MESSAGE_TYPE.REQUEST_STATE:
      handleRequestStatePush(pushMessage.request);
      break;
    case PUSH_MESSAGE_TYPE.ACTION_REJECTED:
      handleActionRejectedPush(pushMessage);
      break;
    case PUSH_MESSAGE_TYPE.CASE_NOTE:
      handleCaseNotePush(pushMessage.note);
      break;
    case PUSH_MESSAGE_TYPE.CASE_STATE:
      handleCaseStatePush(pushMessage.case);
      break;
    case PUSH_MESSAGE_TYPE.PIDGE_REPLY:
      handlePidgeReplyPush(pushMessage);
      break;
    case PUSH_MESSAGE_TYPE.CONTACT_NOTICE:
      if (typeof pushMessage.text === "string") showActionStatusMessage(pushMessage.text);
      break;
    case PUSH_MESSAGE_TYPE.CONTACT_ADDED:
      handleContactAddedPush(pushMessage);
      break;
    case PUSH_MESSAGE_TYPE.CONTACT_EDITED:
      handleContactEditedPush(pushMessage);
      break;
    case PUSH_MESSAGE_TYPE.CIRCLE_STATE:
      applyCircleStatePush(pushMessage.circle);
      applyOrgPreferenceState(pushMessage.orgPreference);
      handleCircleSyncDuringOnboarding();
      break;
    case PUSH_MESSAGE_TYPE.ORG_CHANGED:
      scheduleOrgDashboardResync();
      break;
    default:
      console.warn("SendPidge: ignoring push of unknown type", pushMessageType);
  }
}

function updateConnectionDot(connectionStatus) {
  const isConnected = connectionStatus === CONNECTION_STATUS.CONNECTED;
  pageElements.connectionDots.forEach((dot) => dot.classList.toggle("is-connected", isConnected));
  pageElements.connectionLabel.textContent = isConnected ? "Connected" : "Reconnecting";
}

// No token to connect with: the token error, plus Sign in when sign-in is on.
function showTokenErrorScreen(isSignInEnabled) {
  pageElements.loadingState.hidden = true;
  pageElements.tokenError.hidden = false;
  pageElements.signInButton.hidden = !isSignInEnabled;
}

// Sign in stays visible until the socket first connects; a signed-out socket (refresh failed) shows the token error again.
function handleConnectionStatusChange(connectionStatus) {
  updateConnectionDot(connectionStatus);
  if (connectionStatus === CONNECTION_STATUS.CONNECTED) {
    pageElements.signInButton.hidden = true;
  } else if (connectionStatus === CONNECTION_STATUS.SIGNED_OUT) {
    showTokenErrorScreen(true);
  }
}

// The Cognito ID token when sign-in is on and a session exists, else the opaque access token as before; null when neither.
function buildConnectionTokenResolver(signInConfig) {
  const accessToken = resolveAccessToken(); // also strips ?token= from the address bar
  if (signInConfig.isSignInEnabled && hasSignInSession()) {
    return () => resolveIdToken(signInConfig);
  }
  if (!accessToken) return null;
  return async () => accessToken;
}

async function startApp() {
  Object.entries(tabButtons).forEach(([tabName, button]) => button.addEventListener("click", () => showReceiverTab(tabName)));
  setUnseenHoldListener((hasUnseenPendingHold) => { hasUnseenHold = hasUnseenPendingHold; renderInboxTabDot(); });
  startCircleView({
    onAddTapped: () => openOnboarding(ONBOARDING_START.ADD_CONTACT),
    onContactTapped: (entry) => openOnboarding(ONBOARDING_START.EDIT_CONTACT, entry),
  });
  startReadingPane(() => { if (activeUserRole === USER_ROLE.RECEIVER && activeReceiverTab === RECEIVER_TAB.INBOX) applyReadingPaneLayout(); });
  startPidgeChat({ onReportPaymentRequested: () => { showReceiverTab(RECEIVER_TAB.CASES); openReportPaymentForm(); } });
  const signInConfig = await readSignInConfig();
  if (signInConfig.isSignInEnabled) {
    pageElements.signInButton.addEventListener("click", () => startSignIn(signInConfig));
    pageElements.signInButton.hidden = false;
    await completeSignInFromRedirect(signInConfig);
  }
  const resolveConnectionToken = buildConnectionTokenResolver(signInConfig);
  if (resolveConnectionToken === null) {
    showTokenErrorScreen(signInConfig.isSignInEnabled);
    return;
  }
  pageElements.loadingState.hidden = false;
  startPushNotifications();
  connectToPushChannel({
    resolveConnectionToken, onPushMessage: handlePushMessage, onConnectionStatusChange: handleConnectionStatusChange,
  });
}

startApp();
