# SendPidge

**When an email claims to be from someone you know and asks for money, SendPidge holds it and asks that person, on their own phone, "was this you?". Their answer decides what you see, not a score.**

Built on **Amazon SES Mail Manager**, **AWS End User Messaging Social (WhatsApp)**, **Amazon Connect**, **Strands Agents on Amazon Bedrock** and **Amazon Bedrock AgentCore**, for the AWS CDS Agentic AI Partner Hackathon (October 2026).

**Demo video (3 min):** https://youtu.be/rCe4ztLSmQ4

> Grandma, Jimin and the bank are invented, and one real phone plays every party in the demo ([which moments are staged](#judge-sandbox)). SendPidge never moves money and sends nothing to a bank. Pidge is the character in the app; SendPidge is the product.

## Architecture

![SendPidge architecture: catch the email, hold or pass, reach the real person, act on the answer, the home's view](architecture/architecture-overview.png)

SVG sources and the three detail diagrams are in [`architecture/`](architecture/).

## The problem

<!-- founder: confirm the four numbers and add the source links -->
- Reported losses to internet crime tripled, from $7 billion in 2021 to $21 billion in 2025 (FBI IC3).
- One person can run twelve scam calls an hour with a cloned voice that costs about a dollar (Heiding et al. 2026).
- People mistake a cloned voice for the real person about 80% of the time (Farid 2025).
- About 13% of scams ever reach a federal agency (Gallup 2026).

Spotting the fake gets harder every year. Asking the real person still works, but nobody does it for every message. SendPidge does: for Grandma in the demo, and the same check for a CEO's wire request or a supplier's new bank details.

## What it does

- Holds an email that uses a known person's name and asks for money, a code or urgent action.
- Asks that person "was this you?" in the app, on WhatsApp and by email at the same moment.
- Phones them after 10 minutes of silence; an AI agent asks, and a keypad press also counts.
- Shows the answer live on Grandma's screen: No keeps the email held, Yes opens it after a ten-second undo.
- After a No, one tap confirms the scam and reports it; "I already paid" opens a case and a recovery pack she uses herself.
- Gives a senior home's office one safety state per resident, only what she chose to share, and an in-app safety alert to every resident.

## How it works

![Fraud prevention: the hold-and-ask pipeline with WhatsApp texts in, the Amazon Connect call, the composer out, and the office's Cognito sign-in](architecture/architecture-stretch.png)

### 1. Catch the email

- **Amazon SES Mail Manager** accepts mail only for enrolled residents (an address list plus a traffic policy) and checks SPF and DKIM.
- A rule set writes the raw email to **S3** and invokes the Reader.

### 2. Read, then decide in code

- `pidge_reader` (**Lambda**) runs a **Strands** agent on **Bedrock** (Claude Haiku 4.5) with **no tools**. It returns who the email claims to be, what it wants, and how sure it is.
- Code checks the sender and links: a known name from an unknown address, look-alikes, hidden or disguised links, scam-list senders.
- `lambda/shared/policy.py` picks HOLD, WARN or PASS.
- An unknown-address hold gets a second opinion from Claude Opus 4.5. If the two name different people, the hold stays.

### 3. Ask the real person

A HOLD writes a request row in **DynamoDB**, then asks in three places at once:

- **App:** a push over the **API Gateway WebSocket**.
- **WhatsApp:** **End User Messaging Social**, an approved template with Yes / No buttons; replies come back through **SNS**.
- **Email:** **SES**, a signed one-tap No link, or a reply of NO through Mail Manager.

### 4. Call if nobody answers

- After 10 minutes, **EventBridge Scheduler** fires the re-ask, which places an **Amazon Connect** call.
- An **Amazon Q in Connect** agent asks, knowing only whose name, what kind of message, what it asked for and how long ago; never amounts, senders, links or numbers.
- Keypad fallback: 1 = yes, 2 = no. Code records the answer through a `RecordVerdict` tool.

### 5. Act on the answer

- The verdict is pushed to Grandma's open screen. **No:** the email stays held; one tap confirms and reports. **Yes:** it opens after a ten-second undo.
- The contact can take back a No for five minutes. A Yes never counts on the channel the message came in on; a No counts from anywhere; email can answer No only.

### 6. Recover a payment

![Act on the answer: three paths into the case agent](architecture/architecture-after-verdict.png)

- "I already paid" asks the country and payment method; code opens the case and the case agent on **Bedrock AgentCore** (Runtime plus Memory) writes the note.
- Code builds the pack from the rule book: steps with deadlines, a call script, a dispute letter, a police report, mailed to the personal email she saves in Settings.
- The case agent has no tool that sends; Grandma makes the calls herself. Her questions go to a chat agent behind a **Bedrock Guardrail**.

### 7. The home's view

![Organisation oversight: the projection Lambda turns a resident's rows into a safety state through her privacy filter](architecture/architecture-stretch-org.png)

- Each resident picks in her own app what the home sees: alerts only, alerts and cases, or nothing.
- A projection Lambda turns her rows into one safety state per member (held, confirmed "not me", case open, not shared, and so on).
- The office signs in through **Amazon Cognito** managed login (password or passkey) and sees a members table and follow-ups.
- One in-app safety alert goes to every resident, at most 2 an hour per home; she answers "Got it" or "Need help".

### The app

One static PWA on **S3 + CloudFront** (`app.sendpidge.com`). Residents and contacts open it from a tokened link; the office signs in.

## Safety and privacy

- **The model reads, code decides, the real person answers.** The Reader has no tools; code owns the hold, the ask, the release and every message that leaves.
- **No agent text reaches a person with a link, an address, a messenger handle or a long number in it.** `lambda/shared/sanitize.py` refuses such case notes and chat replies and strips the Reader's summary.
- **The call agent** never asks for codes or personal data and never talks about money.
- **Caps fail closed:** 3 asks per contact per hour, 10 calls per hour, 2 alerts per home per hour, 180-second calls. Over a cap nothing is sent; a hold stays held.
- **The office sees a state, never the mail.** The resident sets the filter.
- Mail renders as text under a strict Content Security Policy; secrets live in Secrets Manager; `gitleaks` runs on every commit.

## Agents

| Agent | Runs on | Tools | Where |
|---|---|---|---|
| Reader | Claude Haiku 4.5, Strands, structured output | none | `lambda/shared/reader_agent.py` |
| Second reviewer | Claude Opus 4.5, Strands | none | `lambda/shared/escalation.py` |
| Case agent | Claude Opus 4.5 on AgentCore Runtime + Memory | the case, the family's history, the refund rule, the pack | `lambda/shared/case_agent.py`, `infra/lib/case-agent.ts` |
| Chat agent | Strands, behind Bedrock Guardrails | her own held emails, cases and notes | `lambda/shared/chat_agent.py` |
| Call agent | Amazon Q in Connect | `RecordVerdict` | `lambda/shared/voice_ask.py`, `infra/connect/pidge-voice-instructions.txt` |

Every tool-holding run stops at 8 model turns (`lambda/shared/agent_run.py`).

## AWS services

| Service | Why |
|---|---|
| **Amazon SES Mail Manager** | Inbound mail for enrolled residents; an unlisted recipient is refused at SMTP |
| **Amazon SES** | The ask by email, verdict receipts, the recovery pack |
| **AWS End User Messaging Social** | The WhatsApp ask and its replies (via Amazon SNS) |
| **Amazon Connect + Amazon Q in Connect** | The call, with an AI agent and a keypad fallback (Seoul region) |
| **Amazon Bedrock** | Claude Haiku 4.5 and Claude Opus 4.5; Guardrails on chat |
| **Amazon Bedrock AgentCore** | Runtime and Memory for the case agent |
| **Amazon Cognito** | The office's sign-in (managed login, password or passkey, PKCE) |
| Everything else | AWS Lambda (one function per job), Amazon DynamoDB (one table), Amazon API Gateway WebSocket, Amazon EventBridge Scheduler, Amazon S3 + CloudFront, Amazon SQS + CloudWatch, AWS Secrets Manager |

One AWS CDK stack (`infra/`). Set up outside it: the Amazon Connect instance and flow, the WhatsApp number and template, the SES domain and MX. Built but off (see Limitations): RCS, SMS and the live scam-list feeds. Not deployed: the rules keeper (`scripts/run_rules_keeper.py`, runs from a laptop on mock pages).

## Judge sandbox

Try the live app yourself, with no setup. The [demo video](https://youtu.be/rCe4ztLSmQ4) shows the same flow.

- **Try it (judges):** open the judge link under "Try it out" on the Devpost page (sandbox access, rate-limited, revoked after judging; kept out of the repo per the credentials rule). No setup or code needed; everything is in the app. You are the protected resident, `judge@sendpidge.com`. The three emails already in the inbox are built-in samples.
  1. **Circle → Add someone.** Name: your own first name. Add your WhatsApp number and a second email address of yours (not the one you will send from). Press **Add to my circle**.
  2. The next screen shows a note to your contact with a code. Press **Copy**, open the link from the note in a private window: that is your contact's screen. Then press **Demo: `<name>` replies `<code>`** to verify the contact (nothing is sent).
  3. From your own mailbox, email `judge@sendpidge.com`, signed with that first name, asking for money.
  4. Within about ten seconds **Inbox** shows the email held. SendPidge asks your contact on WhatsApp and by email; the contact's screen always works even if WhatsApp does not arrive.
  5. Tap **No** on the contact's screen (or in WhatsApp). The resident's screen reads "Not `<name>`".
  6. Optional: **Settings → Personal email**, type your address, **Send code**, enter the 6-digit code from that inbox, **Save**. Recovery packs from **Cases** ("I already paid") are mailed there.

  If nobody answers: SendPidge asks again at 10 minutes, marks "did not answer" at 20, and the email stays held. One inbox is shared by every judge; a name already in the circle is refused, so use your own first name.

| Moment | Staged or live |
|---|---|
| A fake "Jimin" email reaches Grandma's inbox through Mail Manager and is held | Live: a real send to the deployed stack (proven 2026-10-28) |
| Jimin gets the WhatsApp ask and taps No; Grandma's screen changes | Live, one phone playing Jimin |
| SendPidge calls Jimin; he answers by voice or keypad | Live, one phone playing Jimin (proven 2026-10-28: a No by voice, a Yes then keypad 1) |
| The scammer, Jimin and the other senders | Staged: the same founder phone and mailboxes play every party |

## Evidence

| Measure | Result |
|---|---|
| Offline tests (`bash scripts/run_tests.sh`) | 870 passed, 6 expected failures (2026-10-29) |
| Attacker swarm, offline (`scripts/swarm.py --generate --seed 7`; scripted verdicts, no model) | 504 of 504 attacks caught; 0 of 288 benign controls held |
| Swarm: camouflage named at parse time | 531 of 748. Not flagged by design (published misses): links behind a trusted-service redirect, a QR code in a PDF, a callback number only, a calendar invite |
| Swarm, live on the deployed pipeline (2026-10-25, 10 emails into the real bucket) | 8 of 8 attacks held or warned; 0 of 2 controls held; 0 outbound summaries with a URL, number or account |
| Live Reader on the 7 sample emails (Claude Haiku 4.5, 2026-10-28) | All 5 with a recorded expected result matched (3 HOLD, 1 WARN, 1 PASS) |
| Case-note and chat eval (`scripts/eval_agents.py`, live Bedrock, calibrated judge, 2026-10-28) | 14 of 14 |

## Setup instructions

For developers: run the tests, or deploy your own copy of the stack to an AWS account.

Tests need Python 3.12 with a `.venv` holding `lambda/requirements.txt` plus pytest and moto, and `npm ci` in `infra/`. No AWS credentials: the stack test synthesises with fake mailboxes.

```
bash scripts/run_tests.sh
```

One sample email through the Reader (one Bedrock call):

```
AWS_PROFILE=<your profile> READER_MODEL=haiku-4.5 .venv/bin/python scripts/read_eml.py samples/fake-jimin.eml --known-address jimin@example.com
```

A full deploy needs, in `us-east-1`: an SES-verified domain with its MX at a Mail Manager ingress endpoint, a Meta-approved WhatsApp number and template in End User Messaging Social, an Amazon Connect instance with the `pidge-ask` flow and a Q in Connect assistant, and Bedrock plus AgentCore access. Fill `.env` from [`.env.example`](.env.example) and the context of `infra/cdk.json`, then:

```
bash infra/scripts/build_layer.sh
bash infra/scripts/build_case_agent.sh
cd infra && npx cdk deploy --require-approval never
```

Seed the rule book and the sample lists with `scripts/seed_rows.py`, and a family with `scripts/enrol_resident.py`.

## Judging criteria map

| Criterion | Where to look |
|---|---|
| Technical execution (40%) | Three CDS services: Amazon SES Mail Manager, AWS End User Messaging Social (WhatsApp) and Amazon Connect, wired to Amazon Bedrock, AgentCore, Strands and Q in Connect ([Agents](#agents), [AWS services](#aws-services)). One CDK stack (`infra/`), reproducible with [Setup instructions](#setup-instructions); tests and stack assertions in `lambda/tests/` |
| Potential value / impact (20%) | [The problem](#the-problem) with its numbers; measured catch rate in [Evidence](#evidence); families now, organisations such as senior living homes next |
| Demo presentation (20%) | The [video](https://youtu.be/rCe4ztLSmQ4) runs the end-to-end agentic flow: email held, real person asked, answer back, call; recovery and the organisation view are in the supplementary file; the [judge sandbox](#judge-sandbox) lets you run it yourself |
| Creativity (10%) | The check asks the impersonated person instead of scoring the message; the model that reads the scam has no tools, and code decides |
| Functionality (10%) | The [judge sandbox](#judge-sandbox) and the live runs in [Evidence](#evidence); serverless throughout (Lambda, DynamoDB, SQS, API Gateway); AI reads the email, runs the call and drafts the recovery pack |

## Limitations

- RCS and SMS are built but off and have never run live (the RCS agent is tester-only and refuses Korean numbers; no SMS number is provisioned).
- Recovery is drafts only. The rule book (`infra/seed/refund_rules*.json`) covers the US, UK, Canada, Australia, Germany, France, Japan, Korea and Singapore, mostly card and wire; with no matching row the pack has no steps, never invented ones.
- Scam lists are sample data; the KISA and AMF feed sync is off.
- The judge sandbox is one inbox shared by every judge, and a circle holds 10 contacts.
- The office sees one home; outreach by WhatsApp or email, events and reminders are not built.
- Not checked live yet: WhatsApp texts into Grandma's inbox and her replies; the home's safety alert; passkey sign-in; the personal email code in Settings.
<!-- founder: update after the live checks -->

## Roadmap

- Homes: outreach by WhatsApp and email, events, reminders, a command center; passkey setup inside the app.
- The "I made a mistake" button on WhatsApp after a No on the call.
- Out of the sandboxes: production WhatsApp numbers and quotas, SMS, RCS in launch countries.
- Cross-organisation checks: one organisation's people confirming messages sent in their name to another.

## Disclosures

- Built during the submission period: the first commit is 2026-10-22.
- Claude Code was used throughout.
- The Pidge character, the design language and the hold-and-ask idea come from MailPidge, an earlier project by the same author; this implementation is new.
- Every person, message and bank in the demo and the samples is invented. The sample scam lists are hand-written; `infra/seed/lists.public.json` carries each public source's attribution.

## License

Apache License 2.0, see `LICENSE`.
