import { Stack } from "aws-cdk-lib";

// Everything the stack reads from cdk.json context and the git-ignored ../.env (via bin/sendpidge.ts), validated once.
export interface StackSettings {
  receiverDomain: string;
  // The one resident bound to receiverMailbox (emailed questions, the mailed recovery pack); "" when none.
  mailboxResidentAddress: string;
  // The one resident whose inbox gets every text to Pidge's WhatsApp, RCS and SMS numbers (pidge-inbound); "" → no pidge-inbound.
  messengerResidentAddress: string;
  // Two-way SMS into that inbox (SmsInboundTopic); on needs smsOriginationNumber, smsPhoneNumberArn and messengerResidentAddress.
  smsEnabled: boolean;
  // Pidge's SMS number in E.164: pidge-inbound refuses an SMS record addressed to any other number.
  smsOriginationNumber: string;
  // That number's End User Messaging ARN; required with smsEnabled.
  smsPhoneNumberArn: string;
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
  // The keypad call to the contact on the re-ask (Amazon Connect); on needs every voice* value below and retrySchedulerEnabled.
  voiceEnabled: boolean;
  // The Connect instance that places the call; its "pidge-ask" flow invokes pidge-verdict with the keypad answer.
  voiceInstanceArn: string;
  // The "pidge-ask" contact flow on that instance: speaks the ask and reads the keypad digit.
  voiceContactFlowId: string;
  // The instance's claimed number in E.164: the caller the contact sees.
  voiceSourcePhoneNumber: string;
  // How many calls the whole stack may place per hour (CALLRATE#stack); a re-ask past the cap goes without a call.
  voiceCallsPerHour: number;
  // A one-time schedule hangs up a call still open after this many seconds (30-600).
  voiceCallMaxSeconds: number;
  // The Amazon Q in Connect assistant the AI call writes facts to; "" keeps the call keypad-only.
  voiceAiAssistantArn: string;
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
  // The Cognito managed-login domain prefix; "" → passkey sign-in off (no user pool, no COGNITO_* env).
  cognitoDomainPrefix: string;
  // Where managed login returns the PWA after sign-in: its root URL.
  pwaCallbackUrl: string;
  // The org dashboard (S3): projection, OrgActions route and canary; off leaves the template as it was.
  orgEnabled: boolean;
  // The one home whose ORG#<orgId>/CANARY row the canary writes; "" when orgEnabled is off.
  orgId: string;
}

const MAXIMUM_CALL_REMINDER_DELAY_SECONDS = 82800;
// Cognito prefix rules: lower-case letters, digits and inner hyphens, at most 63 characters, none of these reserved words.
const COGNITO_DOMAIN_PREFIX_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const COGNITO_DOMAIN_PREFIX_RESERVED_WORDS = ["aws", "amazon", "cognito"];
// lambda/shared/org_ids.py ORG_ID_PATTERN (fullmatch).
const ORG_ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,39}$/;
// lambda/shared/voice_ask.py INSTANCE_ARN_PATTERN (fullmatch); group 1 is the Region.
const CONNECT_INSTANCE_ARN_PATTERN = /^arn:aws:connect:([a-z0-9-]+):\d{12}:instance\/[0-9a-f-]{36}$/;
// An Amazon Q in Connect assistant (`aws qconnect list-assistants`); group 1 is the Region.
const AI_ASSISTANT_ARN_PATTERN = /^arn:aws:wisdom:([a-z0-9-]+):\d{12}:assistant\/[0-9a-f-]{36}$/;
const MINIMUM_VOICE_CALL_MAX_SECONDS = 30;
const MAXIMUM_VOICE_CALL_MAX_SECONDS = 600;
const E164_PHONE_NUMBER_PATTERN = /^\+[1-9][0-9]{1,14}$/;

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
  const mailboxResidentAddress = readResidentAddressContext(stack, "mailboxResidentAddress", receiverDomain);
  const messengerResidentAddress = readResidentAddressContext(stack, "messengerResidentAddress", receiverDomain);
  if ((chatEnabled || receiverMailbox !== "") && mailboxResidentAddress === "") {
    throw new Error("chatEnabled or RECEIVER_MAILBOX needs mailboxResidentAddress: the resident that mailbox belongs to");
  }
  const pwaDomain: string = stack.node.getContext("pwaDomain");
  const releaseSchedulerEnabled = stack.node.getContext("releaseSchedulerEnabled") === true;
  const orgEnabled = stack.node.tryGetContext("orgEnabled") === true;
  if (orgEnabled && !releaseSchedulerEnabled) {
    throw new Error("orgEnabled needs releaseSchedulerEnabled: without the release timer a confirmed answer never reaches the stream the org projection reads");
  }
  const retrySchedulerEnabled = stack.node.getContext("retrySchedulerEnabled") === true;
  return {
    receiverDomain,
    mailboxResidentAddress,
    messengerResidentAddress,
    ...readSmsSettings(stack, messengerResidentAddress),
    readerModel,
    receiptSender,
    sendingDomain,
    casesAddress: `cases@${receiverDomain}`,
    pwaDomain,
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
    releaseSchedulerEnabled,
    caseAgentRuntimeMode: stack.node.getContext("caseAgentRuntime"),
    caseAgentModel: stack.node.getContext("caseAgentModel"),
    retrySchedulerEnabled,
    ...readVoiceSettings(stack, retrySchedulerEnabled),
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
    cognitoDomainPrefix: readCognitoDomainPrefix(stack),
    pwaCallbackUrl: `https://${pwaDomain}/`,
    orgEnabled,
    orgId: readOrgId(stack, orgEnabled),
  };
}

