// The Add-someone / Edit form as data: the draft she is typing, each saved number's state in edit, and the check before send.
// Mirrors shared/circle.py's parse_contact_form; the server is the judge, this only saves a round trip. No DOM here.
import { CHANNEL, RELATIONSHIP, VERIFIED_VIA } from "./constants.js";
import { parseTimestampMilliseconds } from "./dom-helpers.js";

const CONTACT_NAME_MAXIMUM_LENGTH = 40;
const E164_PATTERN = /^\+[1-9]\d{6,14}$/;
const EMAIL_ADDRESS_PATTERN = /^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$/;

// The repeatable rows of the form; the maxima mirror shared/circle.py. Every WhatsApp number gets the code and is asked once
// it sends it back; a phone is saved but not asked yet; every email is asked and is an address they may write from.
export const CONTACT_ROW_LIST = Object.freeze({
  WHATSAPP: { draftKey: "whatsappNumbers", channel: CHANNEL.WHATSAPP, normalize: normalizePhoneNumber, label: "WhatsApp", addLabel: "+ Add WhatsApp", inputType: "tel", autocomplete: "tel", placeholder: "+82 10 1234 5678", maximumRows: 3 },
  PHONE: { draftKey: "phoneNumbers", channel: CHANNEL.RCS, normalize: normalizePhoneNumber, label: "Phone", addLabel: "+ Add phone", inputType: "tel", autocomplete: "tel", placeholder: "", maximumRows: 3 },
  EMAIL: { draftKey: "emailAddresses", channel: CHANNEL.EMAIL, normalize: normalizeEmailAddress, label: "Email", addLabel: "+ Add email", inputType: "email", autocomplete: "email", placeholder: "", maximumRows: 6 },
});

// The chip beside a saved number in edit; a number typed in edit has none until it is saved, and an email has none.
export const SAVED_ROW_STATE_LABEL = Object.freeze({
  CHECKED: "Checked",
  WAITING_FOR_CODE: "Waiting for code",
  CODE_EXPIRED: "Code expired",
  NOT_CHECKED: "Not checked",
});

export const FORM_ERROR_TEXT = Object.freeze({
  NAME: "Enter their name (up to 40 letters).",
  RELATIONSHIP: "Choose who they are.",
  WHATSAPP: "Enter their WhatsApp number with the country code, like +82 10 1234 5678.",
  PHONE: "Enter phone numbers with the country code, like +82 2 734 1234.",
  EMAIL: "Enter the email address they write from.",
  EMAIL_FORMAT: "Check the email addresses.",
  DUPLICATE_NAME: "Someone with that name is already in your circle.",
  CIRCLE_FULL: "Your circle is full.",
  LAST_CHECKED_NUMBER: "Keep one checked WhatsApp number.",
});

export function emptyFormDraft() {
  return { contactName: "", relationship: RELATIONSHIP.GRANDSON, whatsappNumbers: [""], phoneNumbers: [""], emailAddresses: [""] };
}

// A seeded person has no relationship (null): the select asks her to choose one before Save.
export function formDraftFromCircleEntry(entry) {
  const draft = { contactName: entry.contactName, relationship: entry.relationship };
  Object.values(CONTACT_ROW_LIST).forEach((rowList) => {
    const addresses = entry.reachPoints.filter((point) => point.channel === rowList.channel).map((point) => point.address);
    draft[rowList.draftKey] = addresses.length > 0 ? addresses : [""];
  });
  return draft;
}

export function isKnownRelationship(relationship) {
  return Object.values(RELATIONSHIP).includes(relationship);
}

function normalizePhoneNumber(typedNumber) {
  return typedNumber.replace(/[\s().-]/g, "");
}

function normalizeEmailAddress(typedAddress) {
  return typedAddress.trim().toLowerCase();
}

// editedEntry: the circle entry being edited, or null while adding (then no row has a saved state).
function findSavedPoint(editedEntry, rowList, typedValue) {
  const address = rowList.normalize(typedValue);
  return editedEntry?.reachPoints.find((point) => point.channel === rowList.channel && point.address === address);
}

