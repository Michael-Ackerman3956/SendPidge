// One calm status line under the top bar for refused or unsent client actions. The element is an always-present live region.

export const ACTION_STATUS_MESSAGE = Object.freeze({
  ALREADY_RECORDED: "That answer was already recorded.",
  GENERIC_FAILURE: "Something went wrong. Try again.",
});

const actionStatusElement = document.getElementById("action-status");

export function showActionStatusMessage(messageText) {
  actionStatusElement.textContent = messageText;
}

export function clearActionStatusMessage() {
  actionStatusElement.textContent = "";
}
