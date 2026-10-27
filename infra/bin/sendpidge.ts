import { existsSync, readFileSync } from "node:fs";
import { parseEnv } from "node:util";
import { App } from "aws-cdk-lib";
import { SendPidgeStack } from "../lib/sendpidge-stack.js";

// Personal mailboxes and phone numbers live in the git-ignored ../.env; only these keys become CDK context.
const PERSONAL_CONTEXT_KEYS: Record<string, string> = {
  RECEIVER_MAILBOX: "receiverMailbox",
  MOCK_BANK_MAILBOX: "mockBankMailbox",
  PIDGE_WHATSAPP_NUMBER: "whatsappDisplayNumber",
};

function readPersonalContext(envFilePath: string): Record<string, string> {
  if (!existsSync(envFilePath)) {
    throw new Error(`${envFilePath} is missing; copy .env.example and fill RECEIVER_MAILBOX and MOCK_BANK_MAILBOX`);
  }
  const envValues = parseEnv(readFileSync(envFilePath, "utf8"));
  const context: Record<string, string> = {};
  for (const [envKey, contextKey] of Object.entries(PERSONAL_CONTEXT_KEYS)) {
    context[contextKey] = (envValues[envKey] ?? "").trim();
  }
  return context;
}

const app = new App({ context: readPersonalContext("../.env") });
new SendPidgeStack(app, "SendPidge", {
  env: { account: "358148451054", region: "us-east-1" },
});
