import { Stack } from "aws-cdk-lib";

// Everything the stack reads from cdk.json context and the git-ignored ../.env (via bin/sendpidge.ts), validated once.
export interface StackSettings {
  receiverDomain: string;
  // The one resident bound to receiverMailbox (emailed questions, the mailed recovery pack); "" when none.
  mailboxResidentAddress: string;
  readerModel: string;
  receiptSender: string;
  sendingDomain: string;
  casesAddress: string;
  pwaDomain: string;
  landingDomain: string;
  asksPerContactPerHour: number;
  rcsEnabled: boolean;
  rcsAgentId: string;
  rcsAgentArn: string;
  whatsappEnabled: boolean;
  emailAskEnabled: boolean;
  whatsappPhoneNumberId: string;
  whatsappPhoneNumberArn: string;
  whatsappMetaApiVersion: string;
  whatsappTemplateName: string;
  escalationEnabled: boolean;
  escalationModel: string;
  releaseSchedulerEnabled: boolean;
  caseAgentRuntimeMode: string;
  caseAgentModel: string;
  retrySchedulerEnabled: boolean;
  reAskDelaySeconds: number;
  bankFollowUpEnabled: boolean;
  bankFollowUpDelaySeconds: number;
  mockBankMailbox: string;
  receiverMailbox: string;
  chatEnabled: boolean;
  chatGuardrailEnabled: boolean;
  contactVerificationMock: boolean;
  whatsappDisplayNumber: string;
  callNudgeEnabled: boolean;
  callNudgesPerContactPerHour: number;
  callReminderTodayDelaySeconds: number;
  callReminderTomorrowDelaySeconds: number;
  vapidSecretName: string;
}

const MAXIMUM_CALL_REMINDER_DELAY_SECONDS = 82800;

