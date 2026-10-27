// An org admin's screen (ORG_ADMIN): the office's "Was this you?" cards on the contact screen, then the home under its own
// name: when the projection last ran (amber when stale), the open items to follow up (one Resolved button each), four coverage
// counts, every member with tags and safety state, and the members to call. Every name, tag and key comes from the server:
// textContent only. Resolved sends org_resolve; success has no reply, the projection's org_changed re-sync (app.js) shows it.
import { ACTION_STATUS_MESSAGE, clearActionStatusMessage, showActionStatusMessage } from "./action-status.js";
import { CLIENT_ACTION, MEMBER_SAFETY_STATE } from "./constants.js";
import { applyContactSyncState, showContactView } from "./contact-view.js";
import { createElementWithText, parseTimestampMilliseconds } from "./dom-helpers.js";
import { sendClientAction } from "./websocket.js";

const MEMBER_SAFETY_STATE_LABELS = Object.freeze({
  [MEMBER_SAFETY_STATE.NOT_SHARED]: "Not shared",
  [MEMBER_SAFETY_STATE.NOTHING_OPEN]: "Nothing open",
  [MEMBER_SAFETY_STATE.HELD]: "Held",
  [MEMBER_SAFETY_STATE.HELD_NO_ANSWER]: "No answer",
  [MEMBER_SAFETY_STATE.CONFIRMED_NOT_ME]: "Not me",
  [MEMBER_SAFETY_STATE.WARNED]: "Warned",
  [MEMBER_SAFETY_STATE.CASE_OPEN]: "Case open",
  [MEMBER_SAFETY_STATE.CONTACT_UNREACHABLE]: "Can't reach contact",
});
const KNOWN_MEMBER_SAFETY_STATES = new Set(Object.values(MEMBER_SAFETY_STATE));
// org.coverage keys in display order, each with its label.
const COVERAGE_LABELS = Object.freeze([
  ["enrolled", "Enrolled"],
  ["withVerifiedContact", "Verified contact"],
  ["visibilityChosen", "Chose sharing"],
  ["seenInLastSevenDays", "Seen in 7 days"],
]);

const pageElements = {
  orgScreen: document.getElementById("org-screen"),
  orgName: document.getElementById("org-name"),
  orgMemberList: document.getElementById("org-member-list"),
  orgEmptyMessage: document.getElementById("org-empty-message"),
};
const dashboardElements = buildDashboardElements();
// A tapped Resolved stays aria-disabled until a re-sync no longer lists its item or the action is rejected.
const sourceKeysWithResolveInFlight = new Set();

function buildSectionHeader(headerText) {
  return createElementWithText("h3", "section-header org-section-header", headerText);
}

function buildCoverageStrip() {
  const coverageStrip = createElementWithText("ul", "org-coverage");
  const coverageValueElements = new Map(COVERAGE_LABELS.map(([coverageKey, coverageLabel]) => {
    const coverageValue = createElementWithText("span", "org-coverage-value");
    const coverageItem = createElementWithText("li", "org-coverage-item");
    coverageItem.append(coverageValue, createElementWithText("span", "org-coverage-label", coverageLabel));
    coverageStrip.append(coverageItem);
    return [coverageKey, coverageValue];
  }));
  return { coverageStrip, coverageValueElements };
}

// Built once around the static name, member list and empty message in index.html: name, Updated, follow-up, coverage,
// Members, then the call list.
function buildDashboardElements() {
  const updatedLine = createElementWithText("p", "org-updated");
  const actionableHeader = buildSectionHeader("Follow up");
  const actionableList = createElementWithText("ul", "org-member-list");
  const { coverageStrip, coverageValueElements } = buildCoverageStrip();
  const callListHeader = buildSectionHeader("Call list");
  const callList = createElementWithText("ul", "org-member-list");
  pageElements.orgName.after(updatedLine, actionableHeader, actionableList, coverageStrip, buildSectionHeader("Members"));
  pageElements.orgScreen.append(callListHeader, callList);
  return { updatedLine, actionableHeader, actionableList, coverageValueElements, callListHeader, callList };
}

function isTimestampText(value) {
  return typeof value === "string" && parseTimestampMilliseconds(value) !== null;
}

function isValidCoverage(coverage) {
  return coverage !== null && typeof coverage === "object" && COVERAGE_LABELS.every(([coverageKey]) => Number.isInteger(coverage[coverageKey]));
}

// A missing isStale or lastProjectedAt refuses the whole payload: it never reads as fresh.
function isValidOrg(org) {
  const isValid = org !== null && typeof org === "object" && typeof org.orgName === "string" && Array.isArray(org.members)
    && Array.isArray(org.actionable) && Array.isArray(org.callList) && typeof org.isStale === "boolean"
    && (org.lastProjectedAt === null || isTimestampText(org.lastProjectedAt)) && isValidCoverage(org.coverage);
  if (!isValid) console.error("SendPidge: org_admin sync_state has a missing or malformed org field", org);
  return isValid;
}

function isValidNamedMember(member) {
  return member !== null && typeof member === "object" && typeof member.memberRef === "string" && typeof member.residentName === "string";
}

