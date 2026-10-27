// The link strip under an email body: each link's visible text with its real domain beside it; nothing is clickable.
// Link text, domain and href are attacker-controlled: textContent and title only, never an <a> element or innerHTML.
import { HIDDEN_BY, LINK_KIND, REASON_CODE } from "./constants.js";
import { createElementWithText } from "./dom-helpers.js";
import { showActionStatusMessage } from "./action-status.js";

const LONG_PRESS_MILLISECONDS = 500;
const CODE_RUNNING_LINK_KINDS = new Set([LINK_KIND.SCRIPT, LINK_KIND.DATA]);
const HIDDEN_BY_LABELS = Object.freeze({
  [HIDDEN_BY.IMAGE_ONLY]: "image link",
  [HIDDEN_BY.NO_TEXT]: "hidden",
  [HIDDEN_BY.CSS]: "hidden",
});

function isCodeRunningLink(link) {
  return CODE_RUNNING_LINK_KINDS.has(link.kind);
}

// Where the link goes: its domain, "runs code" for a script or data href, otherwise "not a web address".
function describeLinkDestination(link) {
  if (isCodeRunningLink(link)) return "runs code";
  return link.domain || "not a web address";
}

function buildLinkChipElement(link) {
  const chip = createElementWithText("span", link.flagged ? "link-chip is-mismatch" : "link-chip");
  chip.title = link.href; // hover on the laptop
  chip.tabIndex = 0;
  chip.setAttribute("role", "text");
  chip.append(createElementWithText("span", "link-text", link.text || "(no text)"),
    createElementWithText("span", "link-domain", describeLinkDestination(link)));
  if (Object.prototype.hasOwnProperty.call(HIDDEN_BY_LABELS, link.hiddenBy)) {
    chip.append(createElementWithText("span", "link-domain link-hidden-by", HIDDEN_BY_LABELS[link.hiddenBy]));
  }
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

// One calm sentence per flagged link; shown under the banner. The link's own text is quoted only when the backend found it
// naming another address (then it is one address-like word), never for hidden, whole-body or attachment links.
function describeFlaggedLink(link) {
  if (isCodeRunningLink(link)) return "A link in this email runs code instead of opening a page.";
  const linkReasons = Array.isArray(link.reasons) ? link.reasons : [];
  if (linkReasons.includes(REASON_CODE.LINK_TEXT_HREF_MISMATCH) && link.text && link.domain) {
    return `The link says ${link.text} but goes to ${link.domain}.`;
  }
  return "A link in this email hides where it goes.";
}

export function buildLinkReasonLineElements(links) {
  if (!Array.isArray(links)) return [];
  return links.filter((link) => link.flagged).map((link) => createElementWithText("p", "reason-line", describeFlaggedLink(link)));
}
