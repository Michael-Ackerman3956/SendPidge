// An org admin's screen (ORG_ADMIN): the side panel (the receiver's rail, with Members and Messages in place of her four tabs,
// and "Admin" as the signed-in name), and the Members tab: the office's "Was this you?" cards on the contact screen, then the
// home's dashboard under the home's name in the top bar: resident count and when the projection last ran (amber when stale), tiles
// counting members by safety state and the coverage counts, the open items to follow up (one Resolved button each), every
// member with tags, what they share, their last event and a state chip, and the members to call; the Updated text is
// re-rendered every minute. The Messages tab is org-messages-view.js.
// Every name, tag and key comes from the server: textContent only. Resolved sends org_resolve; success has no reply, the
// projection's org_changed re-sync (app.js) shows it.
import { ACTION_STATUS_MESSAGE, clearActionStatusMessage, showActionStatusMessage } from "./action-status.js";
import { CLIENT_ACTION, MEMBER_SAFETY_STATE, ORG_VISIBILITY } from "./constants.js";
import { applyContactSyncState, hideContactView, showContactView } from "./contact-view.js";
import { createElementWithText, formatTimestampForDisplay, parseTimestampMilliseconds } from "./dom-helpers.js";
import { applyOrgBroadcastState, isValidBroadcast, resetOrgMessagesView, startOrgMessagesView } from "./org-messages-view.js";
import { sendClientAction } from "./websocket.js";

const ORG_TAB = Object.freeze({ MEMBERS: "members", MESSAGES: "messages" });
const ADMIN_DISPLAY_NAME = "Admin"; // no admin row holds a name

// Each state's chip: its label and the tone class org.css colours it with.
const MEMBER_SAFETY_STATE_DISPLAY = Object.freeze({
  [MEMBER_SAFETY_STATE.NOT_SHARED]: { label: "Not shared", toneClass: "is-muted" },
  [MEMBER_SAFETY_STATE.NOTHING_OPEN]: { label: "Nothing open", toneClass: "is-good" },
  [MEMBER_SAFETY_STATE.HELD]: { label: "Held", toneClass: "is-held" },
  [MEMBER_SAFETY_STATE.HELD_NO_ANSWER]: { label: "No answer", toneClass: "is-held" },
  [MEMBER_SAFETY_STATE.CONFIRMED_NOT_ME]: { label: "Not me", toneClass: "is-threat" },
  [MEMBER_SAFETY_STATE.WARNED]: { label: "Warned", toneClass: "is-warned" },
  [MEMBER_SAFETY_STATE.CASE_OPEN]: { label: "Case open", toneClass: "is-case" },
  [MEMBER_SAFETY_STATE.CONTACT_UNREACHABLE]: { label: "Can't reach contact", toneClass: "is-neutral" },
});
const KNOWN_MEMBER_SAFETY_STATES = new Set(Object.values(MEMBER_SAFETY_STATE));
// The Shares cell: what the member's MEMBER# coverage fact lets the office see.
const ORG_VISIBILITY_LABELS = Object.freeze({
  [ORG_VISIBILITY.NONE]: "Nothing",
  [ORG_VISIBILITY.ALERTS_ONLY]: "Alerts",
  [ORG_VISIBILITY.ALERTS_AND_CASES]: "Alerts and cases",
});
const KNOWN_ORG_VISIBILITIES = new Set(Object.values(ORG_VISIBILITY));
const NO_LAST_EVENT_TEXT = "—";
// The first tiles count members[].state, in the prototype's order; Held also counts a hold nobody answered.
const SAFETY_STATE_TILES = Object.freeze([
  { label: "Nothing open", toneClass: "is-good", states: [MEMBER_SAFETY_STATE.NOTHING_OPEN] },
  { label: "Held", toneClass: "is-held", states: [MEMBER_SAFETY_STATE.HELD, MEMBER_SAFETY_STATE.HELD_NO_ANSWER] },
  { label: "Not me", toneClass: "is-threat", states: [MEMBER_SAFETY_STATE.CONFIRMED_NOT_ME] },
  { label: "Case open", toneClass: "is-threat", states: [MEMBER_SAFETY_STATE.CASE_OPEN] },
  { label: "Not shared", toneClass: "is-muted", states: [MEMBER_SAFETY_STATE.NOT_SHARED] },
]);
// Every org.coverage key; "enrolled" is the heading's resident count, the other three are tiles after the state tiles.
const COVERAGE_KEYS = Object.freeze(["enrolled", "withVerifiedContact", "visibilityChosen", "seenInLastSevenDays"]);
const COVERAGE_TILES = Object.freeze([
  ["withVerifiedContact", "Verified contact"],
  ["visibilityChosen", "Chose sharing"],
  ["seenInLastSevenDays", "Seen in 7 days"],
]);
// STALE_AFTER in lambda/shared/org_view.py (the server's isStale rule): keep the two equal.
const PROJECTION_STALE_AFTER_MS = 15 * 60 * 1000;
const UPDATED_LINE_REFRESH_INTERVAL_MS = 60 * 1000;