function isValidOrgMember(member) {
  const isValid = isValidNamedMember(member) && Array.isArray(member.tags) && member.tags.every((tag) => typeof tag === "string")
    && KNOWN_MEMBER_SAFETY_STATES.has(member.state);
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

// Time only when it is today, else date and time.
function formatTimestampForDisplay(isoText) {
  const shownDate = new Date(parseTimestampMilliseconds(isoText));
  const isToday = shownDate.toDateString() === new Date().toDateString();
  return shownDate.toLocaleString([], isToday ? { timeStyle: "short" } : { dateStyle: "medium", timeStyle: "short" });
}

function buildOrgMemberRowElement(member) {
  const row = createElementWithText("li", "org-member-row");
  row.append(createElementWithText("span", "org-member-name", member.residentName));
  if (member.tags.length > 0) {
    const tagList = createElementWithText("span", "org-member-tags");
    member.tags.forEach((tag) => tagList.append(createElementWithText("span", "org-member-tag", tag)));
    row.append(tagList);
  }
  row.append(createElementWithText("span", "org-member-state", MEMBER_SAFETY_STATE_LABELS[member.state]));
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
  const stateLabel = MEMBER_SAFETY_STATE_LABELS[actionableItem.state];
  const row = createElementWithText("li", "org-member-row");
  const details = createElementWithText("span", "org-actionable-details");
  details.append(
    createElementWithText("span", "org-member-name", actionableItem.residentName),
    createElementWithText("span", "org-actionable-state", `${stateLabel} · ${formatTimestampForDisplay(actionableItem.at)}`),
  );
  const resolvedButton = createElementWithText("button", "contact-secondary-button org-resolved-button", "Resolved");
  resolvedButton.type = "button";
  resolvedButton.setAttribute("aria-label", `Resolved, ${actionableItem.residentName}, ${stateLabel}`);
  resolvedButton.setAttribute("aria-disabled", String(sourceKeysWithResolveInFlight.has(actionableItem.sourceKey)));
  resolvedButton.addEventListener("click", () => handleUserTappedResolved(actionableItem, resolvedButton));
  row.append(details, resolvedButton);
  return row;
}

function renderActionableList(actionableItems) {
  const listedSourceKeys = new Set(actionableItems.map((actionableItem) => actionableItem.sourceKey));
  [...sourceKeysWithResolveInFlight].filter((sourceKey) => !listedSourceKeys.has(sourceKey))
    .forEach((sourceKey) => sourceKeysWithResolveInFlight.delete(sourceKey));
  dashboardElements.actionableList.replaceChildren(...actionableItems.map(buildActionableRowElement));
  dashboardElements.actionableList.hidden = actionableItems.length === 0;
  dashboardElements.actionableHeader.hidden = actionableItems.length === 0;
}

function renderCallList(callListMembers) {
  dashboardElements.callList.replaceChildren(...callListMembers.map((member) => {
    const row = createElementWithText("li", "org-member-row");
    row.append(createElementWithText("span", "org-member-name", member.residentName));
    return row;
  }));
  dashboardElements.callList.hidden = callListMembers.length === 0;
  dashboardElements.callListHeader.hidden = callListMembers.length === 0;
}

// The Updated text names the last projection time ("never" when there is none); amber only adds to it when stale.
function renderUpdatedLine(org) {
  const updatedTimeText = org.lastProjectedAt === null ? "never" : formatTimestampForDisplay(org.lastProjectedAt);
  dashboardElements.updatedLine.textContent = `Updated ${updatedTimeText}`;
  dashboardElements.updatedLine.classList.toggle("is-stale", org.isStale);
}

function renderOrg(org) {
  const members = keepValidRows(org.members, isValidOrgMember);
  pageElements.orgName.textContent = org.orgName;
  renderUpdatedLine(org);
  renderActionableList(keepValidRows(org.actionable, isValidActionableItem));
  dashboardElements.coverageValueElements.forEach((coverageValue, coverageKey) => {
    coverageValue.textContent = String(org.coverage[coverageKey]);
  });
  pageElements.orgMemberList.replaceChildren(...members.map(buildOrgMemberRowElement));
  pageElements.orgMemberList.hidden = members.length === 0;
  pageElements.orgEmptyMessage.hidden = members.length > 0;
  renderCallList(keepValidRows(org.callList, isValidCallListMember));
}

// The admin's sync_state: requests are the cards asked of the org (shown as a contact's), org is the home's dashboard.
export function applyOrgAdminSyncState(pushMessage) {
  showContactView();
  applyContactSyncState(pushMessage.requests);
  if (!isValidOrg(pushMessage.org)) {
    showActionStatusMessage(ACTION_STATUS_MESSAGE.GENERIC_FAILURE);
    return;
  }
  renderOrg(pushMessage.org);
  pageElements.orgScreen.hidden = false;
}

// A refused action (a Resolved already applied, not allowed or malformed): every Resolved in flight can be tapped again.
export function handleOrgActionRejected() {
  sourceKeysWithResolveInFlight.clear();
  dashboardElements.actionableList.querySelectorAll(".org-resolved-button").forEach((resolvedButton) => {
    resolvedButton.setAttribute("aria-disabled", "false");
  });
}

export function hideOrgView() {
  pageElements.orgScreen.hidden = true;
}