// The canary's home; absent reads as "". Throws when orgEnabled is on and the id is off ORG_ID_PATTERN (so "" throws too).
function readOrgId(stack: Stack, orgEnabled: boolean): string {
  const orgId = readOptionalTextContext(stack, "orgId");
  if (orgEnabled && !ORG_ID_PATTERN.test(orgId)) {
    throw new Error("orgId must be 1-40 lower-case letters, digits and hyphens, starting with a letter or digit (lambda/shared/org_ids.py)");
  }
  return orgId;
}

// The managed-login domain prefix; absent or "" reads as "" (sign-in off). Throws when it is set but Cognito would refuse it.
function readCognitoDomainPrefix(stack: Stack): string {
  const cognitoDomainPrefix = readOptionalTextContext(stack, "cognitoDomainPrefix");
  if (cognitoDomainPrefix === "") {
    return cognitoDomainPrefix;
  }
  const hasReservedWord = COGNITO_DOMAIN_PREFIX_RESERVED_WORDS.some((reservedWord) => cognitoDomainPrefix.includes(reservedWord));
  if (!COGNITO_DOMAIN_PREFIX_PATTERN.test(cognitoDomainPrefix) || hasReservedWord) {
    const reservedWords = COGNITO_DOMAIN_PREFIX_RESERVED_WORDS.join(", ");
    throw new Error(`cognitoDomainPrefix must be lower-case letters, digits and inner hyphens (1-63) without ${reservedWords}`);
  }
  return cognitoDomainPrefix;
}

// A resident's address from contextKey, lower-cased; absent reads as "". Throws when it is set but not on receiverDomain.
function readResidentAddressContext(stack: Stack, contextKey: string, receiverDomain: string): string {
  const residentAddress = readOptionalTextContext(stack, contextKey).toLowerCase();
  if (residentAddress !== "" && !residentAddress.endsWith(`@${receiverDomain}`)) {
    throw new Error(`${contextKey} must be an address @${receiverDomain}`);
  }
  return residentAddress;
}

// The two-way SMS settings; absent values read as "". Throws when smsEnabled is on without an E.164 number, the phone number's
// ARN or the messenger resident whose inbox the texts go to.
function readSmsSettings(
  stack: Stack, messengerResidentAddress: string,
): Pick<StackSettings, "smsEnabled" | "smsOriginationNumber" | "smsPhoneNumberArn"> {
  const smsEnabled = stack.node.tryGetContext("smsEnabled") === true;
  const smsOriginationNumber = readOptionalTextContext(stack, "smsOriginationNumber");
  const smsPhoneNumberArn = readOptionalTextContext(stack, "smsPhoneNumberArn");
  if (smsEnabled && (!E164_PHONE_NUMBER_PATTERN.test(smsOriginationNumber) || smsPhoneNumberArn === "" || messengerResidentAddress === "")) {
    throw new Error("smsEnabled needs smsOriginationNumber (E.164), smsPhoneNumberArn and messengerResidentAddress");
  }
  return { smsEnabled, smsOriginationNumber, smsPhoneNumberArn };
}

