import { CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from "aws-cdk-lib";
import { WebSocketApi, WebSocketStage } from "aws-cdk-lib/aws-apigatewayv2";
import { WebSocketLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { Alarm, ComparisonOperator, TreatMissingData } from "aws-cdk-lib/aws-cloudwatch";
import { AttributeType, BillingMode, Table } from "aws-cdk-lib/aws-dynamodb";
import { PolicyStatement } from "aws-cdk-lib/aws-iam";
import { Architecture, Code, Function, LayerVersion, Runtime } from "aws-cdk-lib/aws-lambda";
import { SqsDestination } from "aws-cdk-lib/aws-lambda-destinations";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { BlockPublicAccess, Bucket, BucketEncryption, EventType } from "aws-cdk-lib/aws-s3";
import { LambdaDestination } from "aws-cdk-lib/aws-s3-notifications";
import { Queue } from "aws-cdk-lib/aws-sqs";
import { Construct } from "constructs";

// Raw emails land under this prefix (Mail Manager in slice 4, a hand-dropped .eml before that).
const INBOX_PREFIX = "inbox/";
const WEBSOCKET_STAGE_NAME = "live";
const LAMBDA_SOURCE_DIRECTORY = "../lambda";

export class SendPidgeStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    const receiverAddress: string = this.node.getContext("receiverAddress");
    const readerModel: string = this.node.getContext("readerModel");
    const receiverUserId: string = this.node.getContext("receiverUserId");

    const table = new Table(this, "Table", {
      tableName: "sendpidge",
      partitionKey: { name: "PK", type: AttributeType.STRING },
      sortKey: { name: "SK", type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      // Retention TTL; separate from a request's 60 s hold expiry (expiresAt).
      timeToLiveAttribute: "ttlEpochSeconds",
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const emailBucket = new Bucket(this, "EmailBucket", {
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      encryption: BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      lifecycleRules: [{ prefix: INBOX_PREFIX, expiration: Duration.days(30) }],
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
        RECEIVER_ADDRESS: receiverAddress,
        RECEIVER_USER_ID: receiverUserId,
        WEBSOCKET_ENDPOINT: webSocketStage.callbackUrl,
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
        ],
      }),
    );
    emailBucket.addEventNotification(EventType.OBJECT_CREATED, new LambdaDestination(readerFunction), {
      prefix: INBOX_PREFIX,
    });

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
  }
}
