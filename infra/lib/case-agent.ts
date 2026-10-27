import { existsSync, readFileSync, readdirSync } from "node:fs";
import { Duration, Stack } from "aws-cdk-lib";
import * as agentcore from "aws-cdk-lib/aws-bedrockagentcore";
import { CfnGuardrail, CfnGuardrailVersion } from "aws-cdk-lib/aws-bedrock";
import { Alarm, ComparisonOperator, TreatMissingData } from "aws-cdk-lib/aws-cloudwatch";
import { Table } from "aws-cdk-lib/aws-dynamodb";
import { PolicyStatement } from "aws-cdk-lib/aws-iam";
import { FilterCriteria, FilterRule, Function, StartingPosition } from "aws-cdk-lib/aws-lambda";
import { DynamoEventSource, SqsDlq } from "aws-cdk-lib/aws-lambda-event-sources";
import { ISecret } from "aws-cdk-lib/aws-secretsmanager";
import { Queue, QueueEncryption } from "aws-cdk-lib/aws-sqs";
import { BEDROCK_INVOKE_ACTIONS, buildPythonFunction, buildSendEmailStatement, claudeModelResourceArns } from "./python-function.js";
import { SharedResources } from "./realtime.js";
import { StackSettings, flagEnvironmentValue } from "./stack-settings.js";

// The AgentCore zip is built by scripts/build_case_agent.sh; a deploy from a stale build ships old shared code.
function assertCaseAgentBuildIsCurrent(buildDirectory: string): string {
  const sourceFiles: Array<[string, string]> = [["../agent/pidge_case/main.py", `${buildDirectory}/main.py`]];
  for (const fileName of readdirSync("../lambda/shared").filter((name) => name.endsWith(".py"))) {
    sourceFiles.push([`../lambda/shared/${fileName}`, `${buildDirectory}/shared/${fileName}`]);
  }
  const staleFiles = sourceFiles.filter(([sourcePath, builtPath]) =>
    !existsSync(builtPath) || readFileSync(sourcePath, "utf8") !== readFileSync(builtPath, "utf8"));
  if (staleFiles.length > 0) {
    throw new Error(`case agent build is stale (${staleFiles.map(([sourcePath]) => sourcePath).join(", ")}); run: npm run build-agent`);
  }
  return buildDirectory;
}

export interface CaseAgent {
  caseAgentMemory: agentcore.Memory;
  caseAgentRuntime: agentcore.Runtime;
}

// The Pidge case agent on Amazon Bedrock AgentCore: Runtime (direct code deploy, arm64 zip built by
// infra/scripts/build_case_agent.sh) plus Memory (actor = the receiver, one family's facts). The runtime role
// reads the table (never Scan, never S3) and calls Haiku; code in pidge-dispatch still writes every row.
export function buildCaseAgent(stack: Stack, settings: StackSettings, table: Table): CaseAgent {
  const caseAgentMemory = new agentcore.Memory(stack, "CaseAgentMemory", {
    memoryName: "sendpidge_case_agent",
    description: "What Pidge remembers about one family's cases",
    expirationDuration: Duration.days(30),
    memoryStrategies: [
      agentcore.MemoryStrategy.usingSemantic({ strategyName: "family_facts", namespaces: ["/pidge/families/{actorId}/facts"] }),
    ],
  });
  const caseAgentRuntime = new agentcore.Runtime(stack, "CaseAgentRuntime", {
    runtimeName: "sendpidge_case_agent",
    description: "Pidge case agent: reads one family's case rows, writes the case note, drafts the recovery pack",
    agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromCodeAsset({
      path: assertCaseAgentBuildIsCurrent("build/case_agent"),
      runtime: agentcore.AgentCoreRuntime.PYTHON_3_12,
      entrypoint: ["opentelemetry-instrument", "main.py"],
      exclude: ["**/__pycache__"],
    }),
    environmentVariables: {
      TABLE_NAME: table.tableName,
      CASE_AGENT_MODEL: settings.caseAgentModel,
      CASE_AGENT_MEMORY_ID: caseAgentMemory.memoryId,
      // Spans to the shared aws/spans group (X-Ray already may write there); the per-agent default needs
      // logs:PutResourcePolicy on the execution role, and without it every span was dropped.
      UNIFIED_TRACES_DESTINATION_ENABLED: "false",
    },
  });
  caseAgentRuntime.grant(
    ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:BatchGetItem"],
    [table.tableArn, `${table.tableArn}/index/UserIndex`],
  );
  caseAgentRuntime.grant(BEDROCK_INVOKE_ACTIONS, claudeModelResourceArns(stack));
  caseAgentMemory.grantWrite(caseAgentRuntime);
  caseAgentMemory.grantRead(caseAgentRuntime);
  return { caseAgentMemory, caseAgentRuntime };
}

export interface ChatGuardrail {
  chatGuardrail: CfnGuardrail;
  chatGuardrailVersion: CfnGuardrailVersion;
}

// Bedrock Guardrails prompt-attack filter on Grandma's chat questions (input only), pinned to a numbered version.
export function buildChatGuardrail(stack: Stack): ChatGuardrail {
  const chatGuardrail = new CfnGuardrail(stack, "ChatGuardrail", {
    name: "sendpidge-chat-prompt-attack",
    description: "Blocks prompt attacks in questions typed to Pidge",
    contentPolicyConfig: { filtersConfig: [{ type: "PROMPT_ATTACK", inputStrength: "HIGH", outputStrength: "NONE" }] },
    blockedInputMessaging: "I can only help with your own emails and cases.",
    blockedOutputsMessaging: "I can only help with your own emails and cases.",
  });
  const chatGuardrailVersion = new CfnGuardrailVersion(stack, "ChatGuardrailVersion", {
    guardrailIdentifier: chatGuardrail.attrGuardrailId,
    description: "PROMPT_ATTACK HIGH on input",
  });
  return { chatGuardrail, chatGuardrailVersion };
}

