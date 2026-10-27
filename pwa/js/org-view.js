// An org admin's screen (ORG_ADMIN): the office's "Was this you?" cards on the contact screen, then the home under its own
// name with one row per member (resident name, tags). Every name and tag comes from the server: textContent only.
import { ACTION_STATUS_MESSAGE, showActionStatusMessage } from "./action-status.js";
import { applyContactSyncState, showContactView } from "./contact-view.js";
import { createElementWithText } from "./dom-helpers.js";

const pageElements = {
  orgScreen: document.getElementById("org-screen"),
  orgName: document.getElementById("org-name"),
  orgMemberList: document.getElementById("org-member-list"),
  orgEmptyMessage: document.getElementById("org-empty-message"),
};

function isValidOrg(org) {
  const isValid = org !== null && typeof org === "object" && typeof org.orgName === "string" && Array.isArray(org.members);
  if (!isValid) console.error("SendPidge: org_admin sync_state has no org name or members", org);
  return isValid;
}

function isValidOrgMember(member) {
  const isValid = member !== null && typeof member === "object" && typeof member.memberRef === "string"
    && typeof member.residentName === "string" && Array.isArray(member.tags) && member.tags.every((tag) => typeof tag === "string");
  if (!isValid) console.error("SendPidge: org member has a missing field", member);
  return isValid;
}

function buildOrgMemberRowElement(member) {
  const row = createElementWithText("li", "org-member-row");
  row.append(createElementWithText("span", "org-member-name", member.residentName));
  if (member.tags.length > 0) {
    const tagList = createElementWithText("span", "org-member-tags");
    member.tags.forEach((tag) => tagList.append(createElementWithText("span", "org-member-tag", tag)));
    row.append(tagList);
  }
  return row;
}

function renderOrg(org) {
  const members = org.members.filter(isValidOrgMember);
  pageElements.orgName.textContent = org.orgName;
  pageElements.orgMemberList.replaceChildren(...members.map(buildOrgMemberRowElement));
  pageElements.orgMemberList.hidden = members.length === 0;
  pageElements.orgEmptyMessage.hidden = members.length > 0;
}

// The admin's sync_state: requests are the cards asked of the org (shown as a contact's), org is the home and its members.
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

export function hideOrgView() {
  pageElements.orgScreen.hidden = true;
}
