import { Duration, Stack } from "aws-cdk-lib";
import { WebSocketLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { PolicyStatement } from "aws-cdk-lib/aws-iam";
import { Function } from "aws-cdk-lib/aws-lambda";
import { TABLE_READ_ACTIONS, buildLeadingKeysWriteStatement } from "./org-functions.js";
import { buildPythonFunction, buildSendEmailStatement, grantWhatsAppSendIfConfigured } from "./python-function.js";
import { SharedResources } from "./realtime.js";
import { StackSettings, flagEnvironmentValue } from "./stack-settings.js";

// lambda/shared/enums.py ClientAction.SEND_MESSAGE: the socket routes on $request.body.action, so this key reaches SendMessage only.
const SEND_MESSAGE_ROUTE_KEY = "send_message";
// pidge-send puts the out row and marks it sent or failed (MESSAGE#) and takes the hourly send slot (SENDRATE#); nothing else.
const SEND_MESSAGE_WRITE_ACTIONS = ["dynamodb:PutItem", "dynamodb:UpdateItem"];
const SEND_MESSAGE_LEADING_KEYS = ["MESSAGE#*", "SENDRATE#*"];

// sms-voice:SendTextMessage from Pidge's SMS number (only with smsEnabled) and its RCS agent (only when the ARN is set).
function grantTextSendIfConfigured(sendMessageFunction: Function, settings: StackSettings): void {
  const originationArns = [settings.smsEnabled ? settings.smsPhoneNumberArn : "", settings.rcsAgentArn].filter((arn) => arn !== "");
  if (originationArns.length > 0) {
    sendMessageFunction.addToRolePolicy(new PolicyStatement({ actions: ["sms-voice:SendTextMessage"], resources: originationArns }));
  }
}

// pidge-send: the resident's own message to someone in her circle, on its own socket route apart from pidge-verdict's $default.
// It reads the table, writes only MESSAGE# and SENDRATE# rows, sends email only From an address on the receiver domain and
// never deletes a connection row (it pushes with should_remove_gone_connections=False). No Bedrock, no S3.
export function buildSendMessageFunction(stack: Stack, settings: StackSettings, shared: SharedResources): Function {
  const sendMessageFunction = buildPythonFunction(stack, "SendMessage", shared.lambdaCode, {
    handler: "pidge_send.handler.handle",
    memorySize: 512,
    timeout: Duration.seconds(15),
    environment: {
      TABLE_NAME: shared.table.tableName,
      WEBSOCKET_ENDPOINT: shared.webSocketStage.callbackUrl,
      MESSENGER_RESIDENT_ADDRESS: settings.messengerResidentAddress,
      WHATSAPP_ENABLED: flagEnvironmentValue(settings.whatsappEnabled),
      WHATSAPP_PHONE_NUMBER_ID: settings.whatsappPhoneNumberId,
      WHATSAPP_META_API_VERSION: settings.whatsappMetaApiVersion,
      RCS_ENABLED: flagEnvironmentValue(settings.rcsEnabled),
      RCS_AGENT_ARN: settings.rcsAgentArn,
      SMS_ENABLED: flagEnvironmentValue(settings.smsEnabled),
      SMS_PHONE_NUMBER_ARN: settings.smsPhoneNumberArn,
      RECEIPT_SENDER: settings.receiptSender,  // its domain, like the resident's own, is refused as a recipient (a mail loop)
    },
  });
  sendMessageFunction.addToRolePolicy(new PolicyStatement({ actions: TABLE_READ_ACTIONS, resources: [shared.table.tableArn] }));
  sendMessageFunction.addToRolePolicy(
    buildLeadingKeysWriteStatement(shared.table.tableArn, SEND_MESSAGE_WRITE_ACTIONS, SEND_MESSAGE_LEADING_KEYS),
  );
  // From is the caller's own RECEIVER# address (receiverDomain); the SES identity is the sending domain (the same domain today).
  sendMessageFunction.addToRolePolicy(
    buildSendEmailStatement(stack, settings.sendingDomain, { StringLike: { "ses:FromAddress": `*@${settings.receiverDomain}` } }),
  );
  shared.webSocketStage.grantManagementApiAccess(sendMessageFunction);
  grantWhatsAppSendIfConfigured(sendMessageFunction, settings.whatsappPhoneNumberArn);
  grantTextSendIfConfigured(sendMessageFunction, settings);
  shared.webSocketApi.addRoute(SEND_MESSAGE_ROUTE_KEY, {
    integration: new WebSocketLambdaIntegration("SendMessageIntegration", sendMessageFunction),
  });
  return sendMessageFunction;
}
