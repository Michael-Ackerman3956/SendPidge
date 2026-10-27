import { Duration, RemovalPolicy, Stack } from "aws-cdk-lib";
import { WebSocketApi, WebSocketStage } from "aws-cdk-lib/aws-apigatewayv2";
import { WebSocketLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { Table } from "aws-cdk-lib/aws-dynamodb";
import { Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { Code, LayerVersion } from "aws-cdk-lib/aws-lambda";
import { ScheduleGroup } from "aws-cdk-lib/aws-scheduler";
import { Bucket } from "aws-cdk-lib/aws-s3";
import { PasskeySignIn } from "./passkey-sign-in.js";
import { buildPythonFunction } from "./python-function.js";

const WEBSOCKET_STAGE_NAME = "live";
// Caps every route of the stage across all clients (a handful of phones send a few frames a minute); a client over it gets 429.
const WEBSOCKET_THROTTLE = { rateLimit: 20, burstLimit: 40 };

// What every Lambda builder needs: the table, the email bucket, the shared code asset and layer, the socket, the scheduler.
export interface SharedResources {
  table: Table;
  emailBucket: Bucket;
  lambdaCode: Code;
  dependencyLayer: LayerVersion;
  webSocketApi: WebSocketApi;
  webSocketStage: WebSocketStage;
  scheduleGroup: ScheduleGroup;
  schedulerRole: Role;
}

// The PWA's push channel: ws_connect handles $connect/$disconnect; $default is routed to pidge-verdict later.
// With passkey sign-in, $connect also verifies Cognito ID tokens against this pool's issuer and app client. ws_connect
// imports PyJWT at load (sign-in on or off), so the function always carries the dependency layer.
export function buildWebSocketApi(
  stack: Stack, table: Table, lambdaCode: Code, dependencyLayer: LayerVersion, passkeySignIn?: PasskeySignIn,
): { webSocketApi: WebSocketApi; webSocketStage: WebSocketStage } {
  const signInEnvironment: Record<string, string> = passkeySignIn === undefined ? {} : {
    COGNITO_ISSUER: `https://cognito-idp.${stack.region}.amazonaws.com/${passkeySignIn.userPool.userPoolId}`,
    COGNITO_APP_CLIENT_ID: passkeySignIn.appClient.userPoolClientId,
  };
  const webSocketConnectFunction = buildPythonFunction(stack, "WebSocketConnect", lambdaCode, {
    handler: "ws_connect.handler.handle",
    timeout: Duration.seconds(10),
    layers: [dependencyLayer],
    environment: { TABLE_NAME: table.tableName, ...signInEnvironment },
  });
  table.grantReadWriteData(webSocketConnectFunction);

  const connectIntegration = new WebSocketLambdaIntegration("ConnectIntegration", webSocketConnectFunction);
  const webSocketApi = new WebSocketApi(stack, "WebSocketApi", {
    apiName: "sendpidge",
    connectRouteOptions: { integration: connectIntegration },
    disconnectRouteOptions: { integration: connectIntegration },
  });
  const webSocketStage = new WebSocketStage(stack, "WebSocketStage", {
    webSocketApi,
    stageName: WEBSOCKET_STAGE_NAME,
    autoDeploy: true,
    throttle: WEBSOCKET_THROTTLE,
  });
  return { webSocketApi, webSocketStage };
}

// One-time schedules (the release timer, the re-ask, the bank follow-up, the call reminder) live in this group.
// Lambdas create them at runtime; the scheduler assumes this role to invoke the target.
export function buildScheduler(stack: Stack): { scheduleGroup: ScheduleGroup; schedulerRole: Role } {
  const scheduleGroup = new ScheduleGroup(stack, "ScheduleGroup", { removalPolicy: RemovalPolicy.DESTROY });
  const schedulerRole = new Role(stack, "SchedulerRole", {
    assumedBy: new ServicePrincipal("scheduler.amazonaws.com", {
      conditions: { StringEquals: { "aws:SourceAccount": stack.account } },
    }),
  });
  return { scheduleGroup, schedulerRole };
}
