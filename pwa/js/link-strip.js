// An email's links: inside the body they look like links (blue, orange when flagged), and the strip under the body lists
// each link's visible text with its real domain beside it. Nothing is clickable: hover and long-press show the address.
// Link text, domain and href are attacker-controlled: textContent and title only, never an <a> element or innerHTML.
import { HIDDEN_BY, LINK_KIND, REASON_CODE } from "./constants.js";
import { createElementWithText } from "./dom-helpers.js";
import { showActionStatusMessage } from "./action-status.js";

const LONG_PRESS_MILLISECONDS = 500;
// A link in a plain-text body: Gmail and Apple Mail write "text <href>"; a bare address ends before trailing punctuation.
const PLAIN_TEXT_LINK_PATTERN = /(\s*)<((?:https?|mailto|tel):[^\s<>]+)>|(?:https?:\/\/|www\.)[^\s<>]*[^\s<>.,;:!?'")\]]/gi;
const TRAILING_SLASH_PATTERN = /\/$/;
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

// Hover (the title) on the laptop, a long-press on the phone: the link's full address, never a navigation.
function revealAddressOnHoverAndLongPress(element, href) {
  element.title = href;
  let longPressTimerId = null;
  const cancelLongPress = () => clearTimeout(longPressTimerId);
  element.addEventListener("pointerdown", () => {
    cancelLongPress();
    longPressTimerId = setTimeout(() => showActionStatusMessage(href), LONG_PRESS_MILLISECONDS);
  });
  ["pointerup", "pointercancel", "pointerleave"].forEach((eventName) => element.addEventListener(eventName, cancelLongPress));
}

function buildLinkChipElement(link) {
  const chip = createElementWithText("span", link.flagged ? "link-chip is-mismatch" : "link-chip");
  revealAddressOnHoverAndLongPress(chip, link.href);
  chip.tabIndex = 0;
  chip.setAttribute("role", "text");
  chip.append(createElementWithText("span", "link-text", link.text || "(no text)"),
    createElementWithText("span", "link-domain", describeLinkDestination(link)));
  if (Object.prototype.hasOwnProperty.call(HIDDEN_BY_LABELS, link.hiddenBy)) {
    chip.append(createElementWithText("span", "link-domain link-hidden-by", HIDDEN_BY_LABELS[link.hiddenBy]));
  }
  return chip;
}

function isSameHref(firstHref, secondHref) {
  return firstHref.replace(TRAILING_SLASH_PATTERN, "") === secondHref.replace(TRAILING_SLASH_PATTERN, "");
}

// matchedLink is the email's link with this href (it carries flagged), or undefined for an address only the text part has.
function buildBodyLinkElement(linkLabel, href, matchedLink) {
  const isFlagged = matchedLink !== undefined && matchedLink.flagged;
  const linkElement = createElementWithText("span", isFlagged ? "body-link is-mismatch" : "body-link", linkLabel);
  revealAddressOnHoverAndLongPress(linkElement, href);
  return linkElement;
}

// The body text with each link shown as a link: "text <href>" keeps only the text when it is that link's visible text
// (otherwise the address itself), and a bare address stays as written. Strings are appended as text nodes.
export function buildEmailBodyElement(className, bodyText, links) {
  const bodyElement = createElementWithText("p", className);
  let copiedUpToIndex = 0;
  for (const linkMatch of bodyText.matchAll(PLAIN_TEXT_LINK_PATTERN)) {
    const [matchedText, whitespaceBeforeBracket, bracketedHref] = linkMatch;
    const href = bracketedHref === undefined ? matchedText : bracketedHref;
    const matchedLink = links.find((link) => isSameHref(link.href, href));
    let textBeforeLink = bodyText.slice(copiedUpToIndex, linkMatch.index);
    let linkLabel = href;
    if (bracketedHref !== undefined && matchedLink !== undefined && matchedLink.text && textBeforeLink.endsWith(matchedLink.text)) {
      textBeforeLink = textBeforeLink.slice(0, -matchedLink.text.length);
      linkLabel = matchedLink.text;
    } else if (bracketedHref !== undefined) {
      textBeforeLink += whitespaceBeforeBracket;
    }
    bodyElement.append(textBeforeLink, buildBodyLinkElement(linkLabel, href, matchedLink));
    copiedUpToIndex = linkMatch.index + matchedText.length;
  }
  bodyElement.append(bodyText.slice(copiedUpToIndex));
  return bodyElement;
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
// naming another address (then it is one address-like word), never for hidden, whole-body or attachment links. A listed link
// names its list's attribution (a licence obligation), so that line comes before the mismatch one.
function describeFlaggedLink(link) {
  if (isCodeRunningLink(link)) return "A link in this email runs code instead of opening a page.";
  const linkReasons = Array.isArray(link.reasons) ? link.reasons : [];
  if (linkReasons.includes(REASON_CODE.LINK_LISTED) && typeof link.listedBy === "string" && link.listedBy) {
    return `A link here goes to a site on a scam list (${link.listedBy}).`;
  }
  if (linkReasons.includes(REASON_CODE.LINK_TEXT_HREF_MISMATCH) && link.text && link.domain) {
    return `The link says ${link.text} but goes to ${link.domain}.`;
  }
  return "A link in this email hides where it goes.";
}

export function buildLinkReasonLineElements(links) {
  if (!Array.isArray(links)) return [];
  return links.filter((link) => link.flagged).map((link) => createElementWithText("p", "reason-line", describeFlaggedLink(link)));
}
