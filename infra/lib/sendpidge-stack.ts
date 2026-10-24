import { CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from "aws-cdk-lib";
import { WebSocketApi, WebSocketStage } from "aws-cdk-lib/aws-apigatewayv2";
import { WebSocketLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { Alarm, ComparisonOperator, TreatMissingData } from "aws-cdk-lib/aws-cloudwatch";
import { Certificate, CertificateValidation } from "aws-cdk-lib/aws-certificatemanager";
import { Distribution, ResponseHeadersPolicy, ViewerProtocolPolicy } from "aws-cdk-lib/aws-cloudfront";
import { S3BucketOrigin } from "aws-cdk-lib/aws-cloudfront-origins";
import { AttributeType, BillingMode, Table } from "aws-cdk-lib/aws-dynamodb";
import { Effect, PolicyStatement, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { Architecture, Code, Function, LayerVersion, Runtime } from "aws-cdk-lib/aws-lambda";
import { SqsDestination } from "aws-cdk-lib/aws-lambda-destinations";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { BlockPublicAccess, Bucket, BucketEncryption, EventType } from "aws-cdk-lib/aws-s3";
import { ScheduleGroup } from "aws-cdk-lib/aws-scheduler";
import { Topic } from "aws-cdk-lib/aws-sns";
import { LambdaSubscription } from "aws-cdk-lib/aws-sns-subscriptions";
import { CfnMailManagerIngressPoint, CfnMailManagerRuleSet, CfnMailManagerTrafficPolicy, EmailIdentity, Identity } from "aws-cdk-lib/aws-ses";
import { BucketDeployment, Source } from "aws-cdk-lib/aws-s3-deployment";
import { LambdaDestination } from "aws-cdk-lib/aws-s3-notifications";
import { Queue } from "aws-cdk-lib/aws-sqs";
import { Construct } from "constructs";

// Raw emails land under this prefix (Mail Manager in slice 4, a hand-dropped .eml before that).
const INBOX_PREFIX = "inbox/";
const WEBSOCKET_STAGE_NAME = "live";
const LAMBDA_SOURCE_DIRECTORY = "../lambda";
// Stamped by Mail Manager at ingress; a sender-written copy is dropped before it can be trusted.
const PIDGE_AUTH_HEADER_NAME = "X-Pidge-Auth";
const MAXIMUM_INBOUND_MESSAGE_BYTES = 10 * 1024 * 1024;

export class SendPidgeStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    const receiverAddress: string = this.node.getContext("receiverAddress");
    const readerModel: string = this.node.getContext("readerModel");
    const receiverUserId: string = this.node.getContext("receiverUserId");
    const receiverName: string = this.node.getContext("receiverName");
    const receiptSender: string = this.node.getContext("receiptSender");
    const sendingDomain = receiptSender.split("@")[1];
    const pwaDomain: string = this.node.getContext("pwaDomain");
    const landingDomain: string = this.node.getContext("landingDomain");
    const rcsEnabled: boolean = this.node.getContext("rcsEnabled") === true;
    const rcsAgentId: string = this.node.getContext("rcsAgentId") ?? "";
    const rcsAgentArn: string = this.node.getContext("rcsAgentArn") ?? "";
    const whatsappEnabled: boolean = this.node.getContext("whatsappEnabled") === true;
    const whatsappPhoneNumberId: string = this.node.getContext("whatsappPhoneNumberId") ?? "";
    const whatsappPhoneNumberArn: string = this.node.getContext("whatsappPhoneNumberArn") ?? "";
    const whatsappMetaApiVersion: string = this.node.getContext("whatsappMetaApiVersion") ?? "v20.0";
    const whatsappTemplateName: string = this.node.getContext("whatsappTemplateName") ?? "";
    const escalationEnabled: boolean = this.node.getContext("escalationEnabled") === true;
    const releaseSchedulerEnabled: boolean = this.node.getContext("releaseSchedulerEnabled") === true;
    const escalationModel: string = this.node.getContext("escalationModel") ?? "opus-4.5";

    // Sending identity for pidge@<domain> (receipts, later chat replies); Easy DKIM, three CNAMEs at the DNS host.
    const sendingIdentity = new EmailIdentity(this, "SendingIdentity", { identity: Identity.domain(sendingDomain) });

    // The PWA at https://<pwaDomain>: private bucket, CloudFront with origin access control, our own certificate.
    // ACM validates by DNS with no Route 53 zone, so the first deploy pauses until the validation CNAME exists at Cloudflare.
    const pwaBucket = new Bucket(this, "PwaBucket", {
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      encryption: BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });
    const pwaCertificate = new Certificate(this, "PwaCertificate", {
      domainName: pwaDomain,
      validation: CertificateValidation.fromDns(),
    });
    const pwaDistribution = new Distribution(this, "PwaDistribution", {
      defaultBehavior: {
        origin: S3BucketOrigin.withOriginAccessControl(pwaBucket),
        viewerProtocolPolicy: ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        // HSTS, nosniff, frame and referrer protection; the CSP stays a meta tag in index.html.
        responseHeadersPolicy: ResponseHeadersPolicy.SECURITY_HEADERS,
      },
      defaultRootObject: "index.html",
      domainNames: [pwaDomain],
      certificate: pwaCertificate,
    });
    // Every deploy ships the current pwa/ plus the public privacy and terms pages, then invalidates the cache.
    new BucketDeployment(this, "PwaFiles", {
      sources: [Source.asset("../pwa"), Source.asset("../edge/public")],
      destinationBucket: pwaBucket,
      distribution: pwaDistribution,
      distributionPaths: ["/*"],
    });

    // The landing page at https://<landingDomain>: same private-bucket + CloudFront pattern, static files only.
    const landingBucket = new Bucket(this, "LandingBucket", {
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      encryption: BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });
    const landingCertificate = new Certificate(this, "LandingCertificate", {
      domainName: landingDomain,
      validation: CertificateValidation.fromDns(),
    });
    const landingDistribution = new Distribution(this, "LandingDistribution", {
      defaultBehavior: {
        origin: S3BucketOrigin.withOriginAccessControl(landingBucket),
        viewerProtocolPolicy: ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        responseHeadersPolicy: ResponseHeadersPolicy.SECURITY_HEADERS,
      },
      defaultRootObject: "index.html",
      domainNames: [landingDomain],
      certificate: landingCertificate,
    });
    new BucketDeployment(this, "LandingFiles", {
      sources: [Source.asset("../landing")],
      destinationBucket: landingBucket,
      distribution: landingDistribution,
      distributionPaths: ["/*"],
    });

    const table = new Table(this, "Table", {
      tableName: "sendpidge",
      partitionKey: { name: "PK", type: AttributeType.STRING },
      sortKey: { name: "SK", type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      // Retention TTL; separate from a request's 60 s hold expiry (expiresAt).
      timeToLiveAttribute: "ttlEpochSeconds",
      removalPolicy: RemovalPolicy.DESTROY,
    });
    // "<userId>#EMAIL" / "<userId>#REQUEST" → newest first, for the PWA's sync.
    table.addGlobalSecondaryIndex({
      indexName: "UserIndex",
      partitionKey: { name: "userIndexPK", type: AttributeType.STRING },
      sortKey: { name: "userIndexSK", type: AttributeType.STRING },
    });

    const emailBucket = new Bucket(this, "EmailBucket", {
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      encryption: BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      lifecycleRules: [{ prefix: INBOX_PREFIX, expiration: Duration.days(30) }],
    });

    const mailManagerWriteRole = new Role(this, "MailManagerWriteRole", {
      assumedBy: new ServicePrincipal("ses.amazonaws.com", {
        conditions: { StringEquals: { "aws:SourceAccount": this.account } },
      }),
    });
    emailBucket.grantPut(mailManagerWriteRole, `${INBOX_PREFIX}*`);

    const inboundRuleSet = new CfnMailManagerRuleSet(this, "InboundRuleSet", {
      ruleSetName: "sendpidge-inbound",
      rules: [
        // A sender cannot pre-write the verdict header.
        {
          name: "drop-forged-auth-header",
          conditions: [{ stringExpression: {
            evaluate: { mimeHeaderAttribute: PIDGE_AUTH_HEADER_NAME },
            operator: "CONTAINS",
            values: ["=", "pass", "fail", "spf", "dkim", "dmarc", "verified"],
          } }],
          actions: [{ drop: {} }],
        },
        // Exactly one of the next four matches, so the stored email carries exactly one header.
        ...buildAuthHeaderRules(),
        {
          name: "store-raw-email",
          actions: [{ writeToS3: {
            roleArn: mailManagerWriteRole.roleArn,
            s3Bucket: emailBucket.bucketName,
            s3Prefix: INBOX_PREFIX,
            actionFailurePolicy: "DROP",
          } }],
        },
      ],
    });
    inboundRuleSet.node.addDependency(mailManagerWriteRole);

    const inboundTrafficPolicy = new CfnMailManagerTrafficPolicy(this, "InboundTrafficPolicy", {
      trafficPolicyName: "sendpidge-receiver-only",
      defaultAction: "DENY",
      maxMessageSizeBytes: MAXIMUM_INBOUND_MESSAGE_BYTES,
      policyStatements: [{
        action: "ALLOW",
        conditions: [{ stringExpression: {
          evaluate: { attribute: "RECIPIENT" },
          operator: "EQUALS",
          values: [receiverAddress],
        } }],
      }],
    });

    const ingressPoint = new CfnMailManagerIngressPoint(this, "IngressPoint", {
      ingressPointName: "sendpidge-inbound",
      type: "OPEN",
      ruleSetId: inboundRuleSet.attrRuleSetId,
      trafficPolicyId: inboundTrafficPolicy.attrTrafficPolicyId,
    });

    const dependencyLayer = new LayerVersion(this, "DependencyLayer", {
      code: Code.fromAsset("build/layer"),
      compatibleRuntimes: [Runtime.PYTHON_3_12],
      compatibleArchitectures: [Architecture.X86_64],
      description: "strands-agents 1.62.0 + pydantic 2.14.1 (infra/scripts/build_layer.sh)",
    });

    const lambdaCode = Code.fromAsset(LAMBDA_SOURCE_DIRECTORY, {
      exclude: ["tests", "**/__pycache__", "requirements.txt"],
    });

    const webSocketConnectFunction = new Function(this, "WebSocketConnectFunction", {
      runtime: Runtime.PYTHON_3_12,
      architecture: Architecture.X86_64,
      code: lambdaCode,
      handler: "ws_connect.handler.handle",
      timeout: Duration.seconds(10),
      environment: { TABLE_NAME: table.tableName },
      logGroup: new LogGroup(this, "WebSocketConnectLogs", {
        retention: RetentionDays.TWO_WEEKS,
        removalPolicy: RemovalPolicy.DESTROY,
      }),
    });
    table.grantReadWriteData(webSocketConnectFunction);

    const connectIntegration = new WebSocketLambdaIntegration("ConnectIntegration", webSocketConnectFunction);
    const webSocketApi = new WebSocketApi(this, "WebSocketApi", {
      apiName: "sendpidge",
      connectRouteOptions: { integration: connectIntegration },
      disconnectRouteOptions: { integration: connectIntegration },
    });
    const webSocketStage = new WebSocketStage(this, "WebSocketStage", {
      webSocketApi,
      stageName: WEBSOCKET_STAGE_NAME,
      autoDeploy: true,
    });

    const readerDeadLetterQueue = new Queue(this, "ReaderDeadLetterQueue", {
      retentionPeriod: Duration.days(14),
      enforceSSL: true,
    });

    const readerFunction = new Function(this, "ReaderFunction", {
      runtime: Runtime.PYTHON_3_12,
      architecture: Architecture.X86_64,
      code: lambdaCode,
      handler: "pidge_reader.handler.handle",
      layers: [dependencyLayer],
      memorySize: 1024,
      timeout: Duration.seconds(60),
      reservedConcurrentExecutions: 5,
      retryAttempts: 2,
      onFailure: new SqsDestination(readerDeadLetterQueue),
      environment: {
        TABLE_NAME: table.tableName,
        READER_MODEL: readerModel,
        ESCALATION_ENABLED: escalationEnabled ? "true" : "false",
        ESCALATION_MODEL: escalationModel,
        RECEIVER_ADDRESS: receiverAddress,
        RECEIVER_USER_ID: receiverUserId,
        RECEIVER_NAME: receiverName,
        WEBSOCKET_ENDPOINT: webSocketStage.callbackUrl,
        RCS_ENABLED: rcsEnabled ? "true" : "false",
        RCS_AGENT_ID: rcsAgentId,
        WHATSAPP_ENABLED: whatsappEnabled ? "true" : "false",
        WHATSAPP_PHONE_NUMBER_ID: whatsappPhoneNumberId,
        WHATSAPP_META_API_VERSION: whatsappMetaApiVersion,
        WHATSAPP_TEMPLATE_NAME: whatsappTemplateName,
        PIDGE_IMAGE_URL: `https://${pwaDomain}/assets/pidge-icon.png`,
      },
      logGroup: new LogGroup(this, "ReaderLogs", {
        retention: RetentionDays.TWO_WEEKS,
        removalPolicy: RemovalPolicy.DESTROY,
      }),
    });
    emailBucket.grantRead(readerFunction);
    table.grantReadWriteData(readerFunction);
    webSocketStage.grantManagementApiAccess(readerFunction);
    readerFunction.addToRolePolicy(
      new PolicyStatement({
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources: [
          "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5*",
          "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-5-5*",
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/us.anthropic.claude-haiku-4-5*`,
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.anthropic.claude-haiku-5-5*`,
          // The second reviewer (escalation) runs on Opus; a global profile may route to any commercial region.
          "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-5*",
          "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5-5*",
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.anthropic.claude-opus-4-5*`,
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.anthropic.claude-opus-5-5*`,
        ],
      }),
    );
    emailBucket.addEventNotification(EventType.OBJECT_CREATED, new LambdaDestination(readerFunction), {
      prefix: INBOX_PREFIX,
    });
    if (rcsAgentArn) {
      readerFunction.addToRolePolicy(
        new PolicyStatement({ actions: ["sms-voice:SendRcsMessage"], resources: [rcsAgentArn] }),
      );
    }
    if (whatsappPhoneNumberArn) {
      readerFunction.addToRolePolicy(
        new PolicyStatement({ actions: ["social-messaging:SendWhatsAppMessage"], resources: [whatsappPhoneNumberArn] }),
      );
    }

    // WhatsApp: the WABA's one event destination (button taps and delivery statuses) is this topic;
    // End User Messaging Social publishes directly, so the topic policy, not a role, grants it.
    const whatsappInboundTopic = new Topic(this, "WhatsAppInboundTopic");
    whatsappInboundTopic.addToResourcePolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal("social-messaging.amazonaws.com")],
        actions: ["sns:Publish"],
        resources: [whatsappInboundTopic.topicArn],
        conditions: { StringEquals: { "aws:SourceAccount": this.account } },
      }),
    );

    // Two-way RCS: chip taps land on this topic; End User Messaging assumes the role to publish.
    const rcsInboundTopic = new Topic(this, "RcsInboundTopic");
    const rcsTwoWayRole = new Role(this, "RcsTwoWayRole", {
      assumedBy: new ServicePrincipal("sms-voice.amazonaws.com", {
        conditions: { StringEquals: { "aws:SourceAccount": this.account } },
      }),
    });
    rcsTwoWayRole.addToPolicy(
      new PolicyStatement({
        actions: ["sns:Publish"],
        resources: [rcsInboundTopic.topicArn],
        conditions: { StringEquals: { "aws:ResourceAccount": this.account } },
      }),
    );

    // One-time schedules (the release timer now; the re-ask and the bank follow-up later) live in this group.
    // Lambdas create them at runtime; the scheduler assumes this role to invoke the target.
    const scheduleGroup = new ScheduleGroup(this, "ScheduleGroup", { removalPolicy: RemovalPolicy.DESTROY });
    const schedulerRole = new Role(this, "SchedulerRole", {
      assumedBy: new ServicePrincipal("scheduler.amazonaws.com", {
        conditions: { StringEquals: { "aws:SourceAccount": this.account } },
      }),
    });

    // At releaseAt a Yes nobody undid becomes confirmed; with the scheduler off, the next read releases it instead.
    const releaseFunction = new Function(this, "ReleaseFunction", {
      runtime: Runtime.PYTHON_3_12,
      architecture: Architecture.X86_64,
      code: lambdaCode,
      handler: "pidge_release.handler.handle",
      memorySize: 512,
      timeout: Duration.seconds(15),
      environment: {
        TABLE_NAME: table.tableName,
        RECEIVER_USER_ID: receiverUserId,
        RECEIVER_NAME: receiverName,
        RECEIPT_SENDER: receiptSender,
        WEBSOCKET_ENDPOINT: webSocketStage.callbackUrl,
      },
      logGroup: new LogGroup(this, "ReleaseLogs", {
        retention: RetentionDays.TWO_WEEKS,
        removalPolicy: RemovalPolicy.DESTROY,
      }),
    });
    table.grantReadWriteData(releaseFunction);
    webSocketStage.grantManagementApiAccess(releaseFunction);
    releaseFunction.addToRolePolicy(
      new PolicyStatement({
        actions: ["ses:SendEmail"],
        resources: [`arn:aws:ses:${this.region}:${this.account}:identity/${sendingDomain}`],
      }),
    );
    releaseFunction.grantInvoke(schedulerRole);

    const verdictFunction = new Function(this, "VerdictFunction", {
      runtime: Runtime.PYTHON_3_12,
      architecture: Architecture.X86_64,
      code: lambdaCode,
      handler: "pidge_verdict.handler.handle",
      memorySize: 512,
      timeout: Duration.seconds(15),
      environment: {
        TABLE_NAME: table.tableName,
        RECEIVER_ADDRESS: receiverAddress,
        RECEIVER_USER_ID: receiverUserId,
        RECEIVER_NAME: receiverName,
        EMAIL_BUCKET: emailBucket.bucketName,
        RECEIPT_SENDER: receiptSender,
        WEBSOCKET_ENDPOINT: webSocketStage.callbackUrl,
        RCS_TOPIC_ARN: rcsInboundTopic.topicArn,
        WHATSAPP_TOPIC_ARN: whatsappInboundTopic.topicArn,
        RELEASE_SCHEDULER_ENABLED: releaseSchedulerEnabled ? "true" : "false",
        RELEASE_FUNCTION_ARN: releaseFunction.functionArn,
        SCHEDULE_GROUP_NAME: scheduleGroup.scheduleGroupName,
        SCHEDULER_ROLE_ARN: schedulerRole.roleArn,
      },
      logGroup: new LogGroup(this, "VerdictLogs", {
        retention: RetentionDays.TWO_WEEKS,
        removalPolicy: RemovalPolicy.DESTROY,
      }),
    });
    table.grantReadWriteData(verdictFunction);
    webSocketStage.grantManagementApiAccess(verdictFunction);
    verdictFunction.addToRolePolicy(
      new PolicyStatement({
        actions: ["s3:PutObjectTagging"],
        resources: [emailBucket.arnForObjects(`${INBOX_PREFIX}*`)],
      }),
    );
    verdictFunction.addToRolePolicy(
      new PolicyStatement({
        actions: ["ses:SendEmail"],
        resources: [`arn:aws:ses:${this.region}:${this.account}:identity/${sendingDomain}`],
      }),
    );
    scheduleGroup.grantWriteSchedules(verdictFunction);
    schedulerRole.grantPassRole(verdictFunction.grantPrincipal);
    webSocketApi.addRoute("$default", {
      integration: new WebSocketLambdaIntegration("DefaultIntegration", verdictFunction),
    });
    rcsInboundTopic.addSubscription(new LambdaSubscription(verdictFunction));
    whatsappInboundTopic.addSubscription(new LambdaSubscription(verdictFunction));

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
    new CfnOutput(this, "RcsInboundTopicArn", { value: rcsInboundTopic.topicArn });
    new CfnOutput(this, "RcsTwoWayRoleArn", { value: rcsTwoWayRole.roleArn });
    new CfnOutput(this, "WhatsAppInboundTopicArn", {
      value: whatsappInboundTopic.topicArn,
      description: "Set as the WABA event destination: put-whatsapp-business-account-event-destinations",
    });
    new CfnOutput(this, "LandingDistributionDomain", {
      value: landingDistribution.distributionDomainName,
      description: `Create a CNAME for ${landingDomain} (the apex; Cloudflare flattens it) pointing at this host, DNS only`,
    });
    new CfnOutput(this, "PwaUrl", { value: `https://${pwaDomain}/` });
    new CfnOutput(this, "PwaDistributionDomain", {
      value: pwaDistribution.distributionDomainName,
      description: `Create a CNAME for ${pwaDomain} pointing at this host (DNS only; CloudFront serves the certificate)`,
    });
    new CfnOutput(this, "SendingDomainDkimRecords", {
      value: [
        `${sendingIdentity.dkimDnsTokenName1} CNAME ${sendingIdentity.dkimDnsTokenValue1}`,
        `${sendingIdentity.dkimDnsTokenName2} CNAME ${sendingIdentity.dkimDnsTokenValue2}`,
        `${sendingIdentity.dkimDnsTokenName3} CNAME ${sendingIdentity.dkimDnsTokenValue3}`,
      ].join(" | "),
      description: `Create these three CNAME records (DNS-only) so SES can sign mail from ${sendingDomain}`,
    });
  }
}

