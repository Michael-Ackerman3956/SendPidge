// One conversation under an open inbox row (RECEIVER): the thread's in and out rows oldest first, a warning above Reply when a
// message in it was not from them, and Reply only when the server lists the thread's address in a circle entry's sendTo.
// Every text here came from the server or from a sender: textContent only.
import { CHANNEL, MESSAGE_DIRECTION, PROVIDER_STATUS } from "./constants.js";
import { createElementWithText } from "./dom-helpers.js";

const PROVIDER_STATUS_LABELS = Object.freeze({
  [PROVIDER_STATUS.SENDING]: "Sending",
  [PROVIDER_STATUS.SENT]: "Sent",
  [PROVIDER_STATUS.FAILED]: "Not sent",
});
const NOT_FROM_THEM_TEXT = "A message here was not from them.";

// Mirrors lambda/shared/inbox_rows.py thread_key_for: an email address compares case-insensitively (casefold there,
// toLowerCase here: the same for ASCII addresses), a number exactly.
export function buildThreadKey(channel, address) {
  return `${channel}#${channel === CHANNEL.EMAIL ? address.toLowerCase() : address}`;
}

// The chip on Grandma's own sent row: where its send stands.
export function buildProviderStatusChipElement(providerStatus) {
  const chipClassName = providerStatus === PROVIDER_STATUS.FAILED ? "status-chip is-warn" : "status-chip";
  return createElementWithText("span", chipClassName, PROVIDER_STATUS_LABELS[providerStatus]);
}

function buildThreadItemElement(row, isOpenRow, formatTime) {
  const isOutgoing = row.direction === MESSAGE_DIRECTION.OUT;
  const item = createElementWithText("li", isOutgoing ? "thread-item is-out" : "thread-item");
  if (isOpenRow) item.setAttribute("aria-current", "true");
  const timeText = formatTime(row.receivedAt);
  const metaText = isOutgoing ? `${timeText} · ${PROVIDER_STATUS_LABELS[row.providerStatus]}` : timeText;
  item.append(createElementWithText("p", "thread-text", row.textBody), createElementWithText("span", "thread-meta", metaText));
  return item;
}

// threadRows: every row of the thread, oldest first, the open one included (listed only when there is more than it).
// replyTarget: what Reply preselects in the composer, or null for no Reply. Returns null when there is nothing to show.
export function buildThreadElement({ threadRows, openRowId, hasNotFromThemRow, isHeld, replyTarget, formatTime, onReplyTapped }) {
  const threadElement = createElementWithText("section", "thread");
  threadElement.setAttribute("aria-label", "Conversation");
  if (threadRows.length > 1) {
    const threadList = createElementWithText("ol", "thread-list");
    threadList.append(...threadRows.map((row) => buildThreadItemElement(row, row.emailId === openRowId, formatTime)));
    threadElement.append(threadList);
  }
  if (replyTarget !== null) {
    if (hasNotFromThemRow) {
      const warnBanner = createElementWithText("p", "warn-banner", NOT_FROM_THEM_TEXT);
      warnBanner.setAttribute("role", "status");
      threadElement.append(warnBanner);
    }
    const replyButton = createElementWithText("button", "contact-secondary-button", "Reply");
    replyButton.type = "button";
    replyButton.disabled = isHeld; // a pending hold in the thread: pidge-send would refuse it as thread_held
    replyButton.addEventListener("click", () => onReplyTapped(replyTarget));
    threadElement.append(replyButton);
  }
  return threadElement.childElementCount === 0 ? null : threadElement;
}