const pageElements = {
  topbarTitle: document.getElementById("topbar-title"),
  sidePanel: document.getElementById("receiver-tabs"),
  receiverTabButtons: ["tab-inbox", "tab-cases", "tab-circle", "tab-pidge", "tab-settings"].map((buttonId) => document.getElementById(buttonId)),
  railMeInitial: document.getElementById("rail-me-initial"),
  railMeName: document.getElementById("rail-me-name"),
  messagesScreen: document.getElementById("org-messages-screen"),
  orgScreen: document.getElementById("org-screen"),
  residentCount: document.getElementById("org-resident-count"),
  updatedLine: document.getElementById("org-updated"),
  statTiles: document.getElementById("org-stats"),
  followUpSection: document.getElementById("org-follow-up"),
  followUpList: document.getElementById("org-follow-up-list"),
  memberTable: document.getElementById("org-member-table"),
  memberRows: document.getElementById("org-member-rows"),
  orgEmptyMessage: document.getElementById("org-empty-message"),
  callListSection: document.getElementById("org-call-list"),
  callListRows: document.getElementById("org-call-list-rows"),
};
const orgTabButtons = Object.freeze({
  [ORG_TAB.MEMBERS]: document.getElementById("tab-org-members"),
  [ORG_TAB.MESSAGES]: document.getElementById("tab-org-messages"),
});
const statValueElements = buildStatTiles();
// A tapped Resolved stays aria-disabled until a re-sync no longer lists its item or the action is rejected.
const sourceKeysWithResolveInFlight = new Set();
let shownOrg = null; // the last valid org rendered, re-read by the Updated line refresh
let updatedLineRefreshIntervalId = null;
let activeOrgTab = ORG_TAB.MEMBERS;

function buildStatTile(label, toneClass) {
  const statValue = createElementWithText("b", "org-stat-value");
  const statTile = createElementWithText("li", `org-stat ${toneClass}`);
  statTile.append(statValue, createElementWithText("span", "org-stat-label", label));
  pageElements.statTiles.append(statTile);
  return statValue;
}

// Built once into the static list in index.html: the state tiles, then the coverage tiles.
function buildStatTiles() {
  return {
    safetyStateValues: SAFETY_STATE_TILES.map((tile) => buildStatTile(tile.label, tile.toneClass)),
    coverageValues: new Map(COVERAGE_TILES.map(([coverageKey, label]) => [coverageKey, buildStatTile(label, "is-neutral")])),
  };
}

function isTimestampText(value) {
  return typeof value === "string" && parseTimestampMilliseconds(value) !== null;
}

function isValidCoverage(coverage) {
  return coverage !== null && typeof coverage === "object" && COVERAGE_KEYS.every((coverageKey) => Number.isInteger(coverage[coverageKey]));
}

