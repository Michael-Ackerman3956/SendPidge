---
doc: scope
status: approved
round: 2 (2026-10-20; round 1 archived in devpost/archive-v1/)
---

# SendPidge

One line: an AI agent that, when a message claims to be someone you know and asks for money or action, asks the real person "was this you?" before the receiver acts.

What it will not do: The model that reads the email has no tools and only classifies it; code places the hold and sends the question; Jimin answers and Grandma taps. Nothing in Pidge sends money or reaches a real bank.

## The Problem

Someone receives an email or message that claims to be a person they already know — a family member, a colleague, a boss — and asks for money, a code, or an urgent action. The message looks ordinary: a clean address, passing authentication, nothing a spam filter or reputation score can flag, because a fresh account from a free provider carries no history. The receiver could confirm it, since they already hold something the impersonator doesn't (the real person's number, an old thread, another channel), but that check almost never happens. Verifying costs three things each time: knowing that a check is needed at all, knowing how to do it, and the minutes and nerve to do it while the message says "hurry." The scammer never has to defeat a check; they only have to make it feel not worth doing. Platforms stop at generic warnings because none holds a verified, consenting link between the receiver and the real sender, so none can ask "was this you?" on the receiver's behalf — and scams move across email, SMS and messengers while each platform sees only its own channel. The evidence is secondary: family-impersonation messages (메신저피싱) in Korea, BEC wire fraud ($3.05B, FBI IC3 2025), and AI voice-clone distress cases in the US. Investment and romance scams are excluded because there is no known person to check against; calls are out of scope; email and messages are in.

### Short version
Someone gets a message from a person they think they know, asking for money or action under pressure, and nothing on the platform can tell it from the real thing. They could check on another channel they already have, but the cost of checking — knowing to, knowing how, finding the nerve — is higher than trusting. No platform can do it for them, because none holds a verified link to the real sender.

### Root cause, in one line
Known-party impersonation works because verifying against the channel the receiver already has is possible but too costly per message, so it never happens.

### Evidence (secondary, from `devpost/research/scam_landscape/`)
- FBI IC3 2025: BEC $3.05B; AI voice-clone family distress cases >$5M, tracked separately for the first time (01_global_economy.md, 04_elder_fraud.md)
- Korea: 메신저피싱 "엄마 나 폰 고장났어" pattern; 지정인 알림 exists but is notification-only, 65+, bank products (02_south_korea.md)
- Heiding et al. 2026 (arXiv 2607.09970, survey experiment N=4,100 US adults, verified 2026-10-24): "Up to 36% of participants would or might comply with phishing requests in the 'relative-in-distress' category" — the video opener; the request works, so the defense must be a check, not a warning
- FTC, Protecting Older Consumers 2025–2026 (2026-10-19): older adults "15% more likely to report a loss on a scam impersonating a friend or family member"; scammers "tell people not to speak to friends or family members" — channel-agnostic, pairs with the email demo
- FBI Operation Level Up (DOJ 2025 report, as of 2025-06-30): 77% of 6,475 contacted victims did not know they were being scammed — crypto-investment victims, so evidence that a credible outside voice works, not an opener (04_elder_fraud.md:95; replaces the earlier 78%/3,780 snapshot)
- Why platforms stop at warnings: no verified consenting link to the real person, no consent to involve a third party, cross-channel blindness, liability at scale (research_why_verify_person.md, interpretation)

### What exists and why it fails for this person
| Exists | Limit |
|---|---|
| Google Fake Call Detection (2026-06) | Calls only, Android-to-Android, spoofed saved number only |
| Korea 지정인 알림 | After the fact, 65+, bank products, designee cannot block |
| Messenger "new contact" warnings | Warns everyone about everything; nobody to ask |
| Enterprise email relationship stores | Work inside one company's mail; nobody holds a family's graph across email, SMS, KakaoTalk |

None of them asks the real person. That is the one thing the root demands.

## The Unique Kernel
The agent asks the real person, on a channel only they control, and shows their answer to the receiver. Not a score, not a warning: a reply from the person being impersonated. Delete that and it's another spam filter.

## Who It's For
Anyone who receives messages from people they think they know and may be asked to act on them: a parent trusting a message from their child, a finance clerk trusting a wire request from the CEO, an insurer processing a claim from a known supplier. The root cause is the same across all of them. The demo scenario for the hackathon will be chosen in the PRD for relatability to judges — personal scams (family impersonation) are more immediately relatable, but B2B (supplier PO fraud, fake insurance claims, CEO wire requests) is where the dollars are. The learner's own words: "nobody I really knew had this email related payment issue; I only know it from news and reading." The persona is researched, not observed, and the doc says so.

