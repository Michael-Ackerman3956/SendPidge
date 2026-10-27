import { Duration, Stack } from "aws-cdk-lib";
import { PolicyStatement, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { Function } from "aws-cdk-lib/aws-lambda";
import { BlockPublicAccess, Bucket, BucketEncryption } from "aws-cdk-lib/aws-s3";
import {
  CfnMailManagerAddressList, CfnMailManagerIngressPoint, CfnMailManagerRuleSet, CfnMailManagerTrafficPolicy,
} from "aws-cdk-lib/aws-ses";
import { StackSettings } from "./stack-settings.js";

// Mail Manager stores each email to an enrolled resident here as inbox/<messageId>, then invokes pidge-reader with that id.
export const INBOX_PREFIX = "inbox/";
// Replies from the SIMULATED bank to a case report land here (Mail Manager rule store-case-reply).
export const CASES_PREFIX = "cases/";
// Grandma's questions mailed to pidge@ land here (Mail Manager rule store-pidge-question); never the Reader.
export const PIDGE_QUESTION_PREFIX = "pidge/";
// A contact's reply to the ask by email lands here (Mail Manager rule store-ask-reply) and wakes pidge-verdict; never the Reader.
export const ASKS_PREFIX = "asks/";
// The subject of every ask by email carries "[req-<id>]" (lambda/shared/email_ask.py); a reply keeps it.
const ASK_SUBJECT_MARKER = "[req-";
// Stamped by Mail Manager at ingress; a sender-written copy is dropped before it can be trusted.
const PIDGE_AUTH_HEADER_NAME = "X-Pidge-Auth";
const MAXIMUM_INBOUND_MESSAGE_BYTES = 10 * 1024 * 1024;
// A failed invoke of pidge-reader is retried by Mail Manager for this long; the stored email stays either way.
const READER_INVOKE_RETRY_MINUTES = 60;

export interface EmailStore {
  emailBucket: Bucket;
  mailManagerWriteRole: Role;
  residentAddressList: CfnMailManagerAddressList;
}

// The email bucket, the role Mail Manager writes it with, and the enrolled residents' addresses
// (scripts/enrol_resident.py registers each; the store-raw-email rule and the traffic policy read the list).
export function buildEmailStore(stack: Stack): EmailStore {
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
  emailBucket.grantPut(mailManagerWriteRole, `${ASKS_PREFIX}*`);

  const residentAddressList = new CfnMailManagerAddressList(stack, "ResidentAddressList", { addressListName: "sendpidge-residents" });
  return { emailBucket, mailManagerWriteRole, residentAddressList };
}

// The Mail Manager ingress: one rule set (mail to an enrolled resident is stored, then pidge-reader is invoked with it) and
// one traffic policy that accepts only the residents, cases@ and pidge@. Built after pidge-reader, whose ARN the rule names.
export function buildInboundRouting(
  stack: Stack, settings: StackSettings, store: EmailStore, readerFunction: Function,
): CfnMailManagerIngressPoint {
  const { emailBucket, mailManagerWriteRole, residentAddressList } = store;
  const writeToS3Action = (s3Prefix: string): CfnMailManagerRuleSet.RuleActionProperty => ({ writeToS3: {
    roleArn: mailManagerWriteRole.roleArn,
    s3Bucket: emailBucket.bucketName,
    s3Prefix,
    actionFailurePolicy: "DROP",
  } });
  // Mail Manager assumes this role to invoke pidge-reader; no Lambda resource policy and no grantInvoke (it adds a :* grant).
  const readerInvokeRole = new Role(stack, "MailManagerReaderInvokeRole", {
    assumedBy: new ServicePrincipal("ses.amazonaws.com", {
      conditions: {
        StringEquals: { "aws:SourceAccount": stack.account },
        ArnLike: { "aws:SourceArn": `arn:aws:ses:${stack.region}:${stack.account}:mailmanager-rule-set/*` },
      },
    }),
  });
  readerInvokeRole.addToPolicy(new PolicyStatement({ actions: ["lambda:InvokeFunction"], resources: [readerFunction.functionArn] }));
  // The envelope recipient is an enrolled resident (the rule and the traffic policy share this condition).
  const recipientIsResident = {
    evaluate: { isInAddressList: { attribute: "RECIPIENT", addressLists: [residentAddressList.attrAddressListArn] } },
    operator: "IS_TRUE",
  };

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
      // A reply to the ask by email: to pidge@ with the request marker in the subject. pidge-verdict proves the sender
      // (DKIM pass + aligned signature, the contact's own address) before anything is recorded.
      {
        name: "store-ask-reply",
        conditions: [
          { stringExpression: { evaluate: { attribute: "RECIPIENT" }, operator: "EQUALS", values: [settings.receiptSender] } },
          { stringExpression: { evaluate: { attribute: "SUBJECT" }, operator: "CONTAINS", values: [ASK_SUBJECT_MARKER] } },
        ],
        actions: [writeToS3Action(ASKS_PREFIX)],
      },
      // A question to pidge@ that names her mailbox in From (the header may carry a display name, hence CONTAINS);
      // pidge-question-inbound still checks the exact address and DKIM alignment before anything is stored.
      // Only with a mailbox set: CONTAINS "" would match every sender.
      ...(settings.receiverMailbox ? [{
        name: "store-pidge-question",
        conditions: [
          { stringExpression: { evaluate: { attribute: "RECIPIENT" }, operator: "EQUALS", values: [settings.receiptSender] } },
          { stringExpression: { evaluate: { attribute: "FROM" }, operator: "CONTAINS", values: [settings.receiverMailbox] } },
        ],
        actions: [writeToS3Action(PIDGE_QUESTION_PREFIX)],
      }] : []),
      // Mail to an enrolled resident: stored under inbox/, then pidge-reader is invoked with its messageId and recipient.
      {
        name: "store-raw-email",
        conditions: [{ booleanExpression: recipientIsResident }],
        actions: [
          writeToS3Action(INBOX_PREFIX),
          { invokeLambda: {
            functionArn: readerFunction.functionArn,
            invocationType: "EVENT",
            roleArn: readerInvokeRole.roleArn,
            actionFailurePolicy: "CONTINUE",
            retryTimeMinutes: READER_INVOKE_RETRY_MINUTES,
          } },
        ],
      },
    ],
  });
  inboundRuleSet.node.addDependency(mailManagerWriteRole);
  inboundRuleSet.node.addDependency(readerInvokeRole);

  // The name is kept from the single-receiver policy: renaming would replace the policy, and with it the ingress point.
  const inboundTrafficPolicy = new CfnMailManagerTrafficPolicy(stack, "InboundTrafficPolicy", {
    trafficPolicyName: "sendpidge-receiver-only",
    defaultAction: "DENY",
    maxMessageSizeBytes: MAXIMUM_INBOUND_MESSAGE_BYTES,
    policyStatements: [
      { action: "ALLOW", conditions: [{ booleanExpression: recipientIsResident }] },
      {
        action: "ALLOW",
        conditions: [{ stringExpression: {
          evaluate: { attribute: "RECIPIENT" },
          operator: "EQUALS",
          values: [settings.casesAddress, settings.receiptSender],
        } }],
      },
    ],
  });

  return new CfnMailManagerIngressPoint(stack, "IngressPoint", {
    ingressPointName: "sendpidge-inbound",
    type: "OPEN",
    ruleSetId: inboundRuleSet.attrRuleSetId,
    trafficPolicyId: inboundTrafficPolicy.attrTrafficPolicyId,
  });
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