function isCheckedWhatsAppNumber(editedEntry, typedValue) {
  const savedPoint = findSavedPoint(editedEntry, CONTACT_ROW_LIST.WHATSAPP, typedValue);
  return savedPoint !== undefined && savedPoint.verifiedVia !== VERIFIED_VIA.PENDING;
}

// A circle entry's code is dead past codeExpiresAt; saving again sends a new one.
export function isCodeExpired(entry, nowMilliseconds) {
  const expiresAtMilliseconds = parseTimestampMilliseconds(entry.codeExpiresAt);
  return expiresAtMilliseconds !== null && expiresAtMilliseconds < nowMilliseconds;
}

// A SAVED_ROW_STATE_LABEL, or null for a new row, an email, or while adding.
export function describeSavedRowState(editedEntry, rowList, typedValue, nowMilliseconds) {
  if (rowList === CONTACT_ROW_LIST.EMAIL) return null;
  const savedPoint = findSavedPoint(editedEntry, rowList, typedValue);
  if (savedPoint === undefined) return null;
  if (savedPoint.verifiedVia !== VERIFIED_VIA.PENDING) return SAVED_ROW_STATE_LABEL.CHECKED;
  if (rowList !== CONTACT_ROW_LIST.WHATSAPP) return SAVED_ROW_STATE_LABEL.NOT_CHECKED;
  return isCodeExpired(editedEntry, nowMilliseconds) ? SAVED_ROW_STATE_LABEL.CODE_EXPIRED : SAVED_ROW_STATE_LABEL.WAITING_FOR_CODE;
}

// The old number keeps working until a new one sends its code back, so the last checked one cannot be removed.
export function isLastCheckedWhatsAppRow(formDraft, editedEntry, rowList, rowIndex) {
  const rowValues = formDraft[rowList.draftKey];
  const isChecked = (typedValue) => isCheckedWhatsAppNumber(editedEntry, typedValue);
  return rowList === CONTACT_ROW_LIST.WHATSAPP && isChecked(rowValues[rowIndex]) && rowValues.filter(isChecked).length === 1;
}

// The rows that were filled in, normalized; a blank row is ignored.
function readFilledRows(formDraft, rowList) {
  return formDraft[rowList.draftKey].map(rowList.normalize).filter((value) => value !== "");
}

// {form} ready to send, or {errorText} for the first field that is wrong.
export function validateFormDraft(formDraft) {
  const contactName = formDraft.contactName.trim().replace(/\s+/g, " ");
  if (contactName === "" || contactName.length > CONTACT_NAME_MAXIMUM_LENGTH) return { errorText: FORM_ERROR_TEXT.NAME };
  if (!isKnownRelationship(formDraft.relationship)) return { errorText: FORM_ERROR_TEXT.RELATIONSHIP };
  const whatsappNumbers = readFilledRows(formDraft, CONTACT_ROW_LIST.WHATSAPP);
  if (whatsappNumbers.length === 0 || !whatsappNumbers.every((number) => E164_PATTERN.test(number))) return { errorText: FORM_ERROR_TEXT.WHATSAPP };
  const phoneNumbers = readFilledRows(formDraft, CONTACT_ROW_LIST.PHONE);
  if (!phoneNumbers.every((number) => E164_PATTERN.test(number))) return { errorText: FORM_ERROR_TEXT.PHONE };
  const emailAddresses = readFilledRows(formDraft, CONTACT_ROW_LIST.EMAIL);
  if (emailAddresses.length === 0) return { errorText: FORM_ERROR_TEXT.EMAIL };
  if (!emailAddresses.every((address) => EMAIL_ADDRESS_PATTERN.test(address))) return { errorText: FORM_ERROR_TEXT.EMAIL_FORMAT };
  return { form: { contactName, relationship: formDraft.relationship, whatsappNumbers, phoneNumbers, emailAddresses } };
}