// A missing isStale or lastProjectedAt refuses the whole payload: it never reads as fresh.
function isValidOrg(org) {
  const isValid = org !== null && typeof org === "object" && typeof org.orgName === "string" && Array.isArray(org.members)
    && Array.isArray(org.actionable) && Array.isArray(org.callList) && Array.isArray(org.broadcasts) && typeof org.isStale === "boolean"
    && (org.lastProjectedAt === null || isTimestampText(org.lastProjectedAt)) && isValidCoverage(org.coverage);
  if (!isValid) console.error("SendPidge: org_admin sync_state has a missing or malformed org field", org);
  return isValid;
}

function isValidNamedMember(member) {
  return member !== null && typeof member === "object" && typeof member.memberRef === "string" && typeof member.residentName === "string";
}

function isValidOrgMember(member) {
  const isValid = isValidNamedMember(member) && Array.isArray(member.tags) && member.tags.every((tag) => typeof tag === "string")
    && KNOWN_MEMBER_SAFETY_STATES.has(member.state) && KNOWN_ORG_VISIBILITIES.has(member.shares)
    && (member.lastEventAt === null || isTimestampText(member.lastEventAt));
  if (!isValid) console.error("SendPidge: org member has a missing field or unknown state", member);
  return isValid;
}

function isValidActionableItem(actionableItem) {
  const isValid = isValidNamedMember(actionableItem) && typeof actionableItem.sourceKey === "string"
    && KNOWN_MEMBER_SAFETY_STATES.has(actionableItem.state) && isTimestampText(actionableItem.at);
  if (!isValid) console.error("SendPidge: org follow-up item has a missing field or unknown state", actionableItem);
  return isValid;
}

function isValidCallListMember(member) {
  const isValid = isValidNamedMember(member);
  if (!isValid) console.error("SendPidge: org call list member has a missing field", member);
  return isValid;
}

// Rows that fail validation are left out and the status line says something went wrong; the rest still show.
function keepValidRows(rows, isValidRow) {
  const validRows = rows.filter(isValidRow);
  if (validRows.length !== rows.length) showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
  return validRows;
}

// Called only with a validated state, so the lookup always finds its display.
function buildStateChipElement(state) {
  const stateDisplay = MEMBER_SAFETY_STATE_DISPLAY[state];
  return createElementWithText("span", `org-chip ${stateDisplay.toneClass}`, stateDisplay.label);
}

// The resident's initial in a round avatar, the name, and the tags under it when there are any.
function buildPersonElement(residentName, tags) {
  const person = createElementWithText("div", "org-person");
  const avatar = createElementWithText("span", "org-avatar", Array.from(residentName)[0]?.toUpperCase());
  avatar.setAttribute("aria-hidden", "true");
  const names = createElementWithText("div", "org-person-names");
  names.append(createElementWithText("span", "org-person-name", residentName));
  if (tags.length > 0) names.append(createElementWithText("span", "org-person-tags", tags.join(", ")));
  person.append(avatar, names);
  return person;
}

function buildMemberRowElement(member) {
  const row = document.createElement("tr");
  const personCell = document.createElement("td");
  personCell.append(buildPersonElement(member.residentName, member.tags));
  const sharesCell = createElementWithText("td", "org-member-detail", ORG_VISIBILITY_LABELS[member.shares]);
  const lastEventText = member.lastEventAt === null ? NO_LAST_EVENT_TEXT : formatTimestampForDisplay(member.lastEventAt);
  const lastEventCell = createElementWithText("td", "org-member-detail", lastEventText);
  const stateCell = document.createElement("td");
  stateCell.append(buildStateChipElement(member.state));
  row.append(personCell, sharesCell, lastEventCell, stateCell);
  return row;
}

