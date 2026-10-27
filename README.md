# SendPidge

An inbox that asks the real person. When an email claims to be from someone in Grandma's circle and asks for money, a code or an urgent action, Pidge holds it and asks that person on a channel only they control (the SendPidge app or WhatsApp). The answer, not a score, decides what Grandma sees.

Measured on the deployed pipeline on 2026-10-25 by `scripts/swarm.py` (10 emails dropped into the real bucket, not a mock):
8 of 8 attack emails held or warned, 0 of 2 benign controls wrongly held, 0 outbound summaries carrying a URL, number or account from the email.

## How it works

1. Mail arrives at Amazon SES Mail Manager and lands in S3.
2. `pidge-reader` (Lambda, Strands Agents on Amazon Bedrock, Claude Haiku with Claude Opus escalation) reads the email with no tools, returns a typed verdict, and the policy in code decides HOLD, WARN or PASS.
3. A HOLD writes a request row in DynamoDB and asks the contact at the same moment: in-app over an API Gateway WebSocket, WhatsApp through AWS End User Messaging Social (approved utility template, Yes / No buttons), and email through Amazon SES to the contact's own address (a signed one-tap "No" link served under app.sendpidge.com, or a reply of NO that comes back through Mail Manager). RCS is a stub behind a flag (see below).
4. The contact's answer lands as a verdict. No stays held and can be reported; Yes opens the email after a ten-second undo window; a No on WhatsApp is answered with "Noted. The email stays held." and an "I made a mistake" button that takes it back within five minutes. A late No still lands; a late Yes does not. A Yes never counts on the channel the suspicious message came in on (email today), because a hijacked mailbox could send it; a No counts from anywhere.
5. After a No, the case agent on Amazon Bedrock AgentCore remembers the family, writes the case note; code builds the recovery pack from a rule table (the steps plus a call script, dispute letter and police report filled from the case facts) and mails it to Grandma's own mailbox; Grandma can ask Pidge about it in the chat window, behind a Bedrock Guardrail prompt-attack check.

The PWA at `pwa/` is one static site on S3 + CloudFront. The access token in the link decides the role: Grandma (receiver) or Jimin (contact).

## Repository

| Path | What |
|---|---|
| `infra/` | AWS CDK stack (TypeScript): Mail Manager, S3, Lambdas, DynamoDB, API Gateway WebSocket, EUM Social, Bedrock, AgentCore, CloudFront |
| `lambda/` | Python Lambdas: `pidge_reader`, `pidge_verdict`, `pidge_dispatch`, `pidge_release`, `pidge_retry`, `pidge_case_inbound`, `pidge_case_followup`, `pidge_question_inbound`, `ws_connect`; `shared/` modules; `tests/` |
| `pwa/` | The progressive web app: plain ES modules, no build step |
| `scripts/` | `seed_graph.py` (the demo family), `swarm.py` (the attacker swarm), `preflight.py` (deployment checks), `read_eml.py` |
| `samples/` | The `.eml` files the swarm and the demo use |
| `devpost/` | Scope, PRD, spec and the build checklist for the hackathon |

## Run the tests

```
bash scripts/run_tests.sh
```

This runs pytest from `lambda/` (including the grep gate and `test_stack.py`, which synthesises the CDK stack with fake mailboxes and needs no AWS credentials or `.env`), then `infra/node_modules/.bin/tsc --noEmit`. It needs `.venv` and `infra/node_modules` installed and exits non-zero on the first failure.

## Deploy

```
bash infra/scripts/build_case_agent.sh
cd infra && npx cdk deploy --require-approval never
```

The demo family is written once by `scripts/seed_graph.py` (see its usage line: the contact's numbers and addresses come from environment variables, and a re-run without them drops them).

Context flags live in `infra/cdk.json` (`chatEnabled`, `asksPerContactPerHour`, `contactVerificationMock`, …). Secrets come from `.env` and `.env.tokens`, both git-ignored.

## What is real and what is a demo

- The email pipeline, the hold, the WhatsApp ask, the in-app ask and the ask by email run on AWS for real.
- Plain SMS is not built: it needs an origination number in AWS End User Messaging SMS.
- RCS is a stub: the send path, the two-way SNS topic and the reply parser are in the code behind `rcsEnabled: false`, but we had no US number to receive the sandbox agent (a Korean SIM cannot), so the card was never tested end to end on a phone. The inbound parser is covered by pytest only.
- The bank in the recovery flow is simulated: "Send to my bank" emails a mailbox we control.
- Adding someone to the circle is real: the form writes a pending contact with a one-time code, Grandma sends the note from her own phone, and the contact proves the number by replying with the code to Pidge on WhatsApp (`PIDGE_WHATSAPP_NUMBER` in `.env`). With `contactVerificationMock` on, a labelled button can play that reply instead; nothing is sent by it.
- The SMS and app-notification rows in Grandma's inbox are static mock rows; only email comes in today.

## Prior work

Built for the Devpost "Build With AI: Basics" / AWS CDS Agentic AI Partner Hackathon, October 2026. The Pidge character, the design language and the hold-and-ask idea come from MailPidge, an earlier project by the same author; this implementation is new.

## License

MIT, see `LICENSE`.
