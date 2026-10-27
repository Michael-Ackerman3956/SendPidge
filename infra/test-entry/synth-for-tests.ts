import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { App } from "aws-cdk-lib";
import { SendPidgeStack } from "../lib/sendpidge-stack.js";

// Synthesises the stack for lambda/tests/test_stack.py: cdk.json context plus fake mailboxes, never ../.env.
// Usage: tsx synth-for-tests.ts <outdir>, run from a directory whose build/ holds stand-ins and whose ../ mirrors the repo.
const FAKE_PERSONAL_CONTEXT: Record<string, string> = {
  receiverMailbox: "grandma.mailbox@example.com",
  mockBankMailbox: "mock.bank@example.net",
  whatsappDisplayNumber: "+15550100000",
  cognitoDomainPrefix: "sendpidge-test",  // passkey sign-in on, so the Cognito resources are synthesised
};

const outputDirectory = process.argv[2];
if (!outputDirectory) {
  throw new Error("usage: synth-for-tests.ts <outdir>");
}
const cdkJsonPath = resolve(process.argv[1], "../../cdk.json");
const cdkJsonContext: Record<string, unknown> = JSON.parse(readFileSync(cdkJsonPath, "utf8")).context;

const app = new App({ context: { ...cdkJsonContext, ...FAKE_PERSONAL_CONTEXT }, outdir: outputDirectory });
new SendPidgeStack(app, "SendPidge", {
  env: { account: "358148451054", region: "us-east-1" },
});
app.synth();
