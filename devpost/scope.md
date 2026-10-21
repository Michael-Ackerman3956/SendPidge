---
doc: scope
status: approved
round: 2 (2026-10-20; round 1 archived in devpost/archive-v1/)
---

# SendPidge

One line: an AI agent that, when a message claims to be someone you know and asks for money or action, asks the real person "was this you?" before the receiver acts.

## The Problem

Someone receives an email or message that claims to be a person they already know — a family member, a colleague, a boss — and asks for money, a code, or an urgent action. The message looks ordinary: a clean address, passing authentication, nothing a spam filter or reputation score can flag, because a fresh account from a free provider carries no history. The receiver could confirm it, since they already hold something the impersonator doesn't (the real person's number, an old thread, another channel), but that check almost never happens. Verifying costs three things each time: knowing that a check is needed at all, knowing how to do it, and the minutes and nerve to do it while the message says "hurry." The scammer never has to defeat a check; they only have to make it feel not worth doing. Platforms stop at generic warnings because none holds a verified, consenting link between the receiver and the real sender, so none can ask "was this you?" on the receiver's behalf — and scams move across email, SMS and messengers while each platform sees only its own channel. The evidence is secondary: family-impersonation messages (메신저피싱) in Korea, BEC wire fraud ($3.05B, FBI IC3 2025), and AI voice-clone distress cases in the US. Investment and romance scams are excluded because there is no known person to check against; calls are out of scope; email and messages are in.

### Short version
Someone gets a message from a person they think they know, asking for money or action under pressure, and nothing on the platform can tell it from the real thing. They could check on another channel they already have, but the cost of checking — knowing to, knowing how, finding the nerve — is higher than trusting. No platform can do it for them, because none holds a verified link to the real sender.

### Root cause, in one line
Known-party impersonation works because verifying against the channel the receiver already has is possible but too costly per message, so it never happens.

### Evidence (secondary, from `devpost/research/scam_landscape/`)
- FBI IC3 2025: BEC $3.05B; AI voice-clone family distress cases >$5M, tracked separately for the first time (01_global_economy.md, 04_elder_fraud.md)
- Korea: 메신저피싱 "엄마 나 폰 고장났어" pattern; 지정인 알림 exists but is notification-only, 65+, bank products (02_south_korea.md)
- FBI Operation Level Up: 78% of contacted victims did not know they were being scammed; a credible outside voice interrupting works (04_elder_fraud.md:95)
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

Open for PRD/spec: which channel carries the question to Jimin (email, SMS, WhatsApp). Round 1 recommended email-to-email first because it needs no carrier or WhatsApp approval, with an upgrade path.

## Later
- Multiple trusted contacts and disambiguation when no name is given
- SMS and messenger inbound, not just email
- Onboarding the trust circle (consent flow) as a product surface
- Institution impersonation ("fake police", "fake bank") as the known party
- B2B version: supplier PO fraud, fake insurance claims, CEO wire requests

## Explicitly Cut
- Investment and romance scams: no known person to ask, different root
- Voice calls: out of scope by decision; can't be demoed credibly
- Reputation or trust scores: structurally blind to fresh free-provider addresses
- Link preview or URL safety checking: a different problem (link uncertainty), not this root
