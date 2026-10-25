import { Duration, Stack } from "aws-cdk-lib";
import { Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { BlockPublicAccess, Bucket, BucketEncryption } from "aws-cdk-lib/aws-s3";
import { CfnMailManagerIngressPoint, CfnMailManagerRuleSet, CfnMailManagerTrafficPolicy } from "aws-cdk-lib/aws-ses";
import { StackSettings } from "./stack-settings.js";

// Raw emails land under this prefix (Mail Manager in slice 4, a hand-dropped .eml before that).
export const INBOX_PREFIX = "inbox/";
// Replies from the SIMULATED bank to a case report land here (Mail Manager rule store-case-reply).
export const CASES_PREFIX = "cases/";
// Grandma's questions mailed to pidge@ land here (Mail Manager rule store-pidge-question); never the Reader.
export const PIDGE_QUESTION_PREFIX = "pidge/";
// Stamped by Mail Manager at ingress; a sender-written copy is dropped before it can be trusted.
const PIDGE_AUTH_HEADER_NAME = "X-Pidge-Auth";
const MAXIMUM_INBOUND_MESSAGE_BYTES = 10 * 1024 * 1024;

export interface MailIngress {
  emailBucket: Bucket;
  ingressPoint: CfnMailManagerIngressPoint;
}

// The email bucket and the Mail Manager ingress that fills it: one rule set, one receiver-only traffic policy.
export function buildMailIngress(stack: Stack, settings: StackSettings): MailIngress {
  const emailBucket = new Bucket(stack, "EmailBucket", {
    blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
    encryption: BucketEncryption.S3_MANAGED,
    enforceSSL: true,
    lifecycleRules: [{ prefix: INBOX_PREFIX, expiration: Duration.days(30) }],
  });

  const mailManagerWriteRole = new Role(stack, "MailManagerWriteRole", {
    assumedBy: new ServicePrincipal("ses.amazonaws.com", {
      conditions: { StringEquals: { "aws:SourceAccount": stack.account } },
    }),
  });
  emailBucket.grantPut(mailManagerWriteRole, `${INBOX_PREFIX}*`);
  emailBucket.grantPut(mailManagerWriteRole, `${CASES_PREFIX}*`);
  emailBucket.grantPut(mailManagerWriteRole, `${PIDGE_QUESTION_PREFIX}*`);

  const writeToS3Action = (s3Prefix: string): CfnMailManagerRuleSet.RuleActionProperty => ({ writeToS3: {
    roleArn: mailManagerWriteRole.roleArn,
    s3Bucket: emailBucket.bucketName,
    s3Prefix,
    actionFailurePolicy: "DROP",
  } });
  const inboundRuleSet = new CfnMailManagerRuleSet(stack, "InboundRuleSet", {
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
      // A bank's reply to a case report lands under cases/ and never wakes the Reader.
      {
        name: "store-case-reply",
        conditions: [{ stringExpression: { evaluate: { attribute: "RECIPIENT" }, operator: "EQUALS", values: [settings.casesAddress] } }],
        actions: [writeToS3Action(CASES_PREFIX)],
      },
      // A question to pidge@ that names her mailbox in From (the header may carry a display name, hence CONTAINS);
      // pidge-question-inbound still checks the exact address and DKIM alignment before anything is stored.
      {
        name: "store-pidge-question",
        conditions: [
          { stringExpression: { evaluate: { attribute: "RECIPIENT" }, operator: "EQUALS", values: [settings.receiptSender] } },
          { stringExpression: { evaluate: { attribute: "FROM" }, operator: "CONTAINS", values: [settings.receiverMailbox] } },
        ],
        actions: [writeToS3Action(PIDGE_QUESTION_PREFIX)],
      },
      {
        name: "store-raw-email",
        conditions: [{ stringExpression: { evaluate: { attribute: "RECIPIENT" }, operator: "EQUALS", values: [settings.receiverAddress] } }],
        actions: [writeToS3Action(INBOX_PREFIX)],
      },
    ],
  });
  inboundRuleSet.node.addDependency(mailManagerWriteRole);

  const inboundTrafficPolicy = new CfnMailManagerTrafficPolicy(stack, "InboundTrafficPolicy", {
    trafficPolicyName: "sendpidge-receiver-only",
    defaultAction: "DENY",
    maxMessageSizeBytes: MAXIMUM_INBOUND_MESSAGE_BYTES,
    policyStatements: [{
      action: "ALLOW",
      conditions: [{ stringExpression: {
        evaluate: { attribute: "RECIPIENT" },
        operator: "EQUALS",
        values: [settings.receiverAddress, settings.casesAddress, settings.receiptSender],
      } }],
    }],
  });

  const ingressPoint = new CfnMailManagerIngressPoint(stack, "IngressPoint", {
    ingressPointName: "sendpidge-inbound",
    type: "OPEN",
    ruleSetId: inboundRuleSet.attrRuleSetId,
    trafficPolicyId: inboundTrafficPolicy.attrTrafficPolicyId,
  });
  return { emailBucket, ingressPoint };
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
