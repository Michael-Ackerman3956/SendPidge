// The link strip under an email body: each link's visible text with its real domain beside it; nothing is clickable.
// Link text, domain and href are attacker-controlled: textContent and title only, never an <a> element or innerHTML.
import { createElementWithText } from "./dom-helpers.js";
import { showActionStatusMessage } from "./action-status.js";

const LONG_PRESS_MILLISECONDS = 500;

function buildLinkChipElement(link) {
  const chip = createElementWithText("span", link.flagged ? "link-chip is-mismatch" : "link-chip");
  chip.title = link.href; // hover on the laptop
  chip.tabIndex = 0;
  chip.setAttribute("role", "text");
  chip.append(createElementWithText("span", "link-text", link.text || "(no text)"),
    createElementWithText("span", "link-domain", link.domain || "not a web address"));
  let longPressTimerId = null;
  const cancelLongPress = () => clearTimeout(longPressTimerId);
  chip.addEventListener("pointerdown", () => {
    cancelLongPress();
    longPressTimerId = setTimeout(() => showActionStatusMessage(link.href), LONG_PRESS_MILLISECONDS); // long-press on the phone
  });
  ["pointerup", "pointercancel", "pointerleave"].forEach((eventName) => chip.addEventListener(eventName, cancelLongPress));
  return chip;
}

// Null when the email has no links, so plain emails render exactly as before.
export function buildLinkStripElement(links) {
  if (!Array.isArray(links) || links.length === 0) return null;
  const strip = createElementWithText("div", "link-strip");
  strip.setAttribute("aria-label", "Links in this email");
  strip.append(...links.map(buildLinkChipElement));
  return strip;
}

// One calm sentence per flagged link that names a different place than it goes to; shown under the banner.
export function buildLinkReasonLineElements(links) {
  if (!Array.isArray(links)) return [];
  return links.filter((link) => link.flagged).map((link) => {
    const sentence = link.text && link.domain
      ? `The link says ${link.text} but goes to ${link.domain}.`
      : "A link in this email hides where it goes.";
    return createElementWithText("p", "reason-line", sentence);
  });
}
