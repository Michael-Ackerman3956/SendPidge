import { CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from "aws-cdk-lib";
import { AllowedMethods, CachePolicy, OriginRequestPolicy, ResponseHeadersPolicy, ViewerProtocolPolicy } from "aws-cdk-lib/aws-cloudfront";
import { FunctionUrlOrigin } from "aws-cdk-lib/aws-cloudfront-origins";
import { Alarm, ComparisonOperator, Metric, Stats, TreatMissingData } from "aws-cdk-lib/aws-cloudwatch";
import { AttributeType, BillingMode, StreamViewType, Table } from "aws-cdk-lib/aws-dynamodb";
import { Architecture, Code, LayerVersion, Runtime } from "aws-cdk-lib/aws-lambda";
import { Source } from "aws-cdk-lib/aws-s3-deployment";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";
import { EmailIdentity, Identity } from "aws-cdk-lib/aws-ses";
import { Construct } from "constructs";
import { buildCaseAgent, buildChatGuardrail, buildDispatchFunction } from "./case-agent.js";
import { buildCaseInboundFunction, buildQuestionInboundFunction, buildReaderFunction } from "./inbound-functions.js";
import { buildEmailStore, buildInboundRouting } from "./mail-ingress.js";
import { buildPasskeySignIn, buildPwaSignInConfig } from "./passkey-sign-in.js";
import { SharedResources, buildScheduler, buildWebSocketApi } from "./realtime.js";
import { readStackSettings } from "./stack-settings.js";
import { buildStaticSite } from "./static-site.js";
import {
  buildAskLinkFunction, buildCallReminderFunction, buildCaseFollowUpFunction, buildInboundTopics, buildReleaseFunction, buildRetryFunction,
  buildVerdictFunction,
} from "./verdict-functions.js";

const LAMBDA_SOURCE_DIRECTORY = "../lambda";
// The "No, that wasn't me" link in the ask by email lives under the PWA domain (lambda/shared/ask_link.py LINK_PATH_PREFIX).
const ASK_LINK_PATH_PATTERN = "/no/*";
// lambda/shared/metrics.py METRIC_NAMESPACE and lambda/shared/profiles.py UNKNOWN_RECEIVER_METRIC_NAME (EMF on stdout, no dimension).
const LAMBDA_METRIC_NAMESPACE = "SendPidge";
const UNKNOWN_RECEIVER_METRIC_NAME = "UnknownReceiver";
// lambda/shared/profiles.py PROFILE_REJECTED_METRIC_NAME, its dimension and ProfileRejectionReason.RECEIVER_MISMATCH.
const PROFILE_REJECTED_METRIC_NAME = "ProfileRejected";
const PROFILE_REJECTED_DIMENSION_NAME = "reason";
const RECEIVER_MISMATCH_REASON = "receiver_mismatch";
// lambda/shared/sanitize.py SUMMARY_FALLBACK_METRIC_NAME and AGENT_OUTPUT_REFUSED_METRIC_NAME (the alarm reads the dimensionless count).
const SUMMARY_FALLBACK_METRIC_NAME = "SummaryFallback";
const AGENT_OUTPUT_REFUSED_METRIC_NAME = "AgentOutputRefused";

// CDK stack: the declaration of every AWS resource SendPidge deploys (`cdk deploy` turns this into CloudFormation).
// The whole SendPidge deployment: table → email store → the Lambdas → the Mail Manager ingress (it names the Reader) → the case
// agent → alarms and outputs.
// Each piece is built by a plain function in its own file with this stack as the construct scope, so construct ids
// (and therefore CloudFormation logical ids) stay where they were when this was one file.
export class SendPidgeStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);
    const settings = readStackSettings(this);

    // Sending identity for pidge@<domain> (receipts, case reports, emailed chat replies); Easy DKIM, three CNAMEs at the DNS host.
    const sendingIdentity = new EmailIdentity(this, "SendingIdentity", { identity: Identity.domain(settings.sendingDomain) });

    // Passkey sign-in (slice 13) is built first: the PWA's auth-config.json names its client and managed-login domain.
    const passkeySignIn = buildPasskeySignIn(this, settings);

    // The PWA ships the current pwa/, the public privacy and terms pages and its sign-in config; the landing page is static files only.
    const pwaDistribution = buildStaticSite(this, "Pwa", settings.pwaDomain, [
      Source.asset("../pwa"),
      Source.asset("../edge/public"),
      Source.jsonData("auth-config.json", buildPwaSignInConfig(settings, passkeySignIn)),
    ]);
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

    const emailStore = buildEmailStore(this);
    const { emailBucket, residentAddressList } = emailStore;

    const dependencyLayer = new LayerVersion(this, "DependencyLayer", {
      code: Code.fromAsset("build/layer"),
      compatibleRuntimes: [Runtime.PYTHON_3_12],
      compatibleArchitectures: [Architecture.X86_64],
      description: "strands-agents 1.62.0 + pydantic 2.14.1 + pywebpush 2.5.0 + PyJWT 2.15.1 (infra/scripts/build_layer.sh)",
    });
    const lambdaCode = Code.fromAsset(LAMBDA_SOURCE_DIRECTORY, {
      exclude: ["tests", "**/__pycache__", "requirements.txt"],
    });
    const { webSocketApi, webSocketStage } = buildWebSocketApi(this, table, lambdaCode, dependencyLayer, passkeySignIn);
    const { scheduleGroup, schedulerRole } = buildScheduler(this);
    const shared: SharedResources = { table, emailBucket, lambdaCode, dependencyLayer, webSocketApi, webSocketStage, scheduleGroup, schedulerRole };

    // HMAC key for the signed No link; generated here, read by pidge-reader, pidge-retry (sign) and pidge-ask-link (verify).
    const askLinkSecret = new Secret(this, "AskLinkSecret", {
      description: "SendPidge: HMAC key for the No link in the ask by email",
      generateSecretString: { passwordLength: 64, excludePunctuation: true },
    });
    const retryFunction = buildRetryFunction(this, settings, shared, askLinkSecret);
    const caseFollowUpFunction = buildCaseFollowUpFunction(this, settings, shared);
    const vapidSecret = Secret.fromSecretNameV2(this, "VapidPrivateKey", settings.vapidSecretName);
    buildCaseInboundFunction(this, shared, vapidSecret);
    buildQuestionInboundFunction(this, settings, shared);
    const { readerFunction, readerDeadLetterQueue } = buildReaderFunction(this, settings, shared, retryFunction, askLinkSecret);
    // The rule set names the Reader's ARN, so the ingress is built after it (construct ids unchanged: no replacement, MX holds).
    const ingressPoint = buildInboundRouting(this, settings, emailStore, readerFunction);
    const topics = buildInboundTopics(this);
    const releaseFunction = buildReleaseFunction(this, settings, shared);
    // pidge_call_reminder is call-nudge wave 2 and not written yet, so the function exists only once the nudge is switched on.
    const callReminderFunction = settings.callNudgeEnabled ? buildCallReminderFunction(this, settings, shared) : undefined;
    buildVerdictFunction(this, settings, shared, { topics, releaseFunction, caseFollowUpFunction, callReminderFunction });
    const askLinkUrl = buildAskLinkFunction(this, settings, shared, askLinkSecret);
    pwaDistribution.addBehavior(ASK_LINK_PATH_PATTERN, new FunctionUrlOrigin(askLinkUrl), {
      allowedMethods: AllowedMethods.ALLOW_ALL,  // the button is a POST
      cachePolicy: CachePolicy.CACHING_DISABLED,
      originRequestPolicy: OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,  // exp and sig travel in the query string
      viewerProtocolPolicy: ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      responseHeadersPolicy: ResponseHeadersPolicy.SECURITY_HEADERS,
    });

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
    new Alarm(this, "UnknownReceiverAlarm", {
      alarmDescription: "mail or an action named an address with no complete RECEIVER# profile; it was dropped or refused",
      metric: new Metric({
        namespace: LAMBDA_METRIC_NAMESPACE, metricName: UNKNOWN_RECEIVER_METRIC_NAME, statistic: Stats.SUM, period: Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: TreatMissingData.NOT_BREACHING,
    });
    new Alarm(this, "ReceiverMismatchAlarm", {
      alarmDescription: "a resident action came from a user whose PROFILE names another resident's address; it was refused",
      metric: new Metric({
        namespace: LAMBDA_METRIC_NAMESPACE, metricName: PROFILE_REJECTED_METRIC_NAME,
        dimensionsMap: { [PROFILE_REJECTED_DIMENSION_NAME]: RECEIVER_MISMATCH_REASON }, statistic: Stats.SUM, period: Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: TreatMissingData.NOT_BREACHING,
    });
    new Alarm(this, "SummaryFallbackAlarm", {
      alarmDescription: "a sanitised summary still held a link, address or number; the fixed fallback sentence was stored",
      metric: new Metric({
        namespace: LAMBDA_METRIC_NAMESPACE, metricName: SUMMARY_FALLBACK_METRIC_NAME, statistic: Stats.SUM, period: Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: TreatMissingData.NOT_BREACHING,
    });
    new Alarm(this, "AgentOutputRefusedAlarm", {
      alarmDescription: "an agent's text held a link, address, handle, long number or a character outside the allow-list and the gate refused it",
      metric: new Metric({
        namespace: LAMBDA_METRIC_NAMESPACE, metricName: AGENT_OUTPUT_REFUSED_METRIC_NAME, statistic: Stats.SUM, period: Duration.minutes(5),
      }),
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
    new CfnOutput(this, "ResidentAddressListId", { value: residentAddressList.attrAddressListId });
    // scripts/swarm.py live mode invokes the Reader by this name after each drop (Mail Manager does it in production).
    new CfnOutput(this, "ReaderFunctionName", { value: readerFunction.functionName });
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
    if (passkeySignIn !== undefined) {
      // scripts/seed_auth_users.py reads UserPoolId to create and link the Cognito users.
      new CfnOutput(this, "UserPoolId", { value: passkeySignIn.userPool.userPoolId });
      new CfnOutput(this, "UserPoolClientId", { value: passkeySignIn.appClient.userPoolClientId });
      new CfnOutput(this, "CognitoHostedUiDomain", { value: passkeySignIn.domain.baseUrl() });
    }
  }
}