function handleUserTappedResolved(actionableItem, resolvedButton) {
  if (sourceKeysWithResolveInFlight.has(actionableItem.sourceKey)) return;
  const isSent = sendClientAction({
    action: CLIENT_ACTION.ORG_RESOLVE, memberRef: actionableItem.memberRef, sourceKey: actionableItem.sourceKey,
  });
  if (!isSent) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  clearActionStatusMessage();
  sourceKeysWithResolveInFlight.add(actionableItem.sourceKey);
  resolvedButton.setAttribute("aria-disabled", "true");
}

function buildActionableRowElement(actionableItem) {
  const stateLabel = MEMBER_SAFETY_STATE_DISPLAY[actionableItem.state].label;
  const row = createElementWithText("li", "org-log-row");
  const resolvedButton = createElementWithText("button", "contact-secondary-button org-resolved-button", "Resolved");
  resolvedButton.type = "button";
  resolvedButton.setAttribute("aria-label", `Resolved, ${actionableItem.residentName}, ${stateLabel}`);
  resolvedButton.setAttribute("aria-disabled", String(sourceKeysWithResolveInFlight.has(actionableItem.sourceKey)));
  resolvedButton.addEventListener("click", () => handleUserTappedResolved(actionableItem, resolvedButton));
  row.append(
    createElementWithText("time", "org-log-time", formatTimestampForDisplay(actionableItem.at)),
    createElementWithText("span", "org-log-who", actionableItem.residentName),
    buildStateChipElement(actionableItem.state),
    resolvedButton,
  );
  return row;
}

function renderActionableList(actionableItems) {
  const listedSourceKeys = new Set(actionableItems.map((actionableItem) => actionableItem.sourceKey));
  [...sourceKeysWithResolveInFlight].filter((sourceKey) => !listedSourceKeys.has(sourceKey))
    .forEach((sourceKey) => sourceKeysWithResolveInFlight.delete(sourceKey));
  pageElements.followUpList.replaceChildren(...actionableItems.map(buildActionableRowElement));
  pageElements.followUpSection.hidden = actionableItems.length === 0;
}

function renderCallList(callListMembers) {
  pageElements.callListRows.replaceChildren(...callListMembers.map((member) => {
    const row = createElementWithText("li", "org-log-row");
    row.append(buildPersonElement(member.residentName, []));
    return row;
  }));
  pageElements.callListSection.hidden = callListMembers.length === 0;
}

// The tiles are hidden while the home has no members: a row of zeros says nothing the empty message does not.
function renderStatTiles(org, members) {
  SAFETY_STATE_TILES.forEach((tile, tileIndex) => {
    const memberCount = members.filter((member) => tile.states.includes(member.state)).length;
    statValueElements.safetyStateValues[tileIndex].textContent = String(memberCount);
  });
  statValueElements.coverageValues.forEach((coverageValue, coverageKey) => {
    coverageValue.textContent = String(org.coverage[coverageKey]);
  });
  pageElements.statTiles.hidden = members.length === 0;
}

// The Updated text names the last projection time ("never" when there is none); amber only adds to it when stale.
function renderUpdatedLine(org, isStale) {
  const updatedTimeText = org.lastProjectedAt === null ? "never" : formatTimestampForDisplay(org.lastProjectedAt);
  pageElements.updatedLine.textContent = `Updated ${updatedTimeText}`;
  pageElements.updatedLine.classList.toggle("is-stale", isStale);
}

// The server's rule (org_view.is_projection_stale) on this device's clock: never projected, or older than STALE_AFTER.
function isProjectionStaleNow(lastProjectedAt) {
  return lastProjectedAt === null || Date.now() - parseTimestampMilliseconds(lastProjectedAt) > PROJECTION_STALE_AFTER_MS;
}

// Between syncs the line ages: stale once the server said so or the stamp passes the limit; never turned fresh by this clock.
function startUpdatedLineRefresh() {
  if (updatedLineRefreshIntervalId !== null) return;
  updatedLineRefreshIntervalId = setInterval(() => {
    renderUpdatedLine(shownOrg, shownOrg.isStale || isProjectionStaleNow(shownOrg.lastProjectedAt));
  }, UPDATED_LINE_REFRESH_INTERVAL_MS);
}

