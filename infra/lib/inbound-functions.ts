import { Duration, Stack } from "aws-cdk-lib";
import { PolicyStatement } from "aws-cdk-lib/aws-iam";
import { Function } from "aws-cdk-lib/aws-lambda";
import { SqsDestination } from "aws-cdk-lib/aws-lambda-destinations";
import { EventType } from "aws-cdk-lib/aws-s3";
import { LambdaDestination } from "aws-cdk-lib/aws-s3-notifications";
import { ISecret } from "aws-cdk-lib/aws-secretsmanager";
import { LambdaSubscription } from "aws-cdk-lib/aws-sns-subscriptions";
import { Queue } from "aws-cdk-lib/aws-sqs";
import { CASES_PREFIX, PIDGE_QUESTION_PREFIX } from "./mail-ingress.js";
import {
  BEDROCK_INVOKE_ACTIONS, buildPythonFunction, buildSendEmailStatement, claudeModelResourceArns, grantRcsSendIfConfigured,
  grantWhatsAppSendIfConfigured,
} from "./python-function.js";
import { SharedResources } from "./realtime.js";
import { StackSettings, flagEnvironmentValue } from "./stack-settings.js";
import { InboundTopics } from "./verdict-functions.js";

// A bank reply under cases/: matched by token, stored verbatim, shown to Grandma (in-app + Web Push), Jimin nudged. Never the Reader.
export function buildCaseInboundFunction(stack: Stack, shared: SharedResources, vapidSecret: ISecret): Function {
  const caseInboundFunction = buildPythonFunction(stack, "CaseInbound", shared.lambdaCode, {
    handler: "pidge_case_inbound.handler.handle",
    memorySize: 512,
    timeout: Duration.seconds(30),
    layers: [shared.dependencyLayer],  // pywebpush for the lock-screen notice
    environment: {
      TABLE_NAME: shared.table.tableName,
      WEBSOCKET_ENDPOINT: shared.webSocketStage.callbackUrl,
      VAPID_SECRET_NAME: vapidSecret.secretName,
    },
  });
  shared.table.grantReadWriteData(caseInboundFunction);
  vapidSecret.grantRead(caseInboundFunction);
  shared.webSocketStage.grantManagementApiAccess(caseInboundFunction);
  shared.emailBucket.grantRead(caseInboundFunction, `${CASES_PREFIX}*`);
  shared.emailBucket.addEventNotification(EventType.OBJECT_CREATED, new LambdaDestination(caseInboundFunction), { prefix: CASES_PREFIX });
  return caseInboundFunction;
}

// A question mailed to pidge@ under pidge/: exact sender + aligned DKIM → the CHAT# row; no Bedrock, no layer.
export function buildQuestionInboundFunction(stack: Stack, settings: StackSettings, shared: SharedResources): Function {
  const questionInboundFunction = buildPythonFunction(stack, "QuestionInbound", shared.lambdaCode, {
    handler: "pidge_question_inbound.handler.handle",
    memorySize: 256,
    timeout: Duration.seconds(15),
    environment: {
      TABLE_NAME: shared.table.tableName,
      RECEIVER_MAILBOX: settings.receiverMailbox,
      MAILBOX_RESIDENT_ADDRESS: settings.mailboxResidentAddress,
      CHAT_ENABLED: flagEnvironmentValue(settings.chatEnabled),
    },
  });
  shared.table.grantReadWriteData(questionInboundFunction);
  shared.emailBucket.grantRead(questionInboundFunction, `${PIDGE_QUESTION_PREFIX}*`);
  shared.emailBucket.addEventNotification(EventType.OBJECT_CREATED, new LambdaDestination(questionInboundFunction), { prefix: PIDGE_QUESTION_PREFIX });
  return questionInboundFunction;
}

// The environment lambda/shared/pipeline.py needs: it builds the Reader at import, then holds, asks, books the re-ask and pushes.
function buildPipelineEnvironment(
  settings: StackSettings, shared: SharedResources, retryFunction: Function, askLinkSecret: ISecret,
): Record<string, string> {
  return {
    TABLE_NAME: shared.table.tableName,
    READER_MODEL: settings.readerModel,
    ASKS_PER_CONTACT_PER_HOUR: String(settings.asksPerContactPerHour),
    LIVE_FEEDS_ENABLED: flagEnvironmentValue(settings.liveFeedsEnabled),
    ESCALATION_ENABLED: flagEnvironmentValue(settings.escalationEnabled),
    ESCALATION_MODEL: settings.escalationModel,
    RETRY_SCHEDULER_ENABLED: flagEnvironmentValue(settings.retrySchedulerEnabled),
    RE_ASK_DELAY_SECONDS: String(settings.reAskDelaySeconds),
    RETRY_FUNCTION_ARN: retryFunction.functionArn,
    SCHEDULE_GROUP_NAME: shared.scheduleGroup.scheduleGroupName,
    SCHEDULER_ROLE_ARN: shared.schedulerRole.roleArn,
    WEBSOCKET_ENDPOINT: shared.webSocketStage.callbackUrl,
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
  };
}

