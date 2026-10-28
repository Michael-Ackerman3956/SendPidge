import { CfnOutput, Duration, Stack } from "aws-cdk-lib";
import { Alarm, ComparisonOperator, Metric, Stats, TreatMissingData } from "aws-cdk-lib/aws-cloudwatch";
import { Rule, RuleTargetInput, Schedule } from "aws-cdk-lib/aws-events";
import { LambdaFunction } from "aws-cdk-lib/aws-events-targets";
import { buildPythonFunction } from "./python-function.js";
import { SharedResources } from "./realtime.js";

// lambda/pidge_list_dispatch/handler.py ListDispatchTrigger.SYNC: sync every live-fetch source that is due.
const SCHEDULED_SYNC_TRIGGER = "sync";
const LIST_SYNC_INTERVAL = Duration.hours(1);
// lambda/shared/metrics.py METRIC_NAMESPACE and lambda/shared/list_sync.py FEED_STALE_METRIC_NAME (EMF on stdout, no dimension,
// one count per stale live-fetch source per sync run).
const LAMBDA_METRIC_NAMESPACE = "SendPidge";
const FEED_STALE_METRIC_NAME = "FeedStale";

// pidge-list-dispatch on an hourly EventBridge rule, disabled unless liveFeedsEnabled (no IAM role): it syncs the live
// scam-list feeds into LIST# rows. Reserved concurrency 1 keeps a sync_now and the schedule from racing the BUCKETS
// read-modify-write. The list keeper is off and LIST_FEEDS_MOCK is never set (infra/seed is not in the Lambda asset).
export function buildListDispatch(stack: Stack, shared: SharedResources, isScheduleEnabled: boolean): void {
  const listDispatchFunction = buildPythonFunction(stack, "ListDispatch", shared.lambdaCode, {
    handler: "pidge_list_dispatch.handler.handle",
    layers: [shared.dependencyLayer],  // the handler imports the list keeper (strands, pydantic)
    memorySize: 1024,
    timeout: Duration.minutes(5),
    reservedConcurrentExecutions: 1,
    environment: {
      TABLE_NAME: shared.table.tableName,
      LIST_KEEPER_ENABLED: "false",
    },
  });
  shared.table.grantReadWriteData(listDispatchFunction);
  const listSyncSchedule = new Rule(stack, "ListSyncSchedule", {
    description: "SendPidge: sync the live scam-list feeds that are due",
    schedule: Schedule.rate(LIST_SYNC_INTERVAL),
    enabled: isScheduleEnabled,  // liveFeedsEnabled
    targets: [new LambdaFunction(listDispatchFunction, { event: RuleTargetInput.fromObject({ trigger: SCHEDULED_SYNC_TRIGGER }) })],
  });
  new Alarm(stack, "FeedStaleAlarm", {
    alarmDescription: "a live scam-list feed missed three sync intervals; every email reads Pidge's lists as not available",
    metric: new Metric({
      namespace: LAMBDA_METRIC_NAMESPACE, metricName: FEED_STALE_METRIC_NAME, statistic: Stats.SUM, period: LIST_SYNC_INTERVAL,
    }),
    threshold: 1,
    evaluationPeriods: 1,
    comparisonOperator: ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    treatMissingData: TreatMissingData.NOT_BREACHING,
  });
  new CfnOutput(stack, "ListDispatchFunctionName", { value: listDispatchFunction.functionName });
  // aws events disable-rule --name <this> pauses the hourly sync.
  new CfnOutput(stack, "ListSyncScheduleName", { value: listSyncSchedule.ruleName });
}
