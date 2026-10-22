---
doc: checklist
status: approved
---

# Build Checklist

Build mode: learn (chosen 2026-10-22; switch any time)

## Slices

- [x] **1. The Reader returns a typed verdict for the fake Jimin email, and the policy says HOLD**
  Becomes usable: A local script that feeds a sample `.eml` to the Strands Reader on Claude Haiku 5.5 and prints `EmailVerdict` + the policy decision (HOLD / WARN / PASS). Pytest proves the policy table, the sanitizer and the auth-header rule. The long-lead requests (model access, SES identities, WABA template, RCS testing registration + tester invite) are filed so they ripen while later slices build.
  Why now: The riskiest unknown is whether Haiku 5.5 structured output round-trips through Strands with no tools; it must be proven before anything is deployed. Filing the approvals here is the only way they are ready by slice 5–6.
  PRD ref: `prd.md > Features and Behavior > Message Detection`, `prd.md > Features and Behavior > Hold and Verify`
  Spec ref: `spec.md > Agent Layer (Strands Agents SDK)`, `spec.md > Components > Tests (pytest, run at the end of each build step)`, `spec.md > Where It Runs and How Someone Tries It` (Day-1 list), `spec.md > File Structure`
  Build: Scaffold `lambda/shared/` (`reader_agent.py` with the model factory, `policy.py`, `auth.py`, `sanitize.py`), `lambda/tests/` (`test_policy.py`, `test_sanitize.py`), a `scripts/read_eml.py` runner and `samples/fake-jimin.eml` (money ask + the injection line); apply the Reader hardening (confidence wording, EN/KO cues, boundary tag). File the day-1 requests: Haiku 5.5 access, SES identity for the sender and Jimin's `contactEmail`, WABA sandbox + "Was this you?" template (TTL 60 s, UTILITY), `infra/scripts/rcs_agent_setup.sh` + tester invite to Jimin's phone.
  Verify (mechanical): `pytest lambda/tests` green; `python scripts/read_eml.py samples/fake-jimin.eml` prints `intent=money`, `claimed_person=Jimin`, `confidence ≥ 0.6`, decision `HOLD`, and the logged Converse request lists exactly one tool (the `EmailVerdict` schema).
  Learner check: Run the script on the fake email and on a plain family email with no ask; the first says HOLD, the second PASS. Say whether the one-sentence summary reads like something you'd want Jimin to see.
  Commit: `Add tool-less Reader, policy and tests`

- [x] **2. Dropping the email into S3 puts the hold on Grandma's screen**
  Becomes usable: The CDK stack is deployed (S3, DynamoDB, `pidge-reader` with the Strands layer + DLQ, API Gateway WebSocket, `ws_connect`). Grandma's PWA (inbox, email detail, hold overlay with the 60 s countdown and 5 s long-press cancel) connects with her token; uploading the `.eml` to the bucket makes the `!` row appear and the hold overlay open.
  Why now: First end-to-end run on AWS and the first screen the demo shows. Mail Manager is skipped on purpose: the S3 drop is the spec's own fallback, so the pipeline works before MX propagates.
  PRD ref: `prd.md > The Core Journey` (steps 1–6), `prd.md > Screens and Layout`, `prd.md > Look and Feel`
  Spec ref: `spec.md > Components > pidge-reader (Lambda + Strands)`, `spec.md > Components > In-app channel (Amazon API Gateway WebSocket)`, `spec.md > Components > PWA Shell`, `spec.md > Data Model`, `spec.md > Look and Feel`
  Build: `infra/` CDK v2 stack (one role per Lambda, DLQ + alarm, TTLs, log retention, reserved concurrency), `graph.py`, `channels.py` with `inapp.ask` / `ws.push`, `ws_connect` with hashed per-user tokens, seed script writing the `RECEIVER#`/`CONTACT#` rows, `pwa/` shell with `app.js`, `websocket.js`, `pidge-overlay.js`, CSP and text-only body render.
  Verify (mechanical): `cdk deploy` succeeds; `aws s3 cp samples/fake-jimin.eml` → within 10 s a `REQUEST#` row is `pending` with `expiresAt` set and the `EMAIL#` row exists; the WebSocket client log shows `{state: hold}`; a second upload of the same key exits on the idempotent put (no second request).
  Learner check: Open the PWA (served locally), tap the flagged email, watch the overlay say "I will check with Jimin" and count down; try the 5 s long press. Say what feels wrong on the screen.
  Commit: `Deploy reader pipeline and PWA hold overlay`