type VoiceSettings = Pick<
  StackSettings,
  | "voiceEnabled" | "voiceInstanceArn" | "voiceContactFlowId" | "voiceSourcePhoneNumber" | "voiceCallsPerHour" | "voiceCallMaxSeconds"
  | "voiceAiAssistantArn"
>;

// The call settings; absent text reads as "" and an absent number as NaN. Throws when voiceEnabled is on without a Connect
// instance ARN, a flow id, an E.164 caller number, a whole hourly cap above 0, a whole call limit of 30-600 s, or the retry
// scheduler (pidge-retry places the call on the re-ask and hangs it up from a schedule), or with a bad voiceAiAssistantArn.
function readVoiceSettings(stack: Stack, retrySchedulerEnabled: boolean): VoiceSettings {
  const voiceEnabled = stack.node.tryGetContext("voiceEnabled") === true;
  const voiceInstanceArn = readOptionalTextContext(stack, "voiceInstanceArn");
  const voiceContactFlowId = readOptionalTextContext(stack, "voiceContactFlowId");
  const voiceSourcePhoneNumber = readOptionalTextContext(stack, "voiceSourcePhoneNumber");
  const voiceCallsPerHour = Number(stack.node.tryGetContext("voiceCallsPerHour"));
  const voiceCallMaxSeconds = Number(stack.node.tryGetContext("voiceCallMaxSeconds"));
  const isVoiceConfigurationComplete = CONNECT_INSTANCE_ARN_PATTERN.test(voiceInstanceArn)
    && voiceContactFlowId !== ""
    && E164_PHONE_NUMBER_PATTERN.test(voiceSourcePhoneNumber)
    && Number.isInteger(voiceCallsPerHour) && voiceCallsPerHour > 0
    && Number.isInteger(voiceCallMaxSeconds)
    && voiceCallMaxSeconds >= MINIMUM_VOICE_CALL_MAX_SECONDS && voiceCallMaxSeconds <= MAXIMUM_VOICE_CALL_MAX_SECONDS
    && retrySchedulerEnabled;
  if (voiceEnabled && !isVoiceConfigurationComplete) {
    throw new Error("voiceEnabled needs voiceInstanceArn (a Connect instance ARN), voiceContactFlowId, voiceSourcePhoneNumber (E.164), voiceCallsPerHour > 0, voiceCallMaxSeconds 30-600 and retrySchedulerEnabled");
  }
  return {
    voiceEnabled, voiceInstanceArn, voiceContactFlowId, voiceSourcePhoneNumber, voiceCallsPerHour, voiceCallMaxSeconds,
    voiceAiAssistantArn: readVoiceAiAssistantArn(stack, voiceEnabled, voiceInstanceArn),
  };
}

// The AI call's assistant; absent reads as "" (the call stays keypad-only). Throws when voiceEnabled is on and it is set but is
// not an assistant ARN in the voiceInstanceArn Region. Called after readVoiceSettings has checked voiceInstanceArn.
function readVoiceAiAssistantArn(stack: Stack, voiceEnabled: boolean, voiceInstanceArn: string): string {
  const voiceAiAssistantArn = readOptionalTextContext(stack, "voiceAiAssistantArn");
  if (!voiceEnabled || voiceAiAssistantArn === "") {
    return voiceAiAssistantArn;
  }
  const assistantRegion = AI_ASSISTANT_ARN_PATTERN.exec(voiceAiAssistantArn)?.[1];
  const instanceRegion = CONNECT_INSTANCE_ARN_PATTERN.exec(voiceInstanceArn)?.[1];
  if (assistantRegion === undefined || assistantRegion !== instanceRegion) {
    throw new Error("voiceAiAssistantArn must be \"\" or an Amazon Q in Connect assistant ARN in the voiceInstanceArn Region");
  }
  return voiceAiAssistantArn;
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