function renderOrg(org) {
  const members = keepValidRows(org.members, isValidOrgMember);
  pageElements.topbarTitle.textContent = org.orgName;
  pageElements.residentCount.textContent = `${org.coverage.enrolled} ${org.coverage.enrolled === 1 ? "resident" : "residents"}`;
  renderUpdatedLine(org, org.isStale);
  renderStatTiles(org, members);
  renderActionableList(keepValidRows(org.actionable, isValidActionableItem));
  pageElements.memberRows.replaceChildren(...members.map(buildMemberRowElement));
  pageElements.memberTable.hidden = members.length === 0;
  pageElements.orgEmptyMessage.hidden = members.length > 0;
  renderCallList(keepValidRows(org.callList, isValidCallListMember));
}

// Members shows the office's cards and the dashboard; Messages shows the composer and the alert report.
function showOrgTab(tab) {
  activeOrgTab = tab;
  Object.entries(orgTabButtons).forEach(([tabName, button]) => {
    button.classList.toggle("is-active", tabName === tab);
    button.setAttribute("aria-pressed", String(tabName === tab));
  });
  if (tab === ORG_TAB.MEMBERS) showContactView(); else hideContactView();
  pageElements.topbarTitle.textContent = shownOrg.orgName; // after showContactView, which titles the screen "Pidge"
  pageElements.orgScreen.hidden = tab !== ORG_TAB.MEMBERS;
  pageElements.messagesScreen.hidden = tab !== ORG_TAB.MESSAGES;
}

// The side panel shows the two org tabs instead of the receiver's four.
function showOrgSidePanel() {
  pageElements.receiverTabButtons.forEach((button) => { button.hidden = true; });
  Object.values(orgTabButtons).forEach((button) => { button.hidden = false; });
  pageElements.railMeInitial.textContent = ADMIN_DISPLAY_NAME[0];
  pageElements.railMeName.textContent = ADMIN_DISPLAY_NAME;
  pageElements.sidePanel.hidden = false;
}

export function startOrgView() {
  Object.entries(orgTabButtons).forEach(([tabName, button]) => button.addEventListener("click", () => showOrgTab(tabName)));
  startOrgMessagesView();
}

// The admin's sync_state: requests are the cards asked of the org (shown as a contact's), org is the home's dashboard with
// its alert report, adminRole decides whether the composer shows.
export function applyOrgAdminSyncState(pushMessage) {
  applyContactSyncState(pushMessage.requests);
  if (!isValidOrg(pushMessage.org)) {
    showContactView();
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  shownOrg = pushMessage.org;
  renderOrg(shownOrg);
  applyOrgBroadcastState(keepValidRows(shownOrg.broadcasts, isValidBroadcast), pushMessage.adminRole);
  showOrgSidePanel();
  showOrgTab(activeOrgTab);
  startUpdatedLineRefresh();
}

// A refused action (a Resolved already applied, not allowed or malformed): every Resolved in flight can be tapped again.
export function handleOrgActionRejected() {
  sourceKeysWithResolveInFlight.clear();
  pageElements.followUpList.querySelectorAll(".org-resolved-button").forEach((resolvedButton) => {
    resolvedButton.setAttribute("aria-disabled", "false");
  });
}

// The org screens are torn down (another role's sync_state): the Updated line refresh stops and the side panel gets the
// receiver's four tabs back, hidden until her own sync shows it.
export function hideOrgView() {
  pageElements.orgScreen.hidden = true;
  pageElements.messagesScreen.hidden = true;
  Object.values(orgTabButtons).forEach((button) => { button.hidden = true; });
  pageElements.receiverTabButtons.forEach((button) => { button.hidden = false; });
  pageElements.sidePanel.hidden = true;
  resetOrgMessagesView();
  clearInterval(updatedLineRefreshIntervalId);
  updatedLineRefreshIntervalId = null;
  shownOrg = null;
}