## The Core Loop
A message arrives claiming to be someone the receiver knows and asking for money, a code, or an urgent action. The agent steps in while the receiver is reading it, tells them it's checking, asks the real person on their own channel, and shows the answer with one button. Every suspicious message, same loop.

## Inspiration & Identity
Analogies the learner brought: AP three-way matching (request, proof, confirmation), escrow, maker-checker. Tone for the receiver: calm, one sentence, one button; no explainer text. Round 1 decks for reference: `docs/SENDPIDGE_THREE_WAY_MATCHING.html`, `docs/SENDPIDGE_EXECUTION_VARIATIONS.html`.

## Why This Matters to the Learner
"If a developer like me gets scared, what about normal people?" And the restart itself: "I never defined the problem proper and kept jumping into the solution." This round the problem came first; the build has to stay inside it.

## What "Working" Looks Like
In the learner's words: while the receiver is looking at the email, the AI agent steps in, informs her that it is running verification for a payment request, gets confirmation that this email was not sent from the real person, and shows "Phishing detected. Jimin replied: he did not send this" with a button "Report and block". Grandma taps it. Under a minute, on screen, recordable.

The "oh" beat: Jimin's phone buzzing with "Grandma received this, was it you?" while she's still reading.

## The POC Boundary
In:
- One receiver (Grandma), one trusted family member (Jimin), linked in advance
- One inbound email that claims to be Jimin and asks for money
- Detection that it claims a known person and asks for money or a code
- One question to the real Jimin on his channel, one yes/no answer
- Grandma's screen: "checking", then the verdict with "Report and block"
- Built on AWS; email in via SES

Channel (decided 2026-10-21, built): WhatsApp, RCS and in-app carry the question; round 1's email-to-email fallback was not needed.

## Roadmap after Oct 28 (ruled 2026-10-25, single order; the bullets under Later hold the details)
Demo framing (ruled 2026-10-25, option a): the video's title card and narration say a bank, insurer or care operator deploys this stack in its own AWS account for its users; in the demo Nestuary Wellness is the operator and Grandma/Jimin are its users. Say "deploys": there is no organisation row or dashboard in the Oct 28 build.

1. Call nudge after the verdict (Oct 28, in build)
2. WhatsApp Flow for contact enrolment via a `wa.me` tap (draft mode; the RSVP draft is the template)
3. Daily content to the receiver over Web Push and the Pidge tab (rest of E2)
4. Security command center (stretch, ruled 2026-10-25): the deploying organisation (a retiree home, bank, insurer) gets a simple dashboard of its own fraud events and cases, and Nestuary Wellness keeps an aggregate second layer of monitoring above it, so the operator is not left alone with the monitoring. Event stream, not message content: `payment doubted`, `held`, `confirmed not me`, `wrong payment made, recovery pack drafting`, `contact request`. Needs the organisation model (stretch slice S2) and the privacy rule, now written in `spec.md > Stretch`: counts and states per org, no email bodies or contact graphs at the aggregate level, resident identity only inside the org's own view. Demo-grade only for the first cut. Designed 2026-10-25: `spec.md > Stretch: Organisation dashboard and command center`, prototype `docs/SENDPIDGE_ORG_PROTOTYPE.html`.
5. Organisation outreach dashboard (stretch, ruled 2026-10-25; after the demo and the original scope are finished): the retiree home's admin signs in to the same PWA as a second party, an organisation inbox beside Grandma's, and sends over the same channel adapters: invitations, reminders, scheduled medication reminders, occasional social email, and social WhatsApp messages (birthday, riddles). Target and the hold-and-ask core stay the same; this adds the org-initiated side the CDS brief asks for (a conversation the organisation starts, the resident answers, Pidge holds anything that impersonates the organisation). Needs the organisation model (stretch slice S2) and per-organisation templates (roadmap 13); sits on the command center (4). Constraints (spec Stretch): social messages never over WhatsApp to US residents (Meta MARKETING pause), so birthday and riddles go by email and in-app; medication reminders are per-member, in-app only, flag off until counsel rules. Same spec section and prototype as 4.
6. Proactive Pidge brief: a scheduled agent messages Jimin with what needs a decision (checklist slice 14; one UTILITY template)
7. Risk-explanation panel on the verdict screen
8. Link and attachment checks as reasons (Web Risk, RDAP, lookalike domain, virus scan)
9. Organisation events with a location message + reply buttons (E3; needs an organisation model and templates)
10. Flow RSVP published for organisation events (E4; after verification, builds on 2 and 9)
11. Voice via Amazon Connect (kill test first; Seoul instance, then us-east-1)
12. SMS one-way reminders for countries without RCS/WhatsApp
13. More WhatsApp templates, later per-organisation templates
14. Notify OTP at enrolment (E1; mostly superseded by 2, kept for contacts not on WhatsApp)
15. Bring-your-own AI read-only MCP server
16. Vault concept (E5)

