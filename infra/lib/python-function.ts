import { RemovalPolicy, Stack } from "aws-cdk-lib";
import { PolicyStatement } from "aws-cdk-lib/aws-iam";
import { Architecture, Code, Function, FunctionProps, Runtime } from "aws-cdk-lib/aws-lambda";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";

type PythonFunctionProps = Omit<FunctionProps, "runtime" | "architecture" | "code" | "logGroup">;

// Every Lambda here: Python 3.12 on x86_64 from the shared lambda/ asset, with its own two-week log group.
// Construct ids are <name>Function and <name>Logs.
export function buildPythonFunction(stack: Stack, name: string, code: Code, props: PythonFunctionProps): Function {
  return new Function(stack, `${name}Function`, {
    runtime: Runtime.PYTHON_3_12,
    architecture: Architecture.X86_64,
    code,
    ...props,
    logGroup: new LogGroup(stack, `${name}Logs`, { retention: RetentionDays.TWO_WEEKS, removalPolicy: RemovalPolicy.DESTROY }),
  });
}

export const BEDROCK_INVOKE_ACTIONS = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"];

// The Claude models a caller may invoke: Haiku for the Reader and the case agent, Opus for the second reviewer
// (escalation); a global profile may route to any commercial region.
export function claudeModelResourceArns(stack: Stack): string[] {
  return [
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5*",
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-5-5*",
    `arn:aws:bedrock:${stack.region}:${stack.account}:inference-profile/us.anthropic.claude-haiku-4-5*`,
    `arn:aws:bedrock:${stack.region}:${stack.account}:inference-profile/global.anthropic.claude-haiku-5-5*`,
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-5*",
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5-5*",
    `arn:aws:bedrock:${stack.region}:${stack.account}:inference-profile/global.anthropic.claude-opus-4-5*`,
    `arn:aws:bedrock:${stack.region}:${stack.account}:inference-profile/global.anthropic.claude-opus-5-5*`,
  ];
}

// ses:SendEmail from the sending domain's identity, optionally pinned to fixed recipients.
export function buildSendEmailStatement(stack: Stack, sendingDomain: string, conditions?: Record<string, unknown>): PolicyStatement {
  return new PolicyStatement({
    actions: ["ses:SendEmail"],
    resources: [`arn:aws:ses:${stack.region}:${stack.account}:identity/${sendingDomain}`],
    conditions,
  });
}

export function grantWhatsAppSendIfConfigured(lambdaFunction: Function, whatsappPhoneNumberArn: string): void {
  if (whatsappPhoneNumberArn) {
    lambdaFunction.addToRolePolicy(
      new PolicyStatement({ actions: ["social-messaging:SendWhatsAppMessage"], resources: [whatsappPhoneNumberArn] }),
    );
  }
}

export function grantRcsSendIfConfigured(lambdaFunction: Function, rcsAgentArn: string): void {
  if (rcsAgentArn) {
    lambdaFunction.addToRolePolicy(new PolicyStatement({ actions: ["sms-voice:SendRcsMessage"], resources: [rcsAgentArn] }));
  }
}
