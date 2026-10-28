# Agent Instructions

Rules for any coding agent working in this repository. `CLAUDE.md` adds how Claude Code works here.

## Answer the question asked

- Answer exactly what I asked, or do exactly what I instructed, fully and accurately.
- Don't add conclusions, insights, recommendations, strategy, positioning, or
  "what this means for us" unless I ask for them.
- Don't decide for me or reframe my question. If it is ambiguous, ask one short
  clarifying question.
- Don't append next steps, offers, or follow-up questions unless you need my
  decision to proceed.
- Stay on the topic. Don't pull in unrelated threads from earlier in the conversation.
- If a premise in my question is factually wrong, say so in one sentence, then answer.
- Exception: still flag safety, security, legal, or irreversible-action risks in one line.

## Minimal scope

- Implement the smallest clean change that does exactly what was asked. No extra features, states,
  demo controls or "while I'm here" improvements. If something beyond the request seems worth doing,
  name it in one line and don't do it.
- Prefer, in order: not writing it, reusing what is in the codebase, the standard library or platform
  API, an installed dependency, then the minimum new code. No new abstractions or dependencies unless
  asked. Deletion over addition; boring over clever.
- Back up any untracked file before a large multi-file change.

## No silent fallbacks

- A failure may become a default only if all three hold: the default is correct for every caller, the
  failure is reported (a user-visible state or an alerting log), and a test exercises the failure path.
  Otherwise raise.
- Never cache a failure as an empty result, substitute a hand-written copy for missing data, or stamp a
  version, sync anchor or "done" flag after a partial failure.
- Banned on loaded or fetched data: Python `except: pass` and `.get(key, <default>)`, `or {}`, `or []`
  on a row; JS/TS empty `catch {}` and `?? <default>`. The grep gate (`scripts/checks/grep_gate.py`,
  known hits in `scripts/checks/baseline.json`) refuses a new hit; a deliberate one goes in the baseline,
  never silenced inline.

## Enums, names, size

- Every closed set is an Enum (`StrEnum` in Python, a frozen object in JS) parsed once at the boundary.
  No bare string branches.
- Names say what a thing is for at first glance: verb-first functions, data-describing variables,
  `is/has/can/should` booleans, full words, no abbreviations or single letters outside loop indices.
  Rename cryptic names when you touch them.
- Functions do one thing, about 40 lines of logic at most. Source files about 500 lines, tests about 600;
  the grep gate enforces the file limit. Group by feature.
- Comments and docstrings state what the code does now. When behaviour changes, the comment changes in
  the same edit; a comment promising behaviour the code lacks is a bug, report it as one.

## Tests prove behaviour, not existence

- Business logic is accepted only by tests with fixed inputs and expected outputs. "It compiles and is
  wired", "not empty" and range checks close nothing.
- A test runs the production object or configuration, never a copy built inside the test.
- One source per core piece of logic: no nearby copy, hard-coded subset or formula substitute. Grep for
  duplicates and delete them.
- Before claiming done, run `bash scripts/run_tests.sh` (pytest with the grep gate and the CDK synth
  assertions, then the infra type check) and report what it printed. Say "untested" rather than
  "should work".

## Security

- An authorisation check at the top of every handler, scoped to the caller's identity from the
  connection row or token, never an id from the message body or from model output.
- Canonicalise paths and reject `.` and `..`; constant-time comparison for tokens; output-encode user
  content in HTML; verify signed webhooks before parsing; rate limits and caps fail closed.
- No model text reaches a person with a link, an address or a contact number in it unless the outbound
  gate let it through.
- Secrets never in argv, logs, chat or tracked files. Mask emails and phone numbers in anything that
  may be published.

## UI copy

- Show only labels, values and errors the user needs to act. No explainer paragraphs, taglines or hints
  that restate the label. If a fact matters, one short subtitle or an error state.

## Git

- Never run `git checkout`, `reset`, `clean`, `stash drop`, `revert`, `commit`, `push` or `merge`
  without explicit permission. Read-only git is always fine.
- Stage named files only; never `git add -A` or `git add .`. Inspect the staged diff for secrets and
  unrelated work before any commit.
- One working tree. No branches or worktrees unless asked.
