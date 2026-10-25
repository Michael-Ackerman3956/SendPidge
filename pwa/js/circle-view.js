// Grandma's Circle tab (RECEIVER): the people Pidge can ask, from sync_state.circle. Names come from the server: textContent only.
// No number or address is on the wire; the reach chips say which channels the server has for each person.
import { VERIFIED_VIA } from "./constants.js";
import { createElementWithText, parseTimestampMilliseconds } from "./dom-helpers.js";

const KNOWN_VERIFIED_VIA = new Set(Object.values(VERIFIED_VIA));
const REACH_CHIP_LABELS = Object.freeze({ inapp: "App", rcs: "RCS", whatsapp: "WA" });

const circleEntriesByUserId = new Map();

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

function describeVerification(entry) {
  if (entry.verifiedVia === VERIFIED_VIA.SEED) return "Set up for the demo";
  const verifiedAtMilliseconds = parseTimestampMilliseconds(entry.verifiedAt);
  const dayText = verifiedAtMilliseconds === null ? "" : ` ${new Date(verifiedAtMilliseconds).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
  return `Verified${dayText} (demo code)`;
}

function buildCircleRowElement(entry) {
  const row = createElementWithText("li", "circle-row");
  const avatar = createElementWithText("span", "avatar-initial", (entry.contactName.trim()[0] || "?").toUpperCase());
  avatar.setAttribute("aria-hidden", "true");
  const nameBlock = createElementWithText("div", "circle-name-block");
  nameBlock.append(createElementWithText("p", "circle-name", entry.contactName), createElementWithText("p", "circle-detail", describeVerification(entry)));
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

export function applyCircleSyncState(circle) {
  if (!Array.isArray(circle)) {
    console.error("SendPidge: receiver sync_state has no circle array", circle);
    return;
  }
  circleEntriesByUserId.clear();
  circle.filter(isValidCircleEntry).forEach((entry) => circleEntriesByUserId.set(entry.contactUserId, entry));
  renderCircle();
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
