import { Duration, Stack } from "aws-cdk-lib";
import { WebSocketLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { Effect, PolicyStatement, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { Function } from "aws-cdk-lib/aws-lambda";
import { Topic } from "aws-cdk-lib/aws-sns";
import { LambdaSubscription } from "aws-cdk-lib/aws-sns-subscriptions";
import { INBOX_PREFIX } from "./mail-ingress.js";
import { buildPythonFunction, buildSendEmailStatement, grantRcsSendIfConfigured, grantWhatsAppSendIfConfigured } from "./python-function.js";
import { SharedResources } from "./realtime.js";
import { StackSettings, flagEnvironmentValue } from "./stack-settings.js";

// The re-ask timer: at +reAskDelay an unanswered ask is re-sent once; a second silence is marked timeout.
export function buildRetryFunction(stack: Stack, settings: StackSettings, shared: SharedResources): Function {
  const retryFunction = buildPythonFunction(stack, "Retry", shared.lambdaCode, {
    handler: "pidge_retry.handler.handle",
    memorySize: 512,
    timeout: Duration.seconds(30),
    environment: {
      TABLE_NAME: shared.table.tableName,
      RECEIVER_USER_ID: settings.receiverUserId,
      RECEIVER_NAME: settings.receiverName,
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
      PIDGE_IMAGE_URL: `https://${settings.pwaDomain}/assets/pidge-icon.png`,
    },
  });
  shared.table.grantReadWriteData(retryFunction);
  shared.webSocketStage.grantManagementApiAccess(retryFunction);
  shared.scheduleGroup.grantWriteSchedules(retryFunction);
  shared.schedulerRole.grantPassRole(retryFunction.grantPrincipal);
  retryFunction.grantInvoke(shared.schedulerRole);
  grantWhatsAppSendIfConfigured(retryFunction, settings.whatsappPhoneNumberArn);
  grantRcsSendIfConfigured(retryFunction, settings.rcsAgentArn);
  return retryFunction;
}

// The one reminder to the SIMULATED bank, fired by a schedule booked on Send to my bank.
export function buildCaseFollowUpFunction(stack: Stack, settings: StackSettings, shared: SharedResources): Function {
  const caseFollowUpFunction = buildPythonFunction(stack, "CaseFollowUp", shared.lambdaCode, {
    handler: "pidge_case_followup.handler.handle",
    memorySize: 512,
    timeout: Duration.seconds(15),
    environment: {
      TABLE_NAME: shared.table.tableName,
      RECEIVER_USER_ID: settings.receiverUserId,
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
}

// Where the contact's answers arrive from the phone channels; pidge-verdict subscribes to both topics.
export function buildInboundTopics(stack: Stack): InboundTopics {
  // WhatsApp: the WABA's one event destination (button taps and delivery statuses) is this topic;
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

  // Two-way RCS: chip taps land on this topic; End User Messaging assumes the role to publish.
  const rcsInboundTopic = new Topic(stack, "RcsInboundTopic");
  const rcsTwoWayRole = new Role(stack, "RcsTwoWayRole", {
    assumedBy: new ServicePrincipal("sms-voice.amazonaws.com", {
      conditions: { StringEquals: { "aws:SourceAccount": stack.account } },
    }),
  });
  rcsTwoWayRole.addToPolicy(
    new PolicyStatement({
      actions: ["sns:Publish"],
      resources: [rcsInboundTopic.topicArn],
      conditions: { StringEquals: { "aws:ResourceAccount": stack.account } },
    }),
  );
  return { whatsappInboundTopic, rcsInboundTopic, rcsTwoWayRole };
}

// At releaseAt a Yes nobody undid becomes confirmed; with the scheduler off, the next read releases it instead.
export function buildReleaseFunction(stack: Stack, settings: StackSettings, shared: SharedResources): Function {
  const releaseFunction = buildPythonFunction(stack, "Release", shared.lambdaCode, {
    handler: "pidge_release.handler.handle",
    memorySize: 512,
    timeout: Duration.seconds(15),
    environment: {
      TABLE_NAME: shared.table.tableName,
      RECEIVER_USER_ID: settings.receiverUserId,
      RECEIVER_NAME: settings.receiverName,
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
      RECEIVER_NAME: settings.receiverName,
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
  callReminderFunction: Function;
}

// pidge-verdict: every client action on the socket ($default) and every phone-channel answer (both topics).
export function buildVerdictFunction(stack: Stack, settings: StackSettings, shared: SharedResources, dependencies: VerdictDependencies): Function {
  const verdictFunction = buildPythonFunction(stack, "Verdict", shared.lambdaCode, {
    handler: "pidge_verdict.handler.handle",
    memorySize: 512,
    timeout: Duration.seconds(15),
    environment: {
      TABLE_NAME: shared.table.tableName,
      RECEIVER_ADDRESS: settings.receiverAddress,
      RECEIVER_USER_ID: settings.receiverUserId,
      RECEIVER_NAME: settings.receiverName,
      EMAIL_BUCKET: shared.emailBucket.bucketName,
      RECEIPT_SENDER: settings.receiptSender,
      WEBSOCKET_ENDPOINT: shared.webSocketStage.callbackUrl,
      RCS_TOPIC_ARN: dependencies.topics.rcsInboundTopic.topicArn,
      WHATSAPP_TOPIC_ARN: dependencies.topics.whatsappInboundTopic.topicArn,
      RELEASE_SCHEDULER_ENABLED: flagEnvironmentValue(settings.releaseSchedulerEnabled),
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
      WHATSAPP_PHONE_NUMBER_ID: settings.whatsappPhoneNumberId,
      WHATSAPP_META_API_VERSION: settings.whatsappMetaApiVersion,
      CALL_NUDGE_ENABLED: flagEnvironmentValue(settings.callNudgeEnabled),
      CALL_NUDGES_PER_CONTACT_PER_HOUR: String(settings.callNudgesPerContactPerHour),
      CALL_REMINDER_TODAY_DELAY_SECONDS: String(settings.callReminderTodayDelaySeconds),
      CALL_REMINDER_TOMORROW_DELAY_SECONDS: String(settings.callReminderTomorrowDelaySeconds),
      CALL_REMINDER_FUNCTION_ARN: dependencies.callReminderFunction.functionArn,
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
  shared.scheduleGroup.grantWriteSchedules(verdictFunction);
  shared.schedulerRole.grantPassRole(verdictFunction.grantPrincipal);
  grantWhatsAppSendIfConfigured(verdictFunction, settings.whatsappPhoneNumberArn);
  shared.webSocketApi.addRoute("$default", {
    integration: new WebSocketLambdaIntegration("DefaultIntegration", verdictFunction),
  });
  dependencies.topics.rcsInboundTopic.addSubscription(new LambdaSubscription(verdictFunction));
  dependencies.topics.whatsappInboundTopic.addSubscription(new LambdaSubscription(verdictFunction));
  return verdictFunction;
}
