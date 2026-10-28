<!-- Outline only (founder ruling 2026-10-28): verified facts and numbers under each Devpost heading.
     The founder writes the sentences. [founder: …] marks what only you can say; [verify: …] marks a fact not re-checked on main. -->

## Inspiration

- [founder: why this matters to you, in your own words]
- Internet crime reported to the FBI: $\$6.9\text{B}$ (2021) $\rightarrow$ $\$20.9\text{B}$ (2025) (IC3)
- One person, twelve scam calls an hour, a cloned voice for about $\$1$ (Heiding et al. 2026)
- People mistake a cloned voice for the real person about 80% of the time (Farid 2025); about 13% of scams reach a federal agency (Gallup 2026)
- The root, from `devpost/scope.md`: checking with the real person works, but costs too much per message to do by hand
- Prior work: MailPidge (same author) gave the Pidge character and the hold-and-ask idea

## What it does

- An email in a known person's name asks for money → Pidge holds it → asks that person "was this you?" in the app, on WhatsApp and by email, at the same moment
- No answer in 10 minutes → Pidge calls them (Amazon Connect, an AI agent, keypad fallback)
- No keeps it held; Yes opens it after a 10-second undo; a No can be taken back for 5 minutes
- A Yes never counts on the channel the message came in on; a No counts from anywhere
- After a No: report a payment → a case note, recovery steps from a rule table and three drafts (call script, dispute letter, police report), sent to the personal email she saves in Settings; she calls her card issuer and files with the FTC, police and IC3 herself; a chat window answers questions about the case
- What it does not do: move money, contact a bank, police or agency (it drafts; she calls and files herself), call anything "safe"
- The home's view: the office signs in (Amazon Cognito, password then passkey) and sees one safety state per resident, only what she chose to share; one in-app safety alert to every resident, at most 2 an hour (the alert and passkey sign-in not checked live yet)
- Who it is for: the demo is Grandma and Jimin; the same check covers organisations (a CEO's wire request, a supplier's new bank details)
- [upload: one screenshot, Grandma's held email next to Jimin's WhatsApp ask]

## How we built it

- Mail: Amazon SES Mail Manager → S3 → Lambda
- Reader: a Strands agent on Amazon Bedrock (Claude Haiku 4.5), no tools, structured output; policy in code decides HOLD / WARN / PASS; an unknown-address hold gets a second opinion from Claude Opus 4.5
- Asks: API Gateway WebSocket (app), AWS End User Messaging Social (WhatsApp template), Amazon SES (one-tap No link)
- Re-ask call: EventBridge Scheduler → Amazon Connect + Amazon Q in Connect AI agent
- Case agent: Amazon Bedrock AgentCore Runtime + Memory (Claude Opus 4.5); chat behind Bedrock Guardrails
- Office sign-in: Amazon Cognito managed login (password or passkey, PKCE)
- State: one DynamoDB table; the app is a static PWA on S3 + CloudFront; SQS + CloudWatch, Secrets Manager; everything in one AWS CDK stack
- Process: Devpost skills (scope → PRD → spec → a sliced build checklist), Claude Code, 870 offline tests (2026-10-29) and a grep gate that refuses silent defaults

## Challenges we ran into

- RCS: a Korean SIM cannot receive the sandbox agent, so RCS stayed behind a flag, untested on a phone
- WhatsApp: a free-form follow-up is allowed only inside Meta's 24-hour window, so the "I made a mistake" button after a No on a call needs its own approved template (parked)
- Amazon Connect: a ring timeout is accepted only for campaign calls, so it was dropped
- Q in Connect: the prompt builder refuses a variable used twice
- Mail Manager: the Lambda can run before the mail object lands in S3, so the Reader retries a missing object
- Public scam feeds: one feed's bookkeeping row reached about 2.5 MB against DynamoDB's 400 KB item limit; the live feeds are off for the demo and sample lists answer instead
- Text-gate reviews found four bypass classes (look-alike dots, an invisible blank, `@@handle`, a Hangul dash between digits), so the outbound gate became an allow-list of characters (`lambda/shared/sanitize.py`)
- One real phone for every party, so some demo moments are staged

## Accomplishments that we're proud of

- A real email held and answered live: Mail Manager → hold → WhatsApp No → Grandma's screen (2026-10-28)
- A live call: a No said by voice; a Yes then keypad 1 (2026-10-28)
- Attacker swarm, offline: $504/504$ attacks caught, $0/288$ benign controls held
- Live Reader on the 7 sample emails (2026-10-28): all 5 with a recorded expected result matched (3 HOLD, 1 WARN, 1 PASS)
- Swarm, live (2026-10-25, 10 emails): 8 of 8 attacks held or warned, 0 of 2 controls held, no summary carried a URL, number or account
- Case-note and chat eval on live Bedrock: 14 of 14 with a calibrated judge
- The model that reads the scam text has no tools; code holds, asks and releases

## What we learned

- [founder: what you learned, in your own words]
- Facts the build turned up: the WhatsApp 24-hour window; Connect's campaign-only ring timeout; DynamoDB's 400 KB item limit; Mail Manager's event can arrive before the object
- [founder: what a published miss taught you] Not flagged by design: links behind a trusted-service redirect, a QR code in a PDF, a callback number only, a calendar invite (531 of 748 camouflaged attacks named at parse time)

## What's next for SendPidge

- Homes: outreach by WhatsApp and email, events, reminders, a command center; passkey setup inside the app
- The "I made a mistake" button on WhatsApp after a No on the call
- Out of the sandboxes: production WhatsApp numbers, SMS, RCS in launch countries
- Cross-organisation checks: one organisation's people confirming messages sent in their name to another
