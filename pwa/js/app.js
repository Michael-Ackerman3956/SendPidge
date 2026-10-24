// SendPidge PWA entry: access token, socket, role routing from sync_state, and push dispatch to the receiver or contact view.
// The role is never stored: every sync_state names it again.
import { ACTION_REJECTED_REASON, CONNECTION_STATUS, PUSH_MESSAGE_TYPE, USER_ROLE } from "./constants.js";
import { connectToPushChannel } from "./websocket.js";
import { refreshPushSubscriptionAfterSync, startPushNotifications } from "./push-notifications.js";
import { ACTION_STATUS_MESSAGE, showActionStatusMessage } from "./action-status.js";
import {
  applyReceiverSyncState, handleEmailStatePush, handleReceiverActionRejected, handleReceiverRequestStatePush,
  hideReceiverView, showReceiverView,
} from "./receiver-view.js";
import {
  applyContactSyncState, handleContactActionRejected, handleContactRequestStatePush, handleVerificationCardPush,
  hideContactView, showContactView,
} from "./contact-view.js";
import { applyPidgeSyncState, handleCaseNotePush, handleCaseStatePush, handlePidgeActionRejected, hidePidgeView, showPidgeView } from "./pidge-view.js";

const ACCESS_TOKEN_STORAGE_KEY = "sendpidge.accessToken";
const ACCESS_TOKEN_QUERY_PARAMETER = "token";
const KNOWN_ACTION_REJECTED_REASONS = new Set(Object.values(ACTION_REJECTED_REASON));

let activeUserRole = null;

const RECEIVER_TAB = Object.freeze({ INBOX: "inbox", PIDGE: "pidge" });
let activeReceiverTab = RECEIVER_TAB.INBOX;

const pageElements = {
  connectionDot: document.getElementById("connection-dot"),
  connectionLabel: document.getElementById("connection-label"),
  tokenError: document.getElementById("token-error"),
  loadingState: document.getElementById("loading-state"),
  receiverTabs: document.getElementById("receiver-tabs"),
  inboxTab: document.getElementById("tab-inbox"),
  pidgeTab: document.getElementById("tab-pidge"),
};

function showReceiverTab(tab) {
  activeReceiverTab = tab;
  const isInbox = tab === RECEIVER_TAB.INBOX;
  pageElements.inboxTab.classList.toggle("is-active", isInbox);
  pageElements.inboxTab.setAttribute("aria-pressed", String(isInbox));
  pageElements.pidgeTab.classList.toggle("is-active", !isInbox);
  pageElements.pidgeTab.setAttribute("aria-pressed", String(!isInbox));
  if (isInbox) {
    hidePidgeView();
    showReceiverView();
  } else {
    hideReceiverView();
    showPidgeView();
  }
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

function handleSyncStatePush(pushMessage) {
  switch (pushMessage.role) {
    case USER_ROLE.RECEIVER:
      if (activeUserRole === USER_ROLE.CONTACT) hideContactView();
      activeUserRole = USER_ROLE.RECEIVER;
      pageElements.loadingState.hidden = true;
      pageElements.receiverTabs.hidden = false;
      if (activeReceiverTab === RECEIVER_TAB.INBOX) showReceiverView();
      applyReceiverSyncState(pushMessage.emails);
      applyPidgeSyncState(pushMessage.notes ?? [], pushMessage.cases ?? []);
      refreshPushSubscriptionAfterSync();
      break;
    case USER_ROLE.CONTACT:
      if (activeUserRole === USER_ROLE.RECEIVER) { hideReceiverView(); hidePidgeView(); pageElements.receiverTabs.hidden = true; }
      activeUserRole = USER_ROLE.CONTACT;
      pageElements.loadingState.hidden = true;
      showContactView();
      applyContactSyncState(pushMessage.requests);
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
  const isAlreadyRecorded = pushMessage.reason === ACTION_REJECTED_REASON.NOT_APPLIED;
  showActionStatusMessage(isAlreadyRecorded ? ACTION_STATUS_MESSAGE.ALREADY_RECORDED : ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
  if (activeUserRole === USER_ROLE.RECEIVER) {
    handleReceiverActionRejected(requestId);
    handlePidgeActionRejected();
  } else {
    handleContactActionRejected(requestId);
  }
}

// Which role each role-specific push belongs to; a push for the other role (or before sync_state) is ignored with a warning.
const PUSH_MESSAGE_ROLES = Object.freeze({
  [PUSH_MESSAGE_TYPE.EMAIL_STATE]: [USER_ROLE.RECEIVER],
  [PUSH_MESSAGE_TYPE.VERIFICATION_CARD]: [USER_ROLE.CONTACT],
  [PUSH_MESSAGE_TYPE.REQUEST_STATE]: [USER_ROLE.RECEIVER, USER_ROLE.CONTACT],
  [PUSH_MESSAGE_TYPE.ACTION_REJECTED]: [USER_ROLE.RECEIVER, USER_ROLE.CONTACT],
  [PUSH_MESSAGE_TYPE.CASE_NOTE]: [USER_ROLE.RECEIVER],
  [PUSH_MESSAGE_TYPE.CASE_STATE]: [USER_ROLE.RECEIVER],
  [PUSH_MESSAGE_TYPE.CONTACT_NOTICE]: [USER_ROLE.CONTACT],
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
      if (activeReceiverTab === RECEIVER_TAB.PIDGE) showReceiverTab(RECEIVER_TAB.INBOX); // a new hold must be seen
      handleEmailStatePush(pushMessage.email);
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
    case PUSH_MESSAGE_TYPE.CONTACT_NOTICE:
      if (typeof pushMessage.text === "string") showActionStatusMessage(pushMessage.text);
      break;
    default:
      console.warn("SendPidge: ignoring push of unknown type", pushMessageType);
  }
}

function updateConnectionDot(connectionStatus) {
  const isConnected = connectionStatus === CONNECTION_STATUS.CONNECTED;
  pageElements.connectionDot.classList.toggle("is-connected", isConnected);
  pageElements.connectionLabel.textContent = isConnected ? "Connected" : "Reconnecting";
}

function startApp() {
  pageElements.inboxTab.addEventListener("click", () => showReceiverTab(RECEIVER_TAB.INBOX));
  pageElements.pidgeTab.addEventListener("click", () => showReceiverTab(RECEIVER_TAB.PIDGE));
  const accessToken = resolveAccessToken();
  if (!accessToken) {
    pageElements.tokenError.hidden = false;
    return;
  }
  pageElements.loadingState.hidden = false;
  startPushNotifications();
  connectToPushChannel({ accessToken, onPushMessage: handlePushMessage, onConnectionStatusChange: updateConnectionDot });
}

startApp();
