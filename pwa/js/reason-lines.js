// One calm sentence per sender reason from the scam list; shown under the banner beside the link reason lines.
// Reason codes come from the backend enum; the text never includes anything from the email.
import { REASON_CODE } from "./constants.js";
import { createElementWithText } from "./dom-helpers.js";

const SENDER_REASON_SENTENCES = Object.freeze({
  [REASON_CODE.LISTED_SENDER_ADDRESS]: "This sender's address is on Pidge's warning list (sample data).",
  [REASON_CODE.LISTED_SENDER_DOMAIN]: "This sender's mail service is on Pidge's warning list (sample data).",
  [REASON_CODE.LOOKALIKE_SENDER_NAME]: "The sender's name looks like a family name but is not the same.",
  [REASON_CODE.LOOKALIKE_SENDER_DOMAIN]: "The sender's address imitates a family address.",
  [REASON_CODE.REPORTED_SENDER]: "You reported this sender before.",
  [REASON_CODE.LIST_LOOKUP_FAILED]: "Pidge could not check its warning list this time.",
});

export function buildSenderReasonLineElements(reasons) {
  if (!Array.isArray(reasons)) return [];
  return reasons
    .filter((reason) => Object.prototype.hasOwnProperty.call(SENDER_REASON_SENTENCES, reason))
    .map((reason) => createElementWithText("p", "reason-line", SENDER_REASON_SENTENCES[reason]));
}
