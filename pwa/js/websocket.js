// Live push channel: connects with a token resolved before every open, reconnects with backoff, sends SYNC on every open,
// parses JSON and hands each message on. sendClientAction writes one client action to the open socket.
import { WEBSOCKET_URL } from "./config.js";
import { CLIENT_ACTION, CONNECTION_STATUS } from "./constants.js";

const INITIAL_RECONNECT_DELAY_MS = 1000;
const MAXIMUM_RECONNECT_DELAY_MS = 10000;

let openSocketForActions = null;

function buildWebSocketUrlWithToken(connectionToken) {
  return `${WEBSOCKET_URL}?token=${encodeURIComponent(connectionToken)}`;
}

function parsePushMessageOrReportError(rawMessageText) {
  try {
    return JSON.parse(rawMessageText);
  } catch (parseError) {
    console.error("SendPidge: unparseable push message", parseError, rawMessageText);
    return null;
  }
}

// Opens the socket and keeps it open. resolveConnectionToken (async, a token or null) runs before every open; null stops
// reconnecting and reports CONNECTION_STATUS.SIGNED_OUT. onPushMessage gets every parsed message; onConnectionStatusChange
// gets CONNECTION_STATUS values.
export function connectToPushChannel({ resolveConnectionToken, onPushMessage, onConnectionStatusChange }) {
  let reconnectDelayMs = INITIAL_RECONNECT_DELAY_MS;

  function scheduleReconnectWithBackoff() {
    onConnectionStatusChange(CONNECTION_STATUS.RECONNECTING);
    const currentDelayMs = reconnectDelayMs;
    reconnectDelayMs = Math.min(reconnectDelayMs * 2, MAXIMUM_RECONNECT_DELAY_MS);
    setTimeout(openSocket, currentDelayMs);
  }

  function handleSocketMessage(messageEvent) {
    const pushMessage = parsePushMessageOrReportError(messageEvent.data);
    if (pushMessage === null) {
      return;
    }
    console.info("SendPidge: push received", pushMessage);
    onPushMessage(pushMessage);
  }

  async function openSocket() {
    onConnectionStatusChange(CONNECTION_STATUS.RECONNECTING);
    const connectionToken = await resolveConnectionToken();
    if (connectionToken === null) {
      console.warn("SendPidge: no connection token; not reconnecting");
      onConnectionStatusChange(CONNECTION_STATUS.SIGNED_OUT);
      return;
    }
    let socket;
    try {
      socket = new WebSocket(buildWebSocketUrlWithToken(connectionToken));
    } catch (socketCreationError) {
      // The error never includes the URL, so the token is not logged.
      console.error("SendPidge: could not open WebSocket", socketCreationError.name);
      scheduleReconnectWithBackoff();
      return;
    }
    socket.addEventListener("open", () => {
      reconnectDelayMs = INITIAL_RECONNECT_DELAY_MS;
      openSocketForActions = socket;
      onConnectionStatusChange(CONNECTION_STATUS.CONNECTED);
      sendClientAction({ action: CLIENT_ACTION.SYNC });
    });
    socket.addEventListener("message", handleSocketMessage);
    socket.addEventListener("close", (closeEvent) => {
      console.warn("SendPidge: WebSocket closed", closeEvent.code);
      if (openSocketForActions === socket) {
        openSocketForActions = null;
      }
      scheduleReconnectWithBackoff();
    });
  }

  openSocket();
}

// Sends one client action as JSON. Returns false (and warns) when the socket is not open, so the caller can tell the user.
export function sendClientAction(clientAction) {
  if (openSocketForActions === null || openSocketForActions.readyState !== WebSocket.OPEN) {
    console.warn("SendPidge: socket not open; client action not sent", clientAction.action);
    return false;
  }
  try {
    openSocketForActions.send(JSON.stringify(clientAction));
  } catch (sendError) {
    console.error("SendPidge: client action could not be sent", clientAction.action, sendError.name);
    return false;
  }
  console.info("SendPidge: client action sent", clientAction.action);
  return true;
}