- [ ] **3. Jimin answers No in his PWA and Grandma sees the verdict; Confirm quarantines the email**
  Becomes usable: The kernel, end to end on the in-app channel: Jimin's verification card arrives over WebSocket, his No (or Yes) is written with the conditional update, Grandma's screen shows the warning (or the green tick), Confirm tags the S3 object and writes the `LIST#address` row, and Jimin gets the SES receipt.
  Why now: This is the unique kernel; it has to exist before any phone channel. In-app needs no approvals, so nothing external can block it.
  PRD ref: `prd.md > The Core Journey` (steps 7–9), `prd.md > The Happy Path`, `prd.md > Features and Behavior > Verdict and Resolution`
  Spec ref: `spec.md > Components > pidge-verdict (Lambda)`, `spec.md > Agent Layer (Strands Agents SDK) > Verdict handler (code)`, `spec.md > Important Failure Modes`
  Build: `pidge_verdict/handler.py` (in-app parser, request/sender validation, No-beats-Yes conditional `UpdateItem`, message-id dedupe, SES `SendEmail` receipt, push, quarantine tag + `LIST#address`), Jimin's card in the PWA, Grandma's verdict / green tick / shield states, `test_verdict.py`.
  Verify (mechanical): pytest `test_verdict.py` green (late No overrides Yes, late Yes ignored, mismatched sender rejected, redelivered id ignored, receipt only to `contactEmail`); live: upload → Jimin taps No → `REQUEST#` is `denied`, Grandma's socket receives `{state: denied}`, Confirm → S3 tag `quarantine=true`, `EMAIL#.quarantined=true`, one `LIST#address` row; SES receipt visible in Jimin's inbox.
  Learner check: Two browser windows: Grandma's and Jimin's. Send the fake email, answer No on Jimin's side, tap Confirm on Grandma's; then repeat with Yes and see the green tick. Say whether the two screens tell the story in under a minute.
  Commit: `Add verdict handler, Jimin's card and quarantine`

- [ ] **4. A real email sent to Grandma's address starts the flow**
  Becomes usable: Mail Manager ingress endpoint, traffic policy and rule set (drop pre-written `X-Pidge-Auth`, add the auth header, write to S3) are in the stack; an email sent from any mailbox to `grandma@…` lands in S3 and holds. DMARC fail and a tampered header show as reasons.
  Why now: MX has had time to propagate since slice 1; this turns the demo from "upload a file" into "send an email", which is what the video shows.
  PRD ref: `prd.md > The Core Journey` (steps 1–2)
  Spec ref: `spec.md > Components > Mail Manager Inbound`, `spec.md > Agent Layer (Strands Agents SDK) > Prompt-injection trap (named)`
  Build: `CfnResource` Mail Manager resources in the CDK stack, SSM parameter for the receiver address, `auth.py` wired into policy reasons, `test_policy.py` cases for two headers → `auth_tampered`.
  Verify (mechanical): Send from Gmail to the Mail Manager address → object in S3 within a minute with exactly one `X-Pidge-Auth`; hold appears; a test email that pre-writes `X-Pidge-Auth` is dropped (no S3 object).
  Learner check: Send the fake Jimin email from your own Gmail to Grandma's address and watch the PWA; then send a normal hello and see it pass. Say whether the delay feels acceptable for the video.
  Commit: `Add Mail Manager inbound with auth header`

- [ ] **5. Jimin's phone buzzes with an RCS card; tapping No lands the verdict**
  Becomes usable: `rcs.ask` sends the rich card with Yes / No chips from the testing agent; the tap arrives on the two-way SNS topic and `pidge-verdict` routes on `postbackData`. Behind `RCS_ENABLED`.
  Why now: The first CDS phone channel and the "oh" beat. The tester invite was filed in slice 1; if RBM never delivers to the Korean SIM, the flag turns it off and the build continues.
  PRD ref: `prd.md > Features and Behavior > Verification to Real Person`
  Spec ref: `spec.md > Components > RCS channel (AWS End User Messaging SMS · RCS agent)`, `spec.md > Components > pidge-verdict (Lambda)`
  Build: `channels.py > rcs.ask` (`SendRcsMessage`, TTL 60, public Pidge PNG), two-way SNS topic + role with `SourceAccount`/`SourceArn` in CDK, SNS parser in `pidge-verdict`, `channelsSent` recording, `test_verdict.py` RCS cases.
  Verify (mechanical): `channelsSent` contains `rcs` and `rcsMessageId` is set; a chip tap produces an SNS record with `postbackData: req-id:no` and the request becomes `denied`; a redelivered `inboundMessageId` is ignored.
  Learner check: Send the fake email and watch your phone's Messages app; tap No and see Grandma's screen change. Say whether the card reads right at a glance.
  Commit: `Add RCS channel with Yes/No chips`

