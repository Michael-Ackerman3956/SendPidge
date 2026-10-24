// Web Push for the receiver: registers the service worker, shows "Turn on notifications" while permission is undecided,
// and sends this browser's subscription to the server (again on every sync, so a lost row heals itself).
import { VAPID_PUBLIC_KEY } from "./config.js";
import { CLIENT_ACTION } from "./constants.js";
import { sendClientAction } from "./websocket.js";
import { ACTION_STATUS_MESSAGE, showActionStatusMessage } from "./action-status.js";

const SERVICE_WORKER_URL = "sw.js";
const enableButton = document.getElementById("enable-notifications-button");

// iPhone exposes PushManager only in the home-screen app, so a Safari tab never sees the button.
function isWebPushSupported() {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

function decodeBase64UrlToBytes(base64UrlText) {
  const padded = base64UrlText.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(base64UrlText.length / 4) * 4, "=");
  return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
}

async function subscribeAndSendToServer() {
  const registration = await navigator.serviceWorker.ready;
  const existingSubscription = await registration.pushManager.getSubscription();
  const subscription = existingSubscription
    ?? await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: decodeBase64UrlToBytes(VAPID_PUBLIC_KEY) });
  return sendClientAction({ action: CLIENT_ACTION.SAVE_PUSH_SUBSCRIPTION, subscription: subscription.toJSON() });
}

function updateEnableButton() {
  enableButton.hidden = !isWebPushSupported() || Notification.permission !== "default";
}

async function handleUserClickedEnableNotifications() {
  enableButton.disabled = true;
  try {
    const permission = await Notification.requestPermission();
    if (permission === "granted" && await subscribeAndSendToServer()) {
      showActionStatusMessage("Notifications on.");
    } else if (permission !== "granted") {
      showActionStatusMessage("Notifications stay off.");
    } else {
      showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    }
  } catch (subscribeError) {
    console.error("SendPidge: push subscription failed", subscribeError.name);
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
  } finally {
    enableButton.disabled = false;
    updateEnableButton();
  }
}

export function startPushNotifications() {
  if (!isWebPushSupported()) return;
  navigator.serviceWorker.register(SERVICE_WORKER_URL).catch((registrationError) => {
    console.error("SendPidge: service worker registration failed", registrationError.name);
  });
  enableButton.addEventListener("click", handleUserClickedEnableNotifications);
}

// Called after the receiver's sync_state: show the button if undecided, or re-send an already granted subscription.
export function refreshPushSubscriptionAfterSync() {
  if (!isWebPushSupported()) return;
  updateEnableButton();
  if (Notification.permission !== "granted") return;
  subscribeAndSendToServer().catch((subscribeError) => {
    console.error("SendPidge: push subscription refresh failed", subscribeError.name);
  });
}