// What a pipeline caller may do: the table, the ask by email (with its signed No link), WhatsApp and RCS when configured, the
// re-ask schedule, socket pushes and the Reader and reviewer models. No stored email: the Reader function adds that read itself.
function grantPipelineAccess(
  stack: Stack, settings: StackSettings, shared: SharedResources, pipelineFunction: Function, askLinkSecret: ISecret,
): void {
  pipelineFunction.addToRolePolicy(buildSendEmailStatement(stack, settings.sendingDomain));
  askLinkSecret.grantRead(pipelineFunction);
  shared.table.grantReadWriteData(pipelineFunction);
  shared.scheduleGroup.grantWriteSchedules(pipelineFunction);
  shared.schedulerRole.grantPassRole(pipelineFunction.grantPrincipal);
  shared.webSocketStage.grantManagementApiAccess(pipelineFunction);
  pipelineFunction.addToRolePolicy(new PolicyStatement({ actions: BEDROCK_INVOKE_ACTIONS, resources: claudeModelResourceArns(stack) }));
  grantRcsSendIfConfigured(pipelineFunction, settings.rcsAgentArn);
  grantWhatsAppSendIfConfigured(pipelineFunction, settings.whatsappPhoneNumberArn);
}

// pidge-reader, invoked by Mail Manager (mail-ingress.ts store-raw-email) once per email to an enrolled resident: the Reader
// agent, the policy, the hold and the first ask.
export function buildReaderFunction(
  stack: Stack, settings: StackSettings, shared: SharedResources, retryFunction: Function, askLinkSecret: ISecret,
): { readerFunction: Function; readerDeadLetterQueue: Queue } {
  const readerDeadLetterQueue = new Queue(stack, "ReaderDeadLetterQueue", {
    retentionPeriod: Duration.days(14),
    enforceSSL: true,
  });

  const readerFunction = buildPythonFunction(stack, "Reader", shared.lambdaCode, {
    handler: "pidge_reader.handler.handle",
    layers: [shared.dependencyLayer],
    memorySize: 1024,
    timeout: Duration.seconds(60),
    reservedConcurrentExecutions: 5,
    retryAttempts: 2,
    onFailure: new SqsDestination(readerDeadLetterQueue),
    environment: {
      ...buildPipelineEnvironment(settings, shared, retryFunction, askLinkSecret),
      EMAIL_BUCKET: shared.emailBucket.bucketName,
    },
  });
  shared.emailBucket.grantRead(readerFunction);
  grantPipelineAccess(stack, settings, shared, readerFunction, askLinkSecret);
  return { readerFunction, readerDeadLetterQueue };
}

// pidge-inbound, subscribed to every inbound topic: each WhatsApp, RCS or SMS text to Pidge's numbers becomes a MESSAGE# row in
// the messenger resident's inbox through the same pipeline as email. Taps and statuses are pidge-verdict's; it skips them.
// Anyone can text Pidge and each text is a Reader call, so reserved concurrency and the ask cap bound the spend.
export function buildInboundMessageFunction(
  stack: Stack, settings: StackSettings, shared: SharedResources, retryFunction: Function, askLinkSecret: ISecret, topics: InboundTopics,
): { inboundMessageFunction: Function; inboundMessageDeadLetterQueue: Queue } {
  const inboundMessageDeadLetterQueue = new Queue(stack, "InboundMessageDeadLetterQueue", {
    retentionPeriod: Duration.days(14),
    enforceSSL: true,
  });

  const inboundMessageFunction = buildPythonFunction(stack, "InboundMessage", shared.lambdaCode, {
    handler: "pidge_inbound.handler.handle",
    layers: [shared.dependencyLayer],
    memorySize: 1024,
    timeout: Duration.seconds(60),
    reservedConcurrentExecutions: 2,
    retryAttempts: 2,
    onFailure: new SqsDestination(inboundMessageDeadLetterQueue),
    environment: {
      ...buildPipelineEnvironment(settings, shared, retryFunction, askLinkSecret),
      WHATSAPP_TOPIC_ARN: topics.whatsappInboundTopic.topicArn,
      RCS_TOPIC_ARN: topics.rcsInboundTopic.topicArn,
      SMS_TOPIC_ARN: topics.smsInboundTopic === undefined ? "" : topics.smsInboundTopic.topicArn,  // "" → no SMS parser
      SMS_ORIGINATION_NUMBER: settings.smsOriginationNumber,
      MESSENGER_RESIDENT_ADDRESS: settings.messengerResidentAddress,
    },
  });
  grantPipelineAccess(stack, settings, shared, inboundMessageFunction, askLinkSecret);
  // LambdaSubscription also adds the invoke permission for sns.amazonaws.com scoped to that topic's ARN (SourceArn).
  for (const inboundTopic of [topics.whatsappInboundTopic, topics.rcsInboundTopic, topics.smsInboundTopic]) {
    if (inboundTopic !== undefined) inboundTopic.addSubscription(new LambdaSubscription(inboundMessageFunction));
  }
  return { inboundMessageFunction, inboundMessageDeadLetterQueue };
}
