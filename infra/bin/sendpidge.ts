import { App } from "aws-cdk-lib";
import { SendPidgeStack } from "../lib/sendpidge-stack.js";

const app = new App();
new SendPidgeStack(app, "SendPidge", {
  env: { account: "358148451054", region: "us-east-1" },
});
