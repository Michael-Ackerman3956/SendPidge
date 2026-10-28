// One calm sentence per sender reason from the scam list, and per reason about the email's links as a whole; shown under the
// banner beside the per-link reason lines, plus the coverage line when the lists could not answer. Reason codes come from the
// backend enum; the text never includes anything from the email.
import { REASON_CODE, SCAM_LIST_COVERAGE } from "./constants.js";
import { createElementWithText } from "./dom-helpers.js";

const SENDER_REASON_SENTENCES = Object.freeze({
  [REASON_CODE.LISTED_SENDER_ADDRESS]: "This sender's address is on Pidge's warning list.",
  [REASON_CODE.LISTED_SENDER_DOMAIN]: "This sender's mail service is on Pidge's warning list.",
  [REASON_CODE.LOOKALIKE_SENDER_NAME]: "The sender's name looks like a family name but is not the same.",
  [REASON_CODE.LOOKALIKE_SENDER_DOMAIN]: "The sender's address imitates a family address.",
  [REASON_CODE.REPORTED_SENDER]: "You reported this sender before.",
  [REASON_CODE.LIST_LOOKUP_FAILED]: "Pidge could not check its warning list this time.",
  [REASON_CODE.LINKS_UNREADABLE]: "Pidge could not read this email's links.",
  [REASON_CODE.LINKS_TRUNCATED]: "This email has more links than Pidge checks.",
  [REASON_CODE.IMAGE_ONLY_BODY]: "This email is mostly a picture, so Pidge could not read its words.",
});

export function buildSenderReasonLineElements(reasons) {
  if (!Array.isArray(reasons)) return [];
  return reasons
    .filter((reason) => Object.prototype.hasOwnProperty.call(SENDER_REASON_SENTENCES, reason))
    .map((reason) => createElementWithText("p", "reason-line", SENDER_REASON_SENTENCES[reason]));
}

// The one line for an email the scam lists could not answer for (a stale feed or a failed read); null otherwise.
export function buildScamListCoverageLineElement(email) {
  if (email.scamListCoverage !== SCAM_LIST_COVERAGE.UNAVAILABLE) return null;
  return createElementWithText("p", "reason-line", "Pidge's lists were not available for this one.");
}
