// Live push channel: connects with the access token, reconnects with backoff, parses JSON and hands each message on.
import { WEBSOCKET_URL } from "./config.js";
import { CONNECTION_STATUS } from "./constants.js";

const INITIAL_RECONNECT_DELAY_MS = 1000;
const MAXIMUM_RECONNECT_DELAY_MS = 10000;

function buildWebSocketUrlWithToken(accessToken) {
  return `${WEBSOCKET_URL}?token=${encodeURIComponent(accessToken)}`;
}

function parsePushMessageOrReportError(rawMessageText) {
  try {
    return JSON.parse(rawMessageText);
  } catch (parseError) {
    console.error("SendPidge: unparseable push message", parseError, rawMessageText);
    return null;
  }
}

// Opens the socket and keeps it open. onPushMessage gets every parsed message; onConnectionStatusChange gets CONNECTION_STATUS values.
export function connectToPushChannel({ accessToken, onPushMessage, onConnectionStatusChange }) {
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

  function openSocket() {
    onConnectionStatusChange(CONNECTION_STATUS.RECONNECTING);
    let socket;
    try {
      socket = new WebSocket(buildWebSocketUrlWithToken(accessToken));
    } catch (socketCreationError) {
      // The error never includes the URL, so the token is not logged.
      console.error("SendPidge: could not open WebSocket", socketCreationError.name);
      scheduleReconnectWithBackoff();
      return;
    }
    socket.addEventListener("open", () => {
      reconnectDelayMs = INITIAL_RECONNECT_DELAY_MS;
      onConnectionStatusChange(CONNECTION_STATUS.CONNECTED);
    });
    socket.addEventListener("message", handleSocketMessage);
    socket.addEventListener("close", (closeEvent) => {
      console.warn("SendPidge: WebSocket closed", closeEvent.code);
      scheduleReconnectWithBackoff();
    });
  }

  openSocket();
}
