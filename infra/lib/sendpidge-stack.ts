import { CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from "aws-cdk-lib";
import { Alarm, ComparisonOperator, TreatMissingData } from "aws-cdk-lib/aws-cloudwatch";
import { AttributeType, BillingMode, StreamViewType, Table } from "aws-cdk-lib/aws-dynamodb";
import { Architecture, Code, LayerVersion, Runtime } from "aws-cdk-lib/aws-lambda";
import { Source } from "aws-cdk-lib/aws-s3-deployment";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";
import { EmailIdentity, Identity } from "aws-cdk-lib/aws-ses";
import { Construct } from "constructs";
import { buildCaseAgent, buildChatGuardrail, buildDispatchFunction } from "./case-agent.js";
import { buildCaseInboundFunction, buildQuestionInboundFunction, buildReaderFunction } from "./inbound-functions.js";
import { buildMailIngress } from "./mail-ingress.js";
import { SharedResources, buildScheduler, buildWebSocketApi } from "./realtime.js";
import { readStackSettings } from "./stack-settings.js";
import { buildStaticSite } from "./static-site.js";
import {
  buildCallReminderFunction, buildCaseFollowUpFunction, buildInboundTopics, buildReleaseFunction, buildRetryFunction, buildVerdictFunction,
} from "./verdict-functions.js";

const LAMBDA_SOURCE_DIRECTORY = "../lambda";

// CDK stack: the declaration of every AWS resource SendPidge deploys (`cdk deploy` turns this into CloudFormation).
// The whole SendPidge deployment, in the order the mail flows: ingress → table → the Lambdas → the case agent → outputs.
// Each piece is built by a plain function in its own file with this stack as the construct scope, so construct ids
// (and therefore CloudFormation logical ids) stay where they were when this was one file.
export class SendPidgeStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);
    const settings = readStackSettings(this);

    // Sending identity for pidge@<domain> (receipts, case reports, emailed chat replies); Easy DKIM, three CNAMEs at the DNS host.
    const sendingIdentity = new EmailIdentity(this, "SendingIdentity", { identity: Identity.domain(settings.sendingDomain) });

    // The PWA ships the current pwa/ plus the public privacy and terms pages; the landing page is static files only.
    const pwaDistribution = buildStaticSite(this, "Pwa", settings.pwaDomain, [Source.asset("../pwa"), Source.asset("../edge/public")]);
    const landingDistribution = buildStaticSite(this, "Landing", settings.landingDomain, [Source.asset("../landing")]);

    const table = new Table(this, "Table", {
      tableName: "sendpidge",
      partitionKey: { name: "PK", type: AttributeType.STRING },
      sortKey: { name: "SK", type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      // Retention TTL; separate from a request's hold expiry (expiresAt, one re-ask interval).
      timeToLiveAttribute: "ttlEpochSeconds",
      // The stream wakes pidge-dispatch (the case agent) on a final verdict, a new CASE# row or a closed case.
      stream: StreamViewType.NEW_AND_OLD_IMAGES,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    // "<userId>#EMAIL" / "<userId>#REQUEST" → newest first, for the PWA's sync.
    table.addGlobalSecondaryIndex({
      indexName: "UserIndex",
      partitionKey: { name: "userIndexPK", type: AttributeType.STRING },
      sortKey: { name: "userIndexSK", type: AttributeType.STRING },
    });

    const { emailBucket, ingressPoint } = buildMailIngress(this, settings);

    const dependencyLayer = new LayerVersion(this, "DependencyLayer", {
      code: Code.fromAsset("build/layer"),
      compatibleRuntimes: [Runtime.PYTHON_3_12],
      compatibleArchitectures: [Architecture.X86_64],
      description: "strands-agents 1.62.0 + pydantic 2.14.1 + pywebpush 2.5.0 (infra/scripts/build_layer.sh)",
    });
    const lambdaCode = Code.fromAsset(LAMBDA_SOURCE_DIRECTORY, {
      exclude: ["tests", "**/__pycache__", "requirements.txt"],
    });
    const { webSocketApi, webSocketStage } = buildWebSocketApi(this, table, lambdaCode);
    const { scheduleGroup, schedulerRole } = buildScheduler(this);
    const shared: SharedResources = { table, emailBucket, lambdaCode, dependencyLayer, webSocketApi, webSocketStage, scheduleGroup, schedulerRole };

    const retryFunction = buildRetryFunction(this, settings, shared);
    const caseFollowUpFunction = buildCaseFollowUpFunction(this, settings, shared);
    const vapidSecret = Secret.fromSecretNameV2(this, "VapidPrivateKey", settings.vapidSecretName);
    buildCaseInboundFunction(this, settings, shared, vapidSecret);
    buildQuestionInboundFunction(this, settings, shared);
    const { readerDeadLetterQueue } = buildReaderFunction(this, settings, shared, retryFunction);
    const topics = buildInboundTopics(this);
    const releaseFunction = buildReleaseFunction(this, settings, shared);
    const callReminderFunction = buildCallReminderFunction(this, settings, shared);
    buildVerdictFunction(this, settings, shared, { topics, releaseFunction, caseFollowUpFunction, callReminderFunction });

    const caseAgent = buildCaseAgent(this, settings, table);
    const guardrail = buildChatGuardrail(this);
    buildDispatchFunction(this, settings, shared, caseAgent, guardrail, vapidSecret);

    new Alarm(this, "ReaderDeadLetterAlarm", {
      alarmDescription: "pidge-reader gave up on an email after retries",
      metric: readerDeadLetterQueue.metricApproximateNumberOfMessagesVisible({ period: Duration.minutes(1) }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: TreatMissingData.NOT_BREACHING,
    });

    new CfnOutput(this, "EmailBucketName", { value: emailBucket.bucketName });
    new CfnOutput(this, "TableName", { value: table.tableName });
    new CfnOutput(this, "WebSocketUrl", { value: webSocketStage.url });
    new CfnOutput(this, "MailManagerARecord", {
      value: ingressPoint.attrARecord,
      description: `Create an MX record for the receiver's domain pointing at this host`,
    });
    new CfnOutput(this, "RcsInboundTopicArn", { value: topics.rcsInboundTopic.topicArn });
    new CfnOutput(this, "CaseAgentRuntimeArn", { value: caseAgent.caseAgentRuntime.agentRuntimeArn });
    new CfnOutput(this, "CaseAgentMemoryId", { value: caseAgent.caseAgentMemory.memoryId });
    new CfnOutput(this, "RcsTwoWayRoleArn", { value: topics.rcsTwoWayRole.roleArn });
    new CfnOutput(this, "WhatsAppInboundTopicArn", {
      value: topics.whatsappInboundTopic.topicArn,
      description: "Set as the WABA event destination: put-whatsapp-business-account-event-destinations",
    });
    new CfnOutput(this, "LandingDistributionDomain", {
      value: landingDistribution.distributionDomainName,
      description: `Create a CNAME for ${settings.landingDomain} (the apex; Cloudflare flattens it) pointing at this host, DNS only`,
    });
    new CfnOutput(this, "PwaUrl", { value: `https://${settings.pwaDomain}/` });
    new CfnOutput(this, "PwaDistributionDomain", {
      value: pwaDistribution.distributionDomainName,
      description: `Create a CNAME for ${settings.pwaDomain} pointing at this host (DNS only; CloudFront serves the certificate)`,
    });
    new CfnOutput(this, "SendingDomainDkimRecords", {
      value: [
        `${sendingIdentity.dkimDnsTokenName1} CNAME ${sendingIdentity.dkimDnsTokenValue1}`,
        `${sendingIdentity.dkimDnsTokenName2} CNAME ${sendingIdentity.dkimDnsTokenValue2}`,
        `${sendingIdentity.dkimDnsTokenName3} CNAME ${sendingIdentity.dkimDnsTokenValue3}`,
      ].join(" | "),
      description: `Create these three CNAME records (DNS-only) so SES can sign mail from ${settings.sendingDomain}`,
    });
  }
}
