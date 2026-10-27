// Grandma's Circle tab (RECEIVER): the people Pidge can ask, from sync_state.circle and every circle_state push. Names come from
// the server: textContent only. No number or address is on the wire; the reach chips say which channels the server has for each
// person. sync_state.contactVerification says how a new contact can be verified on this stack (WhatsApp reply, labelled demo).
import { CHANNEL, RELATIONSHIP_LABELS, VERIFIED_VIA } from "./constants.js";
import { createElementWithText, parseTimestampMilliseconds } from "./dom-helpers.js";

const KNOWN_VERIFIED_VIA = new Set(Object.values(VERIFIED_VIA));
const REACH_CHIP_LABELS = Object.freeze({ [CHANNEL.INAPP]: "App", [CHANNEL.RCS]: "RCS", [CHANNEL.WHATSAPP]: "WA", [CHANNEL.EMAIL]: "Email" });

const circleEntriesByUserId = new Map();
let contactVerificationFlags = { whatsapp: false, mock: false };

const pageElements = {
  topbarTitle: document.getElementById("topbar-title"),
  circleScreen: document.getElementById("circle-screen"),
  circleList: document.getElementById("circle-list"),
  addButton: document.getElementById("circle-add-button"),
};

function isValidCircleEntry(entry) {
  const isValid = entry !== null && typeof entry === "object" && typeof entry.contactUserId === "string"
    && typeof entry.contactName === "string" && KNOWN_VERIFIED_VIA.has(entry.verifiedVia)
    && entry.reach !== null && typeof entry.reach === "object";
  if (!isValid) console.error("SendPidge: circle entry has a missing field or unknown verifiedVia", entry);
  return isValid;
}

function isCodeExpired(entry) {
  const expiresAtMilliseconds = parseTimestampMilliseconds(entry.codeExpiresAt);
  return expiresAtMilliseconds !== null && expiresAtMilliseconds < Date.now();
}

function describeVerification(entry) {
  if (entry.verifiedVia === VERIFIED_VIA.PENDING) return isCodeExpired(entry) ? "Code expired. Add them again" : "Waiting for their code";
  if (entry.verifiedVia === VERIFIED_VIA.SEED) return "Set up for the demo";
  const verifiedAtMilliseconds = parseTimestampMilliseconds(entry.verifiedAt);
  const dayText = verifiedAtMilliseconds === null ? "" : ` ${new Date(verifiedAtMilliseconds).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
  return entry.verifiedVia === VERIFIED_VIA.WHATSAPP ? `Verified${dayText} on WhatsApp` : `Verified${dayText} (demo code)`;
}

function describeContact(entry) {
  const relationshipLabel = RELATIONSHIP_LABELS[entry.relationship];
  return relationshipLabel === undefined ? describeVerification(entry) : `${relationshipLabel} · ${describeVerification(entry)}`;
}

function buildCircleRowElement(entry) {
  const row = createElementWithText("li", "circle-row");
  const avatar = createElementWithText("span", "avatar-initial", (entry.contactName.trim()[0] || "?").toUpperCase());
  avatar.setAttribute("aria-hidden", "true");
  const nameBlock = createElementWithText("div", "circle-name-block");
  nameBlock.append(createElementWithText("p", "circle-name", entry.contactName), createElementWithText("p", "circle-detail", describeContact(entry)));
  const reachRow = createElementWithText("div", "circle-reach");
  Object.entries(REACH_CHIP_LABELS).forEach(([channel, label]) => {
    reachRow.append(createElementWithText("span", entry.reach[channel] === true ? "reach-chip is-on" : "reach-chip", label));
  });
  row.append(avatar, nameBlock, reachRow);
  return row;
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

export function startCircleView(onAddTapped) {
  pageElements.addButton.addEventListener("click", onAddTapped);
}

export function showCircleView() {
  pageElements.topbarTitle.textContent = "Circle";
  pageElements.circleScreen.hidden = false;
}

export function hideCircleView() {
  pageElements.circleScreen.hidden = true;
}