## Future expansion
- Carrier/OS-level check (Apple, Google, telcos), as TheCheat does with Korean carriers; needs a platform partner (2026-10-24)

## Later
- Multiple trusted contacts and disambiguation when no name is given
- SMS and messenger inbound, not just email
- Onboarding the trust circle (consent flow) as a product surface
- Institution impersonation ("fake police", "fake bank") as the known party
- B2B version: supplier PO fraud, fake insurance claims, CEO wire requests
- Call nudge (2026-10-25, roadmap 1, in the Oct 28 build): after Jimin's WhatsApp Yes or No tap, Pidge replies inside the 24-hour window with two reply buttons, "I'll call today" / "Tomorrow"; a tap shows "Jimin will call you today" on Grandma's screen (WebSocket + Web Push), schedules one WhatsApp reminder (EventBridge Scheduler) and the case agent notes it. Own hourly cap per contact, fails closed; an offer failure never touches the verdict. In-app and RCS taps get no offer (no open window). Flag `callNudgeEnabled`
- Stretch (2026-10-23): proactive Pidge brief — a scheduled agent that messages Jimin first over RCS/WhatsApp with what needs a decision (checklist slice 14)
- Mail Manager relay in front of the receiver's existing mailbox (no inbox switch); only the protected person is ever "inside" — the impersonated person just answers a text
- Stretch (2026-10-24): risk-explanation panel on the verdict screen — after the real person answers, show the receiver why Pidge held the message (intent, confidence, auth signals, listed-sender reasons). Deterministic policy logic already exists (policy.py: intent × confidence × contact match → hold/warn/pass); this surfaces it as plain-language guidance, similar to what AskSafe Home does as its entire product. Not a priority for the demo video
- Stretch (2026-10-24, ruled): link and attachment checks as reasons — Google Web Risk list match, domain age (RDAP), lookalike-of-a-contact domain, virus scan (Mail Manager add-on or ClamAV). No model call; lookups and scanners only. A match adds a reason the receiver sees: on a held email, a line under the real person's answer; on an email with no ask, the existing WARN banner (same state as LISTED_SENDER). A match never holds mail by itself, because a hold with nobody to ask is a spam-filter warning with a wait attached. One exception: a virus-positive attachment may be dropped at the Mail Manager door before Pidge reads it. Whether a link match later moves mail to quarantine or a spam folder is undecided; for now it is the mark only
- Stretch (re-ruled 2026-10-25; replaces the 2026-10-24 Chime SDK pick): voice, after the initial demo is done. Chosen: **Amazon Connect (Connect Customer)** — an instance in ap-northeast-2 (Seoul) first, because Seoul calls South Korea, the US and the UK by default with no allowlist ticket; a US DID is claimed on it; a Lambda calls `StartOutboundVoiceContact` with `requestId` in the contact attributes; the contact flow speaks the question and takes the answer two-way: keypad digit or a Lex V2 bot for spoken yes/no (scripted, FEASIBLE), or free-form through a Connect AI agent with Agentic Voice on Claude Haiku and a `RecordVerdict {yes|no|unsure}` Return-to-Control tool (FEASIBLE WITH CAVEATS: every block is documented for voice, none demonstrated on an API-started outbound call); the flow invokes the us-east-1 verdict Lambda and the answer lands like a card tap. Instances cannot move Regions: switch to a us-east-1 instance right after the hackathon demo (ruled 2026-10-25: US customers, everything else in us-east-1, Nova Sonic there, telephony cheaper — Seoul→US $0.0142/min vs us-east-1→US $0.0048; service fee $0.038/min is the same) (flows export/import as JSON; the number is re-claimed; one Support ticket "Country Allowlisting for Outbound Calls" for Korea). Kill test before any code: Seoul instance + one US DID + a 3-block DTMF flow + one `start-outbound-voice-contact` to the US test phone, then to Jimin (surfaces the new-account outbound restriction, the first-claim number limit, US-DID-in-Seoul, KR completion and caller-ID). Fallback: Jimin calls Pidge on WhatsApp through our own Meta app on a Meta test number (aws-samples `sample-multimodal-whatsapp-restaurant-agent`: aiortc + KVS TURN + Nova 2 Sonic on AgentCore). Dropped: Amazon Chime SDK PSTN Audio (maintenance mode, closed to new customers 2026-10-29, free-form needs a self-run SIP gateway); WhatsApp calling through EUM Social (business-initiated calls barred from our +1 number, 2,000-recipient tier required for all calling, inbound call webhook relay undocumented); WhatsApp video (not released). Research: 3 doc agents + Fable 5.1 review, 2026-10-25.
- Post-hackathon (2026-10-24): SMS as a one-way reminder channel for countries without RCS/WhatsApp (US first). South Korea allows no two-way SMS, no sender ID and no local number, so the answer always stays on WhatsApp/RCS. Reuses the RCS send path and SNS reply topic; about an hour, built only after the demo and voice stretch. Not on the diagram.
- Stretch, low priority (2026-10-25): WhatsApp templates as a product surface. First, a few more of our own templates (image header with the suspicious email, follow-up reply buttons after a No, a document send of the held email) to show the EUM Social template API and media capabilities. Later, when SendPidge becomes an organisation messenger (the Slack-alternative lane), an organisation admin creates custom WhatsApp templates under their own WABA through our UI calling CreateWhatsAppMessageTemplate; every member then recognises an authentic SendPidge-verified message by the organisation's own template, and `WHATSAPP_TEMPLATE_NAME` becomes a per-organisation setting. Our inbound parser is unchanged (button payload only). Constraints: Meta review up to 24 h per template, utility vs marketing category sets the price, low-quality templates get paused; in a hosted deployment the templates live under our WABA and the sender name stays ours.
- Stretch, priority 2 (2026-10-25): WhatsApp Flow. A three-screen RSVP Flow (`SendPidge`, Flow ID 2380444122755091: attending → slot + guests → ride + note, no endpoint) was built in WhatsApp Manager as a Draft the same day; source in the session scratchpad `rsvp-flow.json.txt`. Facts verified in the builder and Meta's Flows API doc: the editor and interactive preview work while business verification is in review; Publish needs verification plus a non-Low number quality rating; a Draft can be sent to a real number with `mode: "draft"` inside a 24-hour window the person opened by messaging Pidge first (shows a draft banner); a Flow without an endpoint returns its answers to our webhook as an `interactive` → `nfm_reply` message on the existing SNS topic, no RSA endpoint needed; creating Flows by API needs a Meta token (`whatsapp_business_management`), EUM Social has no Flows API, so the JSON is pasted into the builder. First product use when built: contact enrolment — the receiver's app shows a `wa.me` link, the contact's tap opens the window, Pidge replies with a Flow (name, sending addresses, consent), the submit writes the `CONTACT#` row; proves number control by construction. Demo rule: never an embedded preview iframe (its submit goes nowhere); a draft send with the banner and one README line is honest.
- Stretch (2026-10-24, low priority): bring your own AI for review — a read-only remote MCP server so the user's own Claude / ChatGPT subscription can review what Pidge already has ("what got held this week?", case notes); the check itself stays inside Pidge. Smallest version (~1 day): Lambda MCP server (Streamable HTTP) with a static API key, demoed from Claude Code; full version = AgentCore Gateway with inbound OAuth (Cognito, pre-registered client IDs since Cognito has no DCR), as CommSync does with its own OAuth server. Never answers "was this you?" or releases mail. Does not move Pidge-chat inference cost onto the user's subscription: no browser login for ChatGPT/Claude plans exists; ACP works only on desktop with a local CLI (Strongineering Path C).

## Explicitly Cut
- Investment and romance scams: no known person to ask, different root
- Voice calls: out of the Oct 28 POC; a post-demo stretch (roadmap 11, Later)
- Reputation or trust scores as the verdict: structurally blind to fresh free-provider addresses; list and scanner matches may add reasons (see Later), never decide
- Link preview, page fetching or model-reading of attachments: a different problem (link uncertainty), not this root; list lookups on links are reasons only (see Later)