- [ ] **6. Jimin's WhatsApp gets the template; a button tap lands the verdict**
  Becomes usable: `whatsapp.ask` sends the approved "Was this you?" template through AWS End User Messaging Social; the button reply arrives via the WABA event destination → SNS and is parsed from `interactive.button_reply.id`. All three channels now fan out in parallel.
  Why now: Template approval filed in slice 1 has had days to land; the third channel completes the parallel pattern and the multi-CDS story.
  PRD ref: `prd.md > Features and Behavior > Verification to Real Person`
  Spec ref: `spec.md > Components > WhatsApp channel (AWS End User Messaging Social)`, `spec.md > Workflow Patterns` (Parallel)
  Build: `channels.py > whatsapp.ask` (SSM config, template JSON, `metaApiVersion` ≥ v21), WABA event destination + SNS topic in CDK, WhatsApp parser + `wamid` dedupe, `asyncio.gather(…, return_exceptions=True)` over all three with the all-fail → `unreachable` WARN, `test_policy.py` cases for one / all channels failing.
  Verify (mechanical): pytest green; live: `channelsSent` = `["rcs","whatsapp","inapp"]`; a WhatsApp button tap sets the status; with the WhatsApp parameter deliberately wrong, the hold still shows and `channelsSent` lacks `whatsapp`.
  Learner check: Send the fake email, answer on WhatsApp this time, then try answering a second time on RCS and see that the first answer stuck. Say which channel you'd lead with in the video.
  Commit: `Add WhatsApp channel and parallel fan-out`

- [ ] **7. The injection line and lookalike senders change nothing but the reasons; the swarm proves it**
  Becomes usable: `lists.py` with the sample rows (lookalike skeleton, bad domains, Reply-To ≠ From, display-name tricks) adds reasons and the `!` mark; the injection email holds like any other; `scripts/swarm.py` sends the 8 attack templates + 2 benign controls through an in-app-only receiver and prints hold rate, false holds, and an assertion that nothing from the email reached an outbound payload.
  Why now: The kernel and channels exist, so the attack set can be measured against the real pipeline instead of a mock, and the ask cap is tested under load.
  PRD ref: `prd.md > Product Decisions` (Prompt-injection beat), `prd.md > Features and Behavior > Message Detection` (lookalike sender detection)
  Spec ref: `spec.md > Components > Lookup tables (semi-stub)`, `spec.md > Decisions and Open Issues > Build-time hardening`, `spec.md > Agent Layer (Strands Agents SDK) > Prompt-injection trap (named)`
  Build: `lists.py` + `infra/seed/lists.sample.json` + seed script, reasons shown in the PWA labelled "sample data", ask cap (3 open per contact per hour, keep hold, send nothing), `scripts/swarm.py` with the templates, `test_lists.py`.
  Verify (mechanical): pytest green; swarm run: all 8 attack emails HOLD or WARN (none PASS), both controls behave (gift → HOLD, chat → PASS), zero outbound payloads contain a URL, number or address from the email, 4th request within the hour holds without sending.
  Learner check: Send the injection email and the "JImin" lookalike; see the hold and the reason line. Say whether the reason wording is calm enough for Grandma.
  Commit: `Add lookup tables, ask cap and attacker swarm`

- [ ] **8. Onboarding, celebration and the omni-inbox mock rows complete the demo**
  Becomes usable: Onboarding flow (contacts → pick emergency contact → verification sent → confirmed, mock where the picker is unsupported) writes the graph rows; the happy-path celebration moment; 3–4 mock SMS / notification rows in the inbox; timeout → shield; PWA installable on the iPhone with Web Push.
  Why now: Last by design (spec: "omni-inbox mock rows last"); none of it proves the kernel, all of it makes the video read as a product.
  PRD ref: `prd.md > Onboarding`, `prd.md > The Happy Path`, `prd.md > States and Boundaries`, `prd.md > What We're Building`
  Spec ref: `spec.md > Components > Onboarding`, `spec.md > Components > PWA Shell`, `spec.md > What Was Simplified and Why`
  Build: `onboarding.js`, celebration (CSS first; generated image only if cheap), mock rows in `app.js`, shield state, `manifest.json` + `sw.js` + VAPID push, `scripts/preflight.py` (model access, template APPROVED, SNS subscriptions, RCS tester ACTIVE, MX, SES identities), LICENSE and prior-work note.
  Verify (mechanical): Onboarding writes `RECEIVER#`/`CONTACT#` rows and the PWA reconnects with the new token; `preflight.py` reports every check green; Lighthouse PWA installability passes; a push arrives on the installed PWA.
  Learner check: Install the PWA on your iPhone, run onboarding from a clean table, then play the full scam scene and the happy path once each. Say what you'd cut or change before recording.
  Commit: `Add onboarding, celebration, mock rows and preflight`

