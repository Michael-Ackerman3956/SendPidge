import { Duration, Stack } from "aws-cdk-lib";
import { WebSocketLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { Alarm, ComparisonOperator, TreatMissingData } from "aws-cdk-lib/aws-cloudwatch";
import { PolicyStatement, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { FilterCriteria, FilterRule, Function, StartingPosition } from "aws-cdk-lib/aws-lambda";
import { DynamoEventSource, SqsDlq } from "aws-cdk-lib/aws-lambda-event-sources";
import { ContextAttribute, Schedule, ScheduleExpression, ScheduleTargetInput } from "aws-cdk-lib/aws-scheduler";
import { Universal } from "aws-cdk-lib/aws-scheduler-targets";
import { Queue, QueueEncryption } from "aws-cdk-lib/aws-sqs";
import { buildPythonFunction } from "./python-function.js";
import { SharedResources } from "./realtime.js";
import { StackSettings } from "./stack-settings.js";

// lambda/shared/enums.py ClientAction.ORG_RESOLVE: the socket routes on $request.body.action, so this key reaches OrgActions only.
const ORG_RESOLVE_ROUTE_KEY = "org_resolve";
const TABLE_READ_ACTIONS = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:BatchGetItem"];
// The projection is the only writer of ORGLOG#, ORGSTAT# and the MEMBER# coverage fact; it never writes another partition.
const PROJECTION_WRITE_ACTIONS = [
  "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:ConditionCheckItem",
];
const PROJECTION_LEADING_KEYS = ["ORG#*", "ORGLOG#*", "ORGSTAT#*"];
// OrgActions only puts the RESOLVE# row and its ORGAUDIT# row, in one transaction.
const ORG_ACTIONS_WRITE_ACTIONS = ["dynamodb:PutItem", "dynamodb:ConditionCheckItem"];
const ORG_ACTIONS_LEADING_KEYS = ["ORG#*", "ORGAUDIT#*"];
const CANARY_SORT_KEY = "CANARY";
const CANARY_INTERVAL = Duration.minutes(5);
const INSERT_OR_MODIFY = FilterRule.or("INSERT", "MODIFY");

// A table write limited to partitions whose key matches one of the patterns. ForAllValues is true for an absent key, so the
// Null check makes a request without dynamodb:LeadingKeys fail instead of pass.
function buildLeadingKeysWriteStatement(tableArn: string, actions: string[], leadingKeyPatterns: string[]): PolicyStatement {
  return new PolicyStatement({
    actions,
    resources: [tableArn],
    conditions: {
      "ForAllValues:StringLike": { "dynamodb:LeadingKeys": leadingKeyPatterns },
      Null: { "dynamodb:LeadingKeys": "false" },
    },
  });
}

// Item reads on the table (never Scan, never the index) plus the scoped write; no grantReadWriteData.
function grantOrgTableAccess(orgFunction: Function, shared: SharedResources, writeActions: string[], leadingKeyPatterns: string[]): void {
  orgFunction.addToRolePolicy(new PolicyStatement({ actions: TABLE_READ_ACTIONS, resources: [shared.table.tableArn] }));
  orgFunction.addToRolePolicy(buildLeadingKeysWriteStatement(shared.table.tableArn, writeActions, leadingKeyPatterns));
}

// A stream record the projection still fails after its retries and bisects lands here, and one message raises the alarm.
function buildOrgProjectionDeadLetterQueue(stack: Stack): Queue {
  const orgProjectionDeadLetterQueue = new Queue(stack, "OrgProjectionDeadLetterQueue", {
    encryption: QueueEncryption.SQS_MANAGED,
    retentionPeriod: Duration.days(14),
    enforceSSL: true,
  });
  new Alarm(stack, "OrgProjectionDeadLetterAlarm", {
    alarmDescription: "pidge-org-projection gave up on a stream record after retries",
    metric: orgProjectionDeadLetterQueue.metricApproximateNumberOfMessagesVisible({ period: Duration.minutes(5) }),
    threshold: 1,
    evaluationPeriods: 1,
    comparisonOperator: ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    treatMissingData: TreatMissingData.NOT_BREACHING,
  });
  return orgProjectionDeadLetterQueue;
}

// One stream filter on the event name and the record's keys; each rule list matches when any of its values or prefixes does.
function buildStreamKeyFilter(eventNames: unknown[], partitionKeyRules: unknown[], sortKeyRules: unknown[]): Record<string, unknown> {
  return FilterCriteria.filter({ eventName: eventNames, dynamodb: { Keys: { PK: { S: partitionKeyRules }, SK: { S: sortKeyRules } } } });
}

// Five filters (the default quota) over the nine key pairs the projection maps: REQUEST# META and NOTE#; EMAIL# and CASE# META;
// ORG# RESOLVE# and CANARY; RECEIVER# ORGPREF# and CONTACT#; a new USER# CONN#. Every other record never wakes it.
function buildOrgProjectionFilters(): Array<Record<string, unknown>> {
  return [
    buildStreamKeyFilter(INSERT_OR_MODIFY, FilterRule.beginsWith("REQUEST#"), [...FilterRule.isEqual("META"), ...FilterRule.beginsWith("NOTE#")]),
    buildStreamKeyFilter(INSERT_OR_MODIFY, [...FilterRule.beginsWith("EMAIL#"), ...FilterRule.beginsWith("CASE#")], FilterRule.isEqual("META")),
    buildStreamKeyFilter(INSERT_OR_MODIFY, FilterRule.beginsWith("ORG#"), [...FilterRule.beginsWith("RESOLVE#"), ...FilterRule.isEqual(CANARY_SORT_KEY)]),
    buildStreamKeyFilter(INSERT_OR_MODIFY, FilterRule.beginsWith("RECEIVER#"), [...FilterRule.beginsWith("ORGPREF#"), ...FilterRule.beginsWith("CONTACT#")]),
    buildStreamKeyFilter(FilterRule.isEqual("INSERT"), FilterRule.beginsWith("USER#"), FilterRule.beginsWith("CONN#")),
  ];
}

// pidge-org-projection: the one stream consumer that turns resident rows into ORGLOG# events, ORGSTAT# counts and coverage
// facts, then tells the home's admins {type: org_changed}. A failed record is a batch item failure: retried, bisected, then DLQ.
function buildOrgProjectionFunction(stack: Stack, shared: SharedResources): Function {
  const orgProjectionFunction = buildPythonFunction(stack, "OrgProjection", shared.lambdaCode, {
    handler: "pidge_org_projection.handler.handle",
    memorySize: 512,
    timeout: Duration.seconds(60),
    environment: {
      TABLE_NAME: shared.table.tableName,
      WEBSOCKET_ENDPOINT: shared.webSocketStage.callbackUrl,
    },
  });
  grantOrgTableAccess(orgProjectionFunction, shared, PROJECTION_WRITE_ACTIONS, PROJECTION_LEADING_KEYS);
  shared.webSocketStage.grantManagementApiAccess(orgProjectionFunction);
  orgProjectionFunction.addEventSource(new DynamoEventSource(shared.table, {
    startingPosition: StartingPosition.LATEST,
    batchSize: 10,
    retryAttempts: 2,
    bisectBatchOnError: true,
    reportBatchItemFailures: true,
    onFailure: new SqsDlq(buildOrgProjectionDeadLetterQueue(stack)),
    filters: buildOrgProjectionFilters(),
  }));
  return orgProjectionFunction;
}

// pidge-org: the admin's write actions (today only org_resolve) on their own socket route, apart from pidge-verdict's $default.
function buildOrgActionsFunction(stack: Stack, shared: SharedResources): Function {
  const orgActionsFunction = buildPythonFunction(stack, "OrgActions", shared.lambdaCode, {
    handler: "pidge_org.handler.handle",
    memorySize: 512,
    timeout: Duration.seconds(15),
    environment: {
      TABLE_NAME: shared.table.tableName,
      WEBSOCKET_ENDPOINT: shared.webSocketStage.callbackUrl,
    },
  });
  grantOrgTableAccess(orgActionsFunction, shared, ORG_ACTIONS_WRITE_ACTIONS, ORG_ACTIONS_LEADING_KEYS);
  shared.webSocketStage.grantManagementApiAccess(orgActionsFunction);
  shared.webSocketApi.addRoute(ORG_RESOLVE_ROUTE_KEY, {
    integration: new WebSocketLambdaIntegration("OrgActionsIntegration", orgActionsFunction),
  });
  return orgActionsFunction;
}

// Every 5 minutes EventBridge Scheduler puts ORG#<orgId>/CANARY itself (no Lambda) with its own role, limited to that one key.
// The record's stream time becomes lastProjectedAt, so a dead projection or an empty filter turns the dashboard amber.
function buildOrgCanary(stack: Stack, settings: StackSettings, shared: SharedResources): void {
  const canaryPartitionKey = `ORG#${settings.orgId}`;
  const orgCanaryRole = new Role(stack, "OrgCanaryRole", {
    assumedBy: new ServicePrincipal("scheduler.amazonaws.com", {
      conditions: { StringEquals: { "aws:SourceAccount": stack.account } },
    }),
  });
  new Schedule(stack, "OrgCanarySchedule", {
    schedule: ScheduleExpression.rate(CANARY_INTERVAL),
    description: "SendPidge: org projection canary row",
    target: new Universal({
      service: "dynamodb",
      action: "putItem",
      role: orgCanaryRole,
      policyStatements: [buildLeadingKeysWriteStatement(shared.table.tableArn, ["dynamodb:PutItem"], [canaryPartitionKey])],
      input: ScheduleTargetInput.fromObject({
        TableName: shared.table.tableName,
        Item: { PK: { S: canaryPartitionKey }, SK: { S: CANARY_SORT_KEY }, writtenAt: { S: ContextAttribute.scheduledTime } },
      }),
    }),
  });
}

// The org dashboard (S3), built only when orgEnabled: every construct id here is new and none replaces an existing one,
// so orgEnabled false leaves the template unchanged.
export function buildOrgFunctions(stack: Stack, settings: StackSettings, shared: SharedResources): void {
  if (!settings.orgEnabled) {
    return;
  }
  buildOrgProjectionFunction(stack, shared);
  buildOrgActionsFunction(stack, shared);
  buildOrgCanary(stack, settings, shared);
}
