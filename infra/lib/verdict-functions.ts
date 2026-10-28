import { Duration, Stack } from "aws-cdk-lib";
import { WebSocketLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { Effect, PolicyStatement, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { Function, FunctionUrl, FunctionUrlAuthType } from "aws-cdk-lib/aws-lambda";
import { ISecret } from "aws-cdk-lib/aws-secretsmanager";
import { EventType } from "aws-cdk-lib/aws-s3";
import { LambdaDestination } from "aws-cdk-lib/aws-s3-notifications";
import { Topic } from "aws-cdk-lib/aws-sns";
import { LambdaSubscription } from "aws-cdk-lib/aws-sns-subscriptions";
import { ASKS_PREFIX, INBOX_PREFIX } from "./mail-ingress.js";
import { buildPythonFunction, buildSendEmailStatement, grantRcsSendIfConfigured, grantWhatsAppSendIfConfigured } from "./python-function.js";
import { SharedResources } from "./realtime.js";
import { StackSettings, flagEnvironmentValue } from "./stack-settings.js";

// The re-ask timer: at +reAskDelay an unanswered ask is re-sent once; a second silence is marked timeout.
export function buildRetryFunction(stack: Stack, settings: StackSettings, shared: SharedResources, askLinkSecret: ISecret): Function {
  const retryFunction = buildPythonFunction(stack, "Retry", shared.lambdaCode, {
    handler: "pidge_retry.handler.handle",
    memorySize: 512,
    timeout: Duration.seconds(30),
    environment: {
      TABLE_NAME: shared.table.tableName,
      RE_ASK_DELAY_SECONDS: String(settings.reAskDelaySeconds),
      WEBSOCKET_ENDPOINT: shared.webSocketStage.callbackUrl,
      SCHEDULE_GROUP_NAME: shared.scheduleGroup.scheduleGroupName,
      SCHEDULER_ROLE_ARN: shared.schedulerRole.roleArn,
      RCS_ENABLED: flagEnvironmentValue(settings.rcsEnabled),
      RCS_AGENT_ID: settings.rcsAgentId,
      WHATSAPP_ENABLED: flagEnvironmentValue(settings.whatsappEnabled),
      WHATSAPP_PHONE_NUMBER_ID: settings.whatsappPhoneNumberId,
      WHATSAPP_META_API_VERSION: settings.whatsappMetaApiVersion,
      WHATSAPP_TEMPLATE_NAME: settings.whatsappTemplateName,
      EMAIL_ASK_ENABLED: flagEnvironmentValue(settings.emailAskEnabled),
      RECEIPT_SENDER: settings.receiptSender,
      ASK_LINK_BASE_URL: `https://${settings.pwaDomain}`,
      ASK_LINK_SECRET_NAME: askLinkSecret.secretName,
      PIDGE_IMAGE_URL: `https://${settings.pwaDomain}/assets/pidge-icon.png`,
      ...buildRetryVoiceEnvironment(settings),
    },
  });
  shared.table.grantReadWriteData(retryFunction);
  retryFunction.addToRolePolicy(buildSendEmailStatement(stack, settings.sendingDomain));
  askLinkSecret.grantRead(retryFunction);
  shared.webSocketStage.grantManagementApiAccess(retryFunction);
  shared.scheduleGroup.grantWriteSchedules(retryFunction);
  shared.schedulerRole.grantPassRole(retryFunction.grantPrincipal);
  retryFunction.grantInvoke(shared.schedulerRole);
  grantWhatsAppSendIfConfigured(retryFunction, settings.whatsappPhoneNumberArn);
  grantRcsSendIfConfigured(retryFunction, settings.rcsAgentArn);
  if (settings.voiceEnabled) {
    // The re-ask call and its scheduled hang-up, on that one instance's contacts only (lambda/shared/voice_ask.py).
    retryFunction.addToRolePolicy(
      new PolicyStatement({
        actions: ["connect:StartOutboundVoiceContact", "connect:StopContact"],
        resources: [`${settings.voiceInstanceArn}/contact/*`],
      }),
    );
  }
  return retryFunction;
}

// pidge-retry's call settings (lambda/shared/voice_ask.py): the real values with voiceEnabled, every one "" while it is off.
function buildRetryVoiceEnvironment(settings: StackSettings): Record<string, string> {
  if (!settings.voiceEnabled) {
    return {
      VOICE_ENABLED: flagEnvironmentValue(false),
      VOICE_INSTANCE_ARN: "",
      VOICE_CONTACT_FLOW_ID: "",
      VOICE_SOURCE_PHONE_NUMBER: "",
      VOICE_CALLS_PER_HOUR: "",
      VOICE_CALL_MAX_SECONDS: "",
    };
  }
  return {
    VOICE_ENABLED: flagEnvironmentValue(true),
    VOICE_INSTANCE_ARN: settings.voiceInstanceArn,
    VOICE_CONTACT_FLOW_ID: settings.voiceContactFlowId,
    VOICE_SOURCE_PHONE_NUMBER: settings.voiceSourcePhoneNumber,
    VOICE_CALLS_PER_HOUR: String(settings.voiceCallsPerHour),
    VOICE_CALL_MAX_SECONDS: String(settings.voiceCallMaxSeconds),
  };
}

// The one reminder to the SIMULATED bank, fired by a schedule booked on Send to my bank.
export function buildCaseFollowUpFunction(stack: Stack, settings: StackSettings, shared: SharedResources): Function {
  const caseFollowUpFunction = buildPythonFunction(stack, "CaseFollowUp", shared.lambdaCode, {
    handler: "pidge_case_followup.handler.handle",
    memorySize: 512,
    timeout: Duration.seconds(15),
    environment: {
      TABLE_NAME: shared.table.tableName,
      RECEIPT_SENDER: settings.receiptSender,
      CASES_ADDRESS: settings.casesAddress,
      WEBSOCKET_ENDPOINT: shared.webSocketStage.callbackUrl,
    },
  });
  shared.table.grantReadWriteData(caseFollowUpFunction);
  shared.webSocketStage.grantManagementApiAccess(caseFollowUpFunction);
  caseFollowUpFunction.addToRolePolicy(buildSendEmailStatement(stack, settings.sendingDomain));
  caseFollowUpFunction.grantInvoke(shared.schedulerRole);
  return caseFollowUpFunction;
}

export interface InboundTopics {
  whatsappInboundTopic: Topic;
  rcsInboundTopic: Topic;
  rcsTwoWayRole: Role;
  smsInboundTopic: Topic | undefined;  // undefined while smsEnabled is off
}

// Where texts and the contact's answers arrive from the phone channels. pidge-verdict subscribes to WhatsApp and RCS (taps,
// statuses, codes); pidge-inbound subscribes to every topic here (texts into the messenger resident's inbox).
export function buildInboundTopics(stack: Stack, smsEnabled: boolean): InboundTopics {
  // WhatsApp: the WABA's one event destination (texts, button taps and delivery statuses) is this topic;
  // End User Messaging Social publishes directly, so the topic policy, not a role, grants it.
  const whatsappInboundTopic = new Topic(stack, "WhatsAppInboundTopic");
  whatsappInboundTopic.addToResourcePolicy(
    new PolicyStatement({
      effect: Effect.ALLOW,
      principals: [new ServicePrincipal("social-messaging.amazonaws.com")],
      actions: ["sns:Publish"],
      resources: [whatsappInboundTopic.topicArn],
      conditions: { StringEquals: { "aws:SourceAccount": stack.account } },
    }),
  );

  // Two-way RCS: chip taps and typed texts land on this topic; End User Messaging assumes the role to publish.
  const rcsInboundTopic = new Topic(stack, "RcsInboundTopic");
  const rcsTwoWayRole = buildTwoWayPublishRole(stack, "RcsTwoWayRole", rcsInboundTopic);
  // Two-way SMS: texts to Pidge's SMS number land on this topic, published the same way as RCS.
  let smsInboundTopic: Topic | undefined;
  if (smsEnabled) {
    smsInboundTopic = new Topic(stack, "SmsInboundTopic");
    buildTwoWayPublishRole(stack, "SmsTwoWayRole", smsInboundTopic);
  }
  return { whatsappInboundTopic, rcsInboundTopic, rcsTwoWayRole, smsInboundTopic };
}

// The role End User Messaging (sms-voice) assumes, from this account only, to publish two-way records to one topic.
function buildTwoWayPublishRole(stack: Stack, constructId: string, inboundTopic: Topic): Role {
  const twoWayRole = new Role(stack, constructId, {
    assumedBy: new ServicePrincipal("sms-voice.amazonaws.com", {
      conditions: { StringEquals: { "aws:SourceAccount": stack.account } },
    }),
  });
  twoWayRole.addToPolicy(
    new PolicyStatement({
      actions: ["sns:Publish"],
      resources: [inboundTopic.topicArn],
      conditions: { StringEquals: { "aws:ResourceAccount": stack.account } },
    }),
  );
  return twoWayRole;
}

// At releaseAt a Yes nobody undid becomes confirmed; with the scheduler off, the next read releases it instead.
export function buildReleaseFunction(stack: Stack, settings: StackSettings, shared: SharedResources): Function {
  const releaseFunction = buildPythonFunction(stack, "Release", shared.lambdaCode, {
    handler: "pidge_release.handler.handle",
    memorySize: 512,
    timeout: Duration.seconds(15),
    environment: {
      TABLE_NAME: shared.table.tableName,
      RECEIPT_SENDER: settings.receiptSender,
      WEBSOCKET_ENDPOINT: shared.webSocketStage.callbackUrl,
    },
  });
  shared.table.grantReadWriteData(releaseFunction);
  shared.webSocketStage.grantManagementApiAccess(releaseFunction);
  releaseFunction.addToRolePolicy(buildSendEmailStatement(stack, settings.sendingDomain));
  releaseFunction.grantInvoke(shared.schedulerRole);
  return releaseFunction;
}

// The one "call Grandma" reminder, fired by a schedule booked when the contact taps the call offer.
export function buildCallReminderFunction(stack: Stack, settings: StackSettings, shared: SharedResources): Function {
  const callReminderFunction = buildPythonFunction(stack, "CallReminder", shared.lambdaCode, {
    handler: "pidge_call_reminder.handler.handle",
    memorySize: 256,
    timeout: Duration.seconds(15),
    environment: {
      TABLE_NAME: shared.table.tableName,
      WHATSAPP_PHONE_NUMBER_ID: settings.whatsappPhoneNumberId,
      WHATSAPP_META_API_VERSION: settings.whatsappMetaApiVersion,
    },
  });
  shared.table.grantReadWriteData(callReminderFunction);
  grantWhatsAppSendIfConfigured(callReminderFunction, settings.whatsappPhoneNumberArn);
  callReminderFunction.grantInvoke(shared.schedulerRole);
  return callReminderFunction;
}

export interface VerdictDependencies {
  topics: InboundTopics;
  releaseFunction: Function;
  caseFollowUpFunction: Function;
  callReminderFunction: Function | undefined;  // undefined while callNudgeEnabled is off
}

// pidge-ask-link: the signed "No, that wasn't me" link in the ask by email, reached through the PWA's CloudFront (/no/*).
// GET shows the button, POST records the No; the HMAC in the link is the only credential (lambda/shared/ask_link.py).
export function buildAskLinkFunction(stack: Stack, settings: StackSettings, shared: SharedResources, askLinkSecret: ISecret): FunctionUrl {
  const askLinkFunction = buildPythonFunction(stack, "AskLink", shared.lambdaCode, {
    handler: "pidge_ask_link.handler.handle",
    memorySize: 256,
    timeout: Duration.seconds(10),
    environment: {
      TABLE_NAME: shared.table.tableName,
      RECEIPT_SENDER: settings.receiptSender,
      WEBSOCKET_ENDPOINT: shared.webSocketStage.callbackUrl,
      ASK_LINK_SECRET_NAME: askLinkSecret.secretName,
    },
  });
  shared.table.grantReadWriteData(askLinkFunction);
  shared.webSocketStage.grantManagementApiAccess(askLinkFunction);
  askLinkFunction.addToRolePolicy(buildSendEmailStatement(stack, settings.sendingDomain));
  askLinkSecret.grantRead(askLinkFunction);
  return askLinkFunction.addFunctionUrl({ authType: FunctionUrlAuthType.NONE });
}

// pidge-verdict: every client action on the socket ($default), every phone-channel answer (the WhatsApp and RCS topics) and
// every reply to the ask by email (S3 asks/).
export function buildVerdictFunction(stack: Stack, settings: StackSettings, shared: SharedResources, dependencies: VerdictDependencies): Function {
  const verdictFunction = buildPythonFunction(stack, "Verdict", shared.lambdaCode, {
    handler: "pidge_verdict.handler.handle",
    memorySize: 512,
    timeout: Duration.seconds(15),
    environment: {
      TABLE_NAME: shared.table.tableName,
      EMAIL_BUCKET: shared.emailBucket.bucketName,
      RECEIPT_SENDER: settings.receiptSender,
      WEBSOCKET_ENDPOINT: shared.webSocketStage.callbackUrl,
      RCS_TOPIC_ARN: dependencies.topics.rcsInboundTopic.topicArn,
      WHATSAPP_TOPIC_ARN: dependencies.topics.whatsappInboundTopic.topicArn,
      RELEASE_SCHEDULER_ENABLED: flagEnvironmentValue(settings.releaseSchedulerEnabled),
      RCS_ENABLED: flagEnvironmentValue(settings.rcsEnabled),
      // The circle sync's sendTo (messenger_send.send_channels_for): phone channels only for this resident, SMS only when on.
      MESSENGER_RESIDENT_ADDRESS: settings.messengerResidentAddress,
      SMS_ENABLED: flagEnvironmentValue(settings.smsEnabled),
      RELEASE_FUNCTION_ARN: dependencies.releaseFunction.functionArn,
      SCHEDULE_GROUP_NAME: shared.scheduleGroup.scheduleGroupName,
      SCHEDULER_ROLE_ARN: shared.schedulerRole.roleArn,
      MOCK_BANK_MAILBOX: settings.mockBankMailbox,
      CASES_ADDRESS: settings.casesAddress,
      BANK_FOLLOW_UP_ENABLED: flagEnvironmentValue(settings.bankFollowUpEnabled),
      BANK_FOLLOW_UP_DELAY_SECONDS: String(settings.bankFollowUpDelaySeconds),
      CASE_FOLLOWUP_FUNCTION_ARN: dependencies.caseFollowUpFunction.functionArn,
      CHAT_ENABLED: flagEnvironmentValue(settings.chatEnabled),
      CONTACT_VERIFICATION_MOCK: flagEnvironmentValue(settings.contactVerificationMock),
      PWA_BASE_URL: `https://${settings.pwaDomain}`,
      PIDGE_WHATSAPP_NUMBER: settings.whatsappDisplayNumber,
      WHATSAPP_ENABLED: flagEnvironmentValue(settings.whatsappEnabled),
      EMAIL_ASK_ENABLED: flagEnvironmentValue(settings.emailAskEnabled),
      WHATSAPP_PHONE_NUMBER_ID: settings.whatsappPhoneNumberId,
      WHATSAPP_META_API_VERSION: settings.whatsappMetaApiVersion,
      CALL_NUDGE_ENABLED: flagEnvironmentValue(settings.callNudgeEnabled),
      CALL_NUDGES_PER_CONTACT_PER_HOUR: String(settings.callNudgesPerContactPerHour),
      CALL_REMINDER_TODAY_DELAY_SECONDS: String(settings.callReminderTodayDelaySeconds),
      CALL_REMINDER_TOMORROW_DELAY_SECONDS: String(settings.callReminderTomorrowDelaySeconds),
      ...(dependencies.callReminderFunction ? { CALL_REMINDER_FUNCTION_ARN: dependencies.callReminderFunction.functionArn } : {}),
      // The keypad answer's flow event must name this instance (lambda/pidge_verdict/voice_answers.py); "" while voice is off.
      VOICE_ENABLED: flagEnvironmentValue(settings.voiceEnabled),
      VOICE_INSTANCE_ARN: settings.voiceEnabled ? settings.voiceInstanceArn : "",
    },
  });
  shared.table.grantReadWriteData(verdictFunction);
  shared.webSocketStage.grantManagementApiAccess(verdictFunction);
  verdictFunction.addToRolePolicy(
    new PolicyStatement({
      actions: ["s3:PutObjectTagging"],
      resources: [shared.emailBucket.arnForObjects(`${INBOX_PREFIX}*`)],
    }),
  );
  verdictFunction.addToRolePolicy(buildSendEmailStatement(stack, settings.sendingDomain));
  shared.emailBucket.grantRead(verdictFunction, `${ASKS_PREFIX}*`);
  shared.emailBucket.addEventNotification(EventType.OBJECT_CREATED, new LambdaDestination(verdictFunction), { prefix: ASKS_PREFIX });
  shared.scheduleGroup.grantWriteSchedules(verdictFunction);
  shared.schedulerRole.grantPassRole(verdictFunction.grantPrincipal);
  grantWhatsAppSendIfConfigured(verdictFunction, settings.whatsappPhoneNumberArn);
  shared.webSocketApi.addRoute("$default", {
    integration: new WebSocketLambdaIntegration("DefaultIntegration", verdictFunction),
  });
  dependencies.topics.rcsInboundTopic.addSubscription(new LambdaSubscription(verdictFunction));
  dependencies.topics.whatsappInboundTopic.addSubscription(new LambdaSubscription(verdictFunction));
  if (settings.voiceEnabled) {
    // The Seoul flow "pidge-ask" invokes this us-east-1 function by ARN (cross-Region, D2); only from our instance and account.
    verdictFunction.addPermission("ConnectFlowInvoke", {
      principal: new ServicePrincipal("connect.amazonaws.com"),
      sourceArn: settings.voiceInstanceArn,
      sourceAccount: stack.account,
    });
  }
  return verdictFunction;
}