## Hands-on Checkpoints

- [ ] Early usable behavior explored — after slice 3 (kernel end to end on in-app; feedback shapes the screens before phone channels)
- [ ] Phone channels explored — after slice 6 (RCS + WhatsApp on the real phone; decides which channel leads the video)
- [ ] Final kick-the-tires exploration and feedback completed — after slice 8

## Final Review

- [ ] Final review complete — feedback resolved and learner confirms ready to ship

## Code Tour and App Map

- [ ] Learning activity complete — guided route, focused alternative, prior practice connected, or brief recap
- [ ] Optional edit and transfer reflection addressed — offered/declined/already covered/not applicable as appropriate
- [ ] `devpost/app-map.html` generated from finished code, checked, and shown, including a project-grounded practice to reuse

Activity and evidence: [what actually happened; real document/test/code references; unfinished work if interrupted]
Route and stops: [actual paths and symbols; guided stops completed, or reference-only route]
Edit outcome: [tried/kept/reverted/declined/not applicable; verification if changed]
Reflection: [offered/answered/declined/already covered — personal answer belongs only in the ignored profile]
Activity mode: [live app and editor, explicit static fallback, focused alternative, prior practice, or recap]

## Revisions

- 2026-10-22, slice 1: added `WarnReason.LOW_CONFIDENCE` for an ask from a known contact below the 0.6 threshold (the spec's `elif intent != none` branch labelled it `unknown_name`, which was wrong for a known name; still WARN, never HOLD). Added `lambda/shared/email_parse.py` (.eml → sender, subject, text body, X-Pidge-Auth values; the spec's file tree had no parser) and `lambda/shared/enums.py` per the readability rule. Zero `X-Pidge-Auth` headers is `auth_missing`, not `auth_tampered` (hand-dropped `.eml` files in slice 2 would otherwise all read as tampered). `samples/family-hello.eml` added for the learner check; `pyproject.toml` (pytest path), `requirements-dev.txt`, `.env.example` added. Offline check: Strands 1.62 sends exactly one tool, `EmailVerdict`, and no `outputConfig`, as the checklist assumed.
- 2026-10-22, slice 1: Haiku 5.5 returns "not available for this account" even with model access authorized and the pricing agreement AVAILABLE; Haiku 4.5 (the spec's named fallback, profile `us.anthropic.claude-haiku-4-5-20251001-v1:0`) works, so local `.env` runs `READER_MODEL=haiku-4.5` until AWS enables 5.5. Live verify on 4.5: fake Jimin → money, Jimin, 0.95, HOLD, injection line ignored; family hello → none, PASS; Converse request carries exactly one tool, `EmailVerdict`.
- 2026-10-22, slice 2: the public Strands layer (`strands-agents-py3_12-x86_64:2`) ships Strands 1.40, not the 1.62 slice 1 verified, and newer versions are not public; `infra/scripts/build_layer.sh` builds our own layer from `lambda/requirements.txt` (pinned PyPI packages, manylinux x86_64, 70 MB, no Docker). Receiver address and user id are CDK context → Lambda env vars instead of SSM (one value, POC). Raw emails land under `inbox/` so the 30-day lifecycle can target that prefix (S3 lifecycle can't exclude a tag; Confirm in slice 3 copies to `quarantine/`). Access tokens are stored as `TOKEN#<sha256>` rows (lookup by hash, no plain token in the table); connections are dual-written `CONN#<id>` and `USER#<uid>/CONN#<id>` so pushes can find a user's sockets. `inapp.ask` counts as sent once the request row exists (the PWA reads it on connect), so a closed PWA never turns the hold into `unreachable`. TTL uses `ttlEpochSeconds`, separate from the 60 s `expiresAt`. Reload resync waits for the `$default` route in slice 3. `scripts/seed_graph.py` and `scripts/ws_listen.mjs` added for the verify. WhatsApp template `pidge_was_this_you` (en_US, UTILITY, TTL 60 s) submitted 2026-10-22, Meta id 1089108530578239, PENDING.
