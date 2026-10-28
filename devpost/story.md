## Inspiration

My parents and grandparents often get scam messages and ask me for help, because they are worried when one arrives. At the same time, AI-driven hacking, deepfakes and phishing are making more people afraid. I wanted to use my technical skills to protect the people around me.

Reported losses to internet crime tripled, from $7 billion in 2021 to $21 billion in 2025 (FBI IC3). One person can now run twelve scam calls an hour with a cloned voice that costs about a dollar (Heiding et al. 2026), and people mistake a cloned voice for the real person about 80% of the time (Farid 2025). Only about 13% of scams ever reach a federal agency (Gallup 2026).

Spotting the fake gets harder every year. Asking the real person still works, but nobody does it for every message. SendPidge does.

## What it does

When an email claims to be from someone you know and asks for money, SendPidge holds it and asks that person, on their own phone, "was this you?". Their answer decides what you see, not a score.

- **Hold.** An email that uses a known person's name and asks for money, a code or urgent action never reaches the inbox unchecked.
- **Ask.** The real person is asked in the app, on WhatsApp and by email at the same moment. After 10 minutes of silence, SendPidge calls them: an AI agent asks, and a keypad press also counts.
- **Answer.** No keeps the email held and lets Grandma report it in one tap; Yes opens it after a ten-second undo. A Yes never counts on the channel the message came in on.
- **Recover.** If she already paid, "I already paid" opens a case: a note, recovery steps from a rule book, and three drafts (call script, dispute letter, police report) mailed to the personal email she saves in Settings. She makes the calls herself; SendPidge never moves money or contacts a bank or agency.
- **The home's view.** A senior home's office sees one safety state per resident, only what she chose to share, and can send one in-app safety alert to every resident.

The demo is Grandma and her son Jimin; the same check covers a finance clerk with a CEO's wire request or a supplier's new bank details.

## Try it (judges)

Open the judge link under "Try it out". No setup: you are the protected resident, `judge@sendpidge.com`.

**Judge link (sandbox access, revoked after judging).** It holds no AWS credentials, works only in this shared sandbox inbox, is rate-limited (10 contacts, 3 asks per contact per hour, 10 calls per hour), and is kept out of the repo per the hackathon's credentials rule.

1. **Circle → Add someone:** your first name, your WhatsApp number and a second email of yours (not the one you will send from). Press **Add to my circle**.
2. Press **Copy** on the note and open its link in a private window: that is your contact's screen. Press **Demo: `<name>` replies `<code>`** to verify the contact.
3. From your own mailbox, email `judge@sendpidge.com` signed with that first name, asking for money.
4. Within about ten seconds the email shows as held. Tap **No** on the contact's screen (or in WhatsApp): the resident's screen reads "Not `<name>`".
5. Optional: **Settings → Personal email** to receive recovery packs from **Cases**.

One inbox is shared by every judge; use your own first name.

## How we built it

- **Mail:** Amazon SES Mail Manager accepts mail only for enrolled residents, stores it in S3 and starts a Lambda.
- **Reader:** a Strands agent on Amazon Bedrock (Claude Haiku 4.5) with no tools returns who the email claims to be and what it wants. Code checks the sender and links and decides HOLD, WARN or PASS; an unknown-address hold gets a second opinion from Claude Opus 4.5.
- **Ask:** API Gateway WebSocket (app), AWS End User Messaging Social (WhatsApp template), Amazon SES (one-tap No link).
- **Call:** EventBridge Scheduler places an Amazon Connect call with an Amazon Q in Connect AI agent and a keypad fallback.
- **Recovery:** a case agent on Amazon Bedrock AgentCore (Runtime and Memory, Claude Opus 4.5); a chat agent behind Bedrock Guardrails.
- **Office sign-in:** Amazon Cognito managed login with a password or passkey.
- **Everything else:** one DynamoDB table, a static PWA on S3 + CloudFront, SQS, CloudWatch, Secrets Manager, all in one AWS CDK stack.
- **Process:** Claude Code, a planned build checklist, 870 offline tests and a check that refuses silent fallbacks.

## Challenges we ran into

- **Many channels, one answer.** Syncing the app, WhatsApp, email and calls, then adding AI orchestration on top, was the hardest part. The result I am proudest of is the call: an AI agent asks by voice, with a keypad fallback, so every call ends with an answer. Modern AI plus a traditional algorithm.
- **Mail arrived before its file.** Mail Manager can start the Lambda before the email lands in S3, so the Reader retries a missing object.
- **Sneaky text.** Reviews found four ways to slip a link or number past our text filter (look-alike dots, an invisible blank, `@@handle`, a Hangul dash between digits). The filter became an allow-list of characters (`lambda/shared/sanitize.py`).
- **Channel limits.** WhatsApp allows free-form messages only inside Meta's 24-hour window; Amazon Connect allows a ring timeout only for campaign calls; Q in Connect refuses a variable used twice. Each one changed the design.
- **Data size.** One public scam feed's bookkeeping row reached about 2.5 MB against DynamoDB's 400 KB item limit, so the live feeds are off and sample lists answer.
- **One phone.** A Korean SIM cannot receive the RCS sandbox agent, so RCS stays behind a flag, and one real phone plays every party in the demo.

## Accomplishments that we're proud of

- A real email held and answered live: Mail Manager → hold → WhatsApp No → Grandma's screen, and a live call answered by voice and by keypad (2026-10-28).
- Attacker swarm, offline: 504 of 504 attacks caught, 0 of 288 benign controls held. Live on the deployed pipeline: 8 of 8 attacks held or warned, 0 of 2 controls held.
- Case-note and chat eval on live Bedrock: 14 of 14 with a calibrated judge.
- The model that reads the scam has no tools; code holds, asks and releases.
- One ask, every channel: the app, WhatsApp and email at the same moment, then an AI voice call with a keypad fallback, so every call ends with an answer.
- Help after a loss: "I already paid" turns into a recovery pack (call script, dispute letter, police report) drafted from a rule book, never sent on her behalf.
- The whole stack is one AWS CDK deploy, covered by 870 offline tests.

## What we learned

I learned how comprehensive AWS's communication stack is: email, WhatsApp, SMS, RCS and voice all sit in one account, so one product can cover every channel. I can now see how to add these services to my existing projects.

I also learned where AI belongs in a safety product. The model is good at reading an email and saying who it claims to be and what it wants, but it should not hold the tools: code makes every decision, and an answer from a real person outranks any score. Our own reviews taught us to test with attacks rather than trust a filter: they found four ways to slip text past it, and the filter became an allow-list.

We publish our misses: links behind a trusted-service redirect, a QR code in a PDF, a callback number only, and a calendar invite are not flagged by design (531 of 748 camouflaged attacks were named at parse time).

## What's next for SendPidge

- Our first 2 to 5 customers by the end of 2026.
- Homes: outreach by WhatsApp and email, events, reminders, and passkey setup inside the app.
- Production WhatsApp numbers, SMS, and RCS in launch countries.
- Cross-organisation checks: one organisation's people confirming messages sent in their name to another.