// A stream record pidge-dispatch still fails after its retries (agent run error, AgentCore error) lands here instead of
// being dropped, and one message raises the alarm.
function buildDispatchDeadLetterQueue(stack: Stack): Queue {
  const dispatchDeadLetterQueue = new Queue(stack, "DispatchDeadLetterQueue", {
    encryption: QueueEncryption.SQS_MANAGED,
    retentionPeriod: Duration.days(14),
    enforceSSL: true,
  });
  new Alarm(stack, "DispatchDeadLetterAlarm", {
    alarmDescription: "pidge-dispatch gave up on a stream record after retries",
    metric: dispatchDeadLetterQueue.metricApproximateNumberOfMessagesVisible({ period: Duration.minutes(5) }),
    threshold: 1,
    evaluationPeriods: 1,
    comparisonOperator: ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    treatMissingData: TreatMissingData.NOT_BREACHING,
  });
  return dispatchDeadLetterQueue;
}

// pidge-dispatch wakes on the table stream and asks the case agent (on AgentCore, or in-process in lambda mode)
// for the note, then writes the NOTE# row and pushes it to Grandma.
export function buildDispatchFunction(
  stack: Stack, settings: StackSettings, shared: SharedResources, caseAgent: CaseAgent, guardrail: ChatGuardrail, vapidSecret: ISecret,
): Function {
  const dispatchFunction = buildPythonFunction(stack, "Dispatch", shared.lambdaCode, {
    handler: "pidge_dispatch.handler.handle",
    layers: [shared.dependencyLayer],
    memorySize: 1024,
    timeout: Duration.seconds(90),
    environment: {
      TABLE_NAME: shared.table.tableName,
      RECEIVER_USER_ID: settings.receiverUserId,
      CASE_AGENT_RUNTIME: settings.caseAgentRuntimeMode,
      CASE_AGENT_MODEL: settings.caseAgentModel,
      CASE_AGENT_RUNTIME_ARN: caseAgent.caseAgentRuntime.agentRuntimeArn,
      WEBSOCKET_ENDPOINT: shared.webSocketStage.callbackUrl,
      CHAT_GUARDRAIL_ENABLED: flagEnvironmentValue(settings.chatGuardrailEnabled),
      CHAT_GUARDRAIL_ID: guardrail.chatGuardrail.attrGuardrailId,
      CHAT_GUARDRAIL_VERSION: guardrail.chatGuardrailVersion.attrVersion,
      RECEIVER_MAILBOX: settings.receiverMailbox,
      RECEIPT_SENDER: settings.receiptSender,
      VAPID_SECRET_NAME: vapidSecret.secretName,
    },
  });
  shared.table.grantReadWriteData(dispatchFunction);
  vapidSecret.grantRead(dispatchFunction);
  shared.webSocketStage.grantManagementApiAccess(dispatchFunction);
  dispatchFunction.addToRolePolicy(new PolicyStatement({ actions: BEDROCK_INVOKE_ACTIONS, resources: claudeModelResourceArns(stack) }));
  caseAgent.caseAgentRuntime.grantInvoke(dispatchFunction);
  dispatchFunction.addToRolePolicy(
    new PolicyStatement({ actions: ["bedrock:ApplyGuardrail"], resources: [guardrail.chatGuardrail.attrGuardrailArn] }),
  );
  // Answers to emailed questions and the recovery pack go to her real mailbox only.
  dispatchFunction.addToRolePolicy(
    buildSendEmailStatement(stack, settings.sendingDomain, { "ForAllValues:StringEquals": { "ses:Recipients": [settings.receiverMailbox] } }),
  );
  const dispatchDeadLetterQueue = buildDispatchDeadLetterQueue(stack);
  // Four filters: a REQUEST# META whose new status is final, a CASE# META insert, a CASE# META closed, or a new
  // question row (CHAT# MSG# with role grandma; replies, guardrailHit updates and RATE# counters never match).
  dispatchFunction.addEventSource(new DynamoEventSource(shared.table, {
    startingPosition: StartingPosition.LATEST,
    batchSize: 5,
    retryAttempts: 2,
    bisectBatchOnError: true,
    onFailure: new SqsDlq(dispatchDeadLetterQueue),
    filters: [
      FilterCriteria.filter({
        eventName: FilterRule.isEqual("MODIFY"),
        dynamodb: {
          Keys: { PK: { S: FilterRule.beginsWith("REQUEST#") }, SK: { S: FilterRule.isEqual("META") } },
          NewImage: { status: { S: FilterRule.or("denied", "confirmed", "reversed_no", "timeout") } },
        },
      }),
      FilterCriteria.filter({
        eventName: FilterRule.isEqual("INSERT"),
        dynamodb: { Keys: { PK: { S: FilterRule.beginsWith("CASE#") }, SK: { S: FilterRule.isEqual("META") } } },
      }),
      FilterCriteria.filter({
        eventName: FilterRule.isEqual("MODIFY"),
        dynamodb: {
          Keys: { PK: { S: FilterRule.beginsWith("CASE#") }, SK: { S: FilterRule.isEqual("META") } },
          NewImage: { status: { S: FilterRule.isEqual("closed") } },
        },
      }),
      FilterCriteria.filter({
        eventName: FilterRule.isEqual("INSERT"),
        dynamodb: {
          Keys: { PK: { S: FilterRule.beginsWith("CHAT#") }, SK: { S: FilterRule.beginsWith("MSG#") } },
          NewImage: { role: { S: FilterRule.isEqual("grandma") } },
        },
      }),
    ],
  }));
  return dispatchFunction;
}
