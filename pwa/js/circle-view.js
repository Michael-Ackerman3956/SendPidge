// Grandma's Circle tab (RECEIVER): the people Pidge can ask, from sync_state.circle and every circle_state push. Names come from
// the server: textContent only. Each entry carries the numbers and emails she typed (reachPoints, her own sockets only) for the
// edit form; the rows show none of them, only reach chips for the channels Pidge asks on. Tapping a row opens the edit form,
// except the home's row (verifiedVia org): it is not a button and is never edited.
// sync_state.contactVerification says how a new contact can be verified on this stack (WhatsApp reply, labelled demo).
import { CHANNEL, RELATIONSHIP_LABELS, VERIFIED_VIA } from "./constants.js";
import { isCodeExpired } from "./contact-form-draft.js";
import { createElementWithText, parseTimestampMilliseconds } from "./dom-helpers.js";

const KNOWN_VERIFIED_VIA = new Set(Object.values(VERIFIED_VIA));
const REACH_CHIP_LABELS = Object.freeze({ [CHANNEL.INAPP]: "App", [CHANNEL.RCS]: "RCS", [CHANNEL.WHATSAPP]: "WA", [CHANNEL.EMAIL]: "Email" });

const circleEntriesByUserId = new Map();
let contactVerificationFlags = { whatsapp: false, mock: false };
let onContactTapped = () => {};

const pageElements = {
  topbarTitle: document.getElementById("topbar-title"),
  circleScreen: document.getElementById("circle-screen"),
  circleList: document.getElementById("circle-list"),
  addButton: document.getElementById("circle-add-button"),
};

function isValidCircleEntry(entry) {
  const isValid = entry !== null && typeof entry === "object" && typeof entry.contactUserId === "string"
    && typeof entry.contactName === "string" && KNOWN_VERIFIED_VIA.has(entry.verifiedVia)
    && entry.reach !== null && typeof entry.reach === "object" && Array.isArray(entry.reachPoints);
  if (!isValid) console.error("SendPidge: circle entry has a missing field or unknown verifiedVia", entry);
  return isValid;
}

function describeVerification(entry) {
  if (entry.verifiedVia === VERIFIED_VIA.PENDING) return isCodeExpired(entry, Date.now()) ? "Code expired. Tap, then Save" : "Waiting for their code";
  if (entry.verifiedVia === VERIFIED_VIA.SEED) return "Set up for the demo";
  if (entry.verifiedVia === VERIFIED_VIA.ORG) return "Your home";
  const verifiedAtMilliseconds = parseTimestampMilliseconds(entry.verifiedAt);
  const dayText = verifiedAtMilliseconds === null ? "" : ` ${new Date(verifiedAtMilliseconds).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
  return entry.verifiedVia === VERIFIED_VIA.WHATSAPP ? `Verified${dayText} on WhatsApp` : `Verified${dayText} (demo code)`;
}

function describeContact(entry) {
  const relationshipLabel = RELATIONSHIP_LABELS[entry.relationship];
  return relationshipLabel === undefined ? describeVerification(entry) : `${relationshipLabel} · ${describeVerification(entry)}`;
}

function buildCircleRowElement(entry) {
  const item = createElementWithText("li", "circle-item");
  const isOrgRow = entry.verifiedVia === VERIFIED_VIA.ORG;
  const row = createElementWithText(isOrgRow ? "div" : "button", "circle-row");
  if (!isOrgRow) {
    row.type = "button";
    row.addEventListener("click", () => onContactTapped(entry));
  }
  const avatar = createElementWithText("span", "avatar-initial", (entry.contactName.trim()[0] || "?").toUpperCase());
  avatar.setAttribute("aria-hidden", "true");
  const nameBlock = createElementWithText("span", "circle-name-block");
  nameBlock.append(createElementWithText("span", "circle-name", entry.contactName), createElementWithText("span", "circle-detail", describeContact(entry)));
  const reachRow = createElementWithText("span", "circle-reach");
  Object.entries(REACH_CHIP_LABELS).forEach(([channel, label]) => {
    const isReached = entry.reach[channel] === true;
    const chip = createElementWithText("span", isReached ? "reach-chip is-on" : "reach-chip", label);
    if (!isReached) chip.setAttribute("aria-hidden", "true"); // the button reads only the channels that reach them
    reachRow.append(chip);
  });
  row.append(avatar, nameBlock, reachRow);
  item.append(row);
  return item;
}

function renderCircle() {
  pageElements.circleList.replaceChildren(...[...circleEntriesByUserId.values()].map(buildCircleRowElement));
}

export function applyCircleSyncState(circle, contactVerification) {
  if (!Array.isArray(circle)) {
    console.error("SendPidge: receiver sync_state has no circle array", circle);
    return;
  }
  if (typeof contactVerification?.whatsapp !== "boolean" || typeof contactVerification.mock !== "boolean") {
    console.error("SendPidge: receiver sync_state has no contactVerification flags", contactVerification);
  } else {
    contactVerificationFlags = { whatsapp: contactVerification.whatsapp, mock: contactVerification.mock };
  }
  applyCircleStatePush(circle);
}

export function applyCircleStatePush(circle) {
  if (!Array.isArray(circle)) {
    console.error("SendPidge: circle_state has no circle array", circle);
    return;
  }
  circleEntriesByUserId.clear();
  circle.filter(isValidCircleEntry).forEach((entry) => circleEntriesByUserId.set(entry.contactUserId, entry));
  renderCircle();
}

export function getContactVerificationFlags() {
  return contactVerificationFlags;
}

export function listCircleEntries() {
  return [...circleEntriesByUserId.values()];
}

export function startCircleView({ onAddTapped, onContactTapped: onContactTappedCallback }) {
  pageElements.addButton.addEventListener("click", onAddTapped);
  onContactTapped = onContactTappedCallback;
}

export function showCircleView() {
  pageElements.topbarTitle.textContent = "Circle";
  pageElements.circleScreen.hidden = false;
}

export function hideCircleView() {
  pageElements.circleScreen.hidden = true;
}