export function readStackSettings(stack: Stack): StackSettings {
  const receiverDomain: string = stack.node.getContext("receiverDomain");
  const readerModel: string = stack.node.getContext("readerModel");
  const receiptSender: string = stack.node.getContext("receiptSender");
  const sendingDomain = receiptSender.split("@")[1];
  // Personal mailboxes come from the git-ignored ../.env (bin/sendpidge.ts), never from an address on our own domain.
  // The SIMULATED bank; empty → Send to my bank is refused.
  const mockBankMailbox: string = stack.node.tryGetContext("mockBankMailbox") ?? "";
  // Grandma's real mailbox: the only sender whose mail to pidge@ becomes a question, and where the answer goes.
  const receiverMailbox: string = stack.node.tryGetContext("receiverMailbox") ?? "";
  for (const [contextKey, mailbox] of [["mockBankMailbox", mockBankMailbox], ["receiverMailbox", receiverMailbox]]) {
    if (mailbox.toLowerCase().endsWith(`@${receiverDomain}`) || mailbox.toLowerCase().endsWith(`@${sendingDomain}`)) {
      throw new Error(`${contextKey} must not be on ${receiverDomain} or ${sendingDomain}: a reply would re-enter our own mail flow`);
    }
  }
  const chatEnabled: boolean = stack.node.tryGetContext("chatEnabled") === true;
  // The call nudge: after a WhatsApp verdict, offer "call Grandma?" and book one reminder inside Meta's 24-h window.
  const callReminderTodayDelaySeconds: number = Number(stack.node.tryGetContext("callReminderTodayDelaySeconds") ?? 14400);
  const callReminderTomorrowDelaySeconds: number = Number(stack.node.tryGetContext("callReminderTomorrowDelaySeconds") ?? 79200);
  for (const [contextKey, delaySeconds] of [
    ["callReminderTodayDelaySeconds", callReminderTodayDelaySeconds],
    ["callReminderTomorrowDelaySeconds", callReminderTomorrowDelaySeconds],
  ] as const) {
    if (!(delaySeconds > 0 && delaySeconds < MAXIMUM_CALL_REMINDER_DELAY_SECONDS)) {
      throw new Error(`${contextKey} must be > 0 and < ${MAXIMUM_CALL_REMINDER_DELAY_SECONDS} to stay inside Meta's 24-h window`);
    }
  }
  if (chatEnabled && !receiverMailbox.includes("@")) {
    throw new Error("chatEnabled needs RECEIVER_MAILBOX in ../.env (Grandma's real mailbox for pidge@)");
  }
  const mailboxResidentAddress = readMailboxResidentAddress(stack, receiverDomain);
  if ((chatEnabled || receiverMailbox !== "") && mailboxResidentAddress === "") {
    throw new Error("chatEnabled or RECEIVER_MAILBOX needs mailboxResidentAddress: the resident that mailbox belongs to");
  }
  return {
    receiverDomain,
    mailboxResidentAddress,
    readerModel,
    receiptSender,
    sendingDomain,
    casesAddress: `cases@${receiverDomain}`,
    pwaDomain: stack.node.getContext("pwaDomain"),
    landingDomain: stack.node.getContext("landingDomain"),
    // The ask cap: how many "Was this you?" asks one contact can get per hour; later holds stay silent (slice 9).
    asksPerContactPerHour: Number(stack.node.tryGetContext("asksPerContactPerHour") ?? 3),
    rcsEnabled: stack.node.getContext("rcsEnabled") === true,
    rcsAgentId: stack.node.getContext("rcsAgentId"),
    rcsAgentArn: stack.node.getContext("rcsAgentArn"),
    whatsappEnabled: stack.node.getContext("whatsappEnabled") === true,
    emailAskEnabled: stack.node.getContext("emailAskEnabled") === true,
    whatsappPhoneNumberId: stack.node.getContext("whatsappPhoneNumberId"),
    whatsappPhoneNumberArn: stack.node.getContext("whatsappPhoneNumberArn"),
    whatsappMetaApiVersion: stack.node.getContext("whatsappMetaApiVersion"),
    whatsappTemplateName: stack.node.getContext("whatsappTemplateName"),
    escalationEnabled: stack.node.getContext("escalationEnabled") === true,
    escalationModel: stack.node.getContext("escalationModel"),
    releaseSchedulerEnabled: stack.node.getContext("releaseSchedulerEnabled") === true,
    caseAgentRuntimeMode: stack.node.getContext("caseAgentRuntime"),
    caseAgentModel: stack.node.getContext("caseAgentModel"),
    retrySchedulerEnabled: stack.node.getContext("retrySchedulerEnabled") === true,
    reAskDelaySeconds: Number(stack.node.getContext("reAskDelaySeconds")),
    bankFollowUpEnabled: stack.node.getContext("bankFollowUpEnabled") === true,
    bankFollowUpDelaySeconds: Number(stack.node.getContext("bankFollowUpDelaySeconds")),
    mockBankMailbox,
    receiverMailbox,
    chatEnabled,
    chatGuardrailEnabled: stack.node.tryGetContext("chatGuardrailEnabled") === true,
    // Add someone: with the mock on, Grandma's own PWA may play the contact's code reply (labelled demo, pidge-verdict only).
    contactVerificationMock: stack.node.tryGetContext("contactVerificationMock") === true,
    // Pidge's own WhatsApp number in E.164 (PIDGE_WHATSAPP_NUMBER in ../.env via bin/sendpidge.ts), for the wa.me reply line in
    // the note; "" → only the demo can verify a new contact.
    whatsappDisplayNumber: readOptionalTextContext(stack, "whatsappDisplayNumber"),
    callNudgeEnabled: stack.node.tryGetContext("callNudgeEnabled") === true,
    callNudgesPerContactPerHour: Number(stack.node.tryGetContext("callNudgesPerContactPerHour") ?? 1),
    callReminderTodayDelaySeconds,
    callReminderTomorrowDelaySeconds,
    // The Web Push signing key (raw P-256, base64url), created once by scripts/create_vapid_key.py; never in context or code.
    vapidSecretName: stack.node.tryGetContext("vapidSecretName") ?? "sendpidge/vapid-private-key",
  };
}

// The mailbox resident's address, lower-cased; absent reads as "". Throws when it is set but not on receiverDomain.
function readMailboxResidentAddress(stack: Stack, receiverDomain: string): string {
  const mailboxResidentAddress = readOptionalTextContext(stack, "mailboxResidentAddress").toLowerCase();
  if (mailboxResidentAddress !== "" && !mailboxResidentAddress.endsWith(`@${receiverDomain}`)) {
    throw new Error(`mailboxResidentAddress must be an address @${receiverDomain}`);
  }
  return mailboxResidentAddress;
}

// An optional text context key: absent reads as "" (the feature it configures then reports itself off), never a guess.
function readOptionalTextContext(stack: Stack, contextKey: string): string {
  const value: unknown = stack.node.tryGetContext(contextKey);
  return typeof value === "string" ? value : "";
}

// Lambda environment values are strings; every feature flag crosses as "true" / "false".
export function flagEnvironmentValue(isEnabled: boolean): string {
  return isEnabled ? "true" : "false";
}
