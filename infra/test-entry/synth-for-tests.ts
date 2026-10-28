import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { App } from "aws-cdk-lib";
import { SendPidgeStack } from "../lib/sendpidge-stack.js";

// Synthesises the stack for lambda/tests/test_stack.py: cdk.json context plus fake mailboxes, never ../.env.
// Usage: tsx synth-for-tests.ts <outdir> [<context overrides as JSON>], run from a directory whose build/ holds stand-ins and
// whose ../ mirrors the repo. The overrides win over everything else (a test turns one setting off to see synth refuse).
const FAKE_PERSONAL_CONTEXT: Record<string, unknown> = {
  receiverMailbox: "grandma.mailbox@example.com",
  mockBankMailbox: "mock.bank@example.net",
  whatsappDisplayNumber: "+15550100000",
  cognitoDomainPrefix: "sendpidge-test",  // passkey sign-in on, so the Cognito resources are synthesised
  orgEnabled: true,  // the org projection, OrgActions route and canary are synthesised
  orgId: "test-home",
  voiceEnabled: true,  // the call grants, invoke permission and alarms are synthesised
  // the load_facts grants are synthesised
  voiceAiAssistantArn: "arn:aws:wisdom:ap-northeast-2:358148451054:assistant/11111111-2222-3333-4444-555555555555",
};

const outputDirectory = process.argv[2];
if (!outputDirectory) {
  throw new Error("usage: synth-for-tests.ts <outdir> [<context overrides as JSON>]");
}
const contextOverrides: Record<string, unknown> = process.argv[3] === undefined ? {} : JSON.parse(process.argv[3]);
const cdkJsonPath = resolve(process.argv[1], "../../cdk.json");
const cdkJsonContext: Record<string, unknown> = JSON.parse(readFileSync(cdkJsonPath, "utf8")).context;

const app = new App({ context: { ...cdkJsonContext, ...FAKE_PERSONAL_CONTEXT, ...contextOverrides }, outdir: outputDirectory });
new SendPidgeStack(app, "SendPidge", {
  env: { account: "358148451054", region: "us-east-1" },
});
app.synth();