/** One rule per SPF × DKIM outcome; the four are mutually exclusive, so exactly one header is added. */
function buildAuthHeaderRules(): CfnMailManagerRuleSet.RuleProperty[] {
  const outcomes: Array<{ spfPassed: boolean; dkimPassed: boolean }> = [
    { spfPassed: true, dkimPassed: true },
    { spfPassed: true, dkimPassed: false },
    { spfPassed: false, dkimPassed: true },
    { spfPassed: false, dkimPassed: false },
  ];
  return outcomes.map(({ spfPassed, dkimPassed }) => ({
    name: `stamp-auth-spf-${spfPassed ? "pass" : "fail"}-dkim-${dkimPassed ? "pass" : "fail"}`,
    conditions: [
      { verdictExpression: { evaluate: { attribute: "SPF" }, operator: spfPassed ? "EQUALS" : "NOT_EQUALS", values: ["PASS"] } },
      { verdictExpression: { evaluate: { attribute: "DKIM" }, operator: dkimPassed ? "EQUALS" : "NOT_EQUALS", values: ["PASS"] } },
    ],
    actions: [{ addHeader: {
      headerName: PIDGE_AUTH_HEADER_NAME,
      headerValue: `spf=${spfPassed ? "pass" : "fail"};dkim=${dkimPassed ? "pass" : "fail"}`,
    } }],
  }));
}
