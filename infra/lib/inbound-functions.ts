import { Duration, Stack } from "aws-cdk-lib";
import { PolicyStatement } from "aws-cdk-lib/aws-iam";
import { Function } from "aws-cdk-lib/aws-lambda";
import { SqsDestination } from "aws-cdk-lib/aws-lambda-destinations";
import { EventType } from "aws-cdk-lib/aws-s3";
import { LambdaDestination } from "aws-cdk-lib/aws-s3-notifications";
import { ISecret } from "aws-cdk-lib/aws-secretsmanager";
import { Queue } from "aws-cdk-lib/aws-sqs";
import { CASES_PREFIX, INBOX_PREFIX, PIDGE_QUESTION_PREFIX } from "./mail-ingress.js";
import {
  BEDROCK_INVOKE_ACTIONS, buildPythonFunction, buildSendEmailStatement, claudeModelResourceArns, grantRcsSendIfConfigured,
  grantWhatsAppSendIfConfigured,
} from "./python-function.js";
import { SharedResources } from "./realtime.js";
import { StackSettings, flagEnvironmentValue } from "./stack-settings.js";

// A bank reply under cases/: matched by token, stored verbatim, shown to Grandma (in-app + Web Push), Jimin nudged. Never the Reader.
export function buildCaseInboundFunction(stack: Stack, settings: StackSettings, shared: SharedResources, vapidSecret: ISecret): Function {
  const caseInboundFunction = buildPythonFunction(stack, "CaseInbound", shared.lambdaCode, {
    handler: "pidge_case_inbound.handler.handle",
    memorySize: 512,
    timeout: Duration.seconds(30),
    layers: [shared.dependencyLayer],  // pywebpush for the lock-screen notice
    environment: {
      TABLE_NAME: shared.table.tableName,
      RECEIVER_USER_ID: settings.receiverUserId,
      RECEIVER_NAME: settings.receiverName,
      RECEIVER_ADDRESS: settings.receiverAddress,
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
      RECEIVER_USER_ID: settings.receiverUserId,
      RECEIVER_MAILBOX: settings.receiverMailbox,
      CHAT_ENABLED: flagEnvironmentValue(settings.chatEnabled),
    },
  });
  shared.table.grantReadWriteData(questionInboundFunction);
  shared.emailBucket.grantRead(questionInboundFunction, `${PIDGE_QUESTION_PREFIX}*`);
  shared.emailBucket.addEventNotification(EventType.OBJECT_CREATED, new LambdaDestination(questionInboundFunction), { prefix: PIDGE_QUESTION_PREFIX });
  return questionInboundFunction;
}

// pidge-reader on every new object under inbox/: the Reader agent, the policy, the hold and the first ask.
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
      TABLE_NAME: shared.table.tableName,
      READER_MODEL: settings.readerModel,
      ASKS_PER_CONTACT_PER_HOUR: String(settings.asksPerContactPerHour),
      ESCALATION_ENABLED: flagEnvironmentValue(settings.escalationEnabled),
      ESCALATION_MODEL: settings.escalationModel,
      RETRY_SCHEDULER_ENABLED: flagEnvironmentValue(settings.retrySchedulerEnabled),
      RE_ASK_DELAY_SECONDS: String(settings.reAskDelaySeconds),
      RETRY_FUNCTION_ARN: retryFunction.functionArn,
      SCHEDULE_GROUP_NAME: shared.scheduleGroup.scheduleGroupName,
      SCHEDULER_ROLE_ARN: shared.schedulerRole.roleArn,
      RECEIVER_ADDRESS: settings.receiverAddress,
      RECEIVER_USER_ID: settings.receiverUserId,
      RECEIVER_NAME: settings.receiverName,
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
    },
  });
  shared.emailBucket.grantRead(readerFunction);
  readerFunction.addToRolePolicy(buildSendEmailStatement(stack, settings.sendingDomain));
  askLinkSecret.grantRead(readerFunction);
  shared.table.grantReadWriteData(readerFunction);
  shared.scheduleGroup.grantWriteSchedules(readerFunction);
  shared.schedulerRole.grantPassRole(readerFunction.grantPrincipal);
  shared.webSocketStage.grantManagementApiAccess(readerFunction);
  readerFunction.addToRolePolicy(new PolicyStatement({ actions: BEDROCK_INVOKE_ACTIONS, resources: claudeModelResourceArns(stack) }));
  shared.emailBucket.addEventNotification(EventType.OBJECT_CREATED, new LambdaDestination(readerFunction), {
    prefix: INBOX_PREFIX,
  });
  grantRcsSendIfConfigured(readerFunction, settings.rcsAgentArn);
  grantWhatsAppSendIfConfigured(readerFunction, settings.whatsappPhoneNumberArn);
  return { readerFunction, readerDeadLetterQueue };
}
