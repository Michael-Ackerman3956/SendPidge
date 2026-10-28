# Claude Code Instructions

Engineering rules for every agent are in `AGENTS.md`. This file covers how Claude Code works in this repo.

## Communication

- Terse: all substance, no filler, no preamble, no trailing recap. Answer a direct question in the first
  sentence.
- Questions and status answers get one or two short paragraphs. A table only when comparing; no bullet
  walls or headers in short replies.
- Code changes in chat: `file:line — what changed`. Show BEFORE/AFTER only when the change is subtle
  (behaviour, semantics, security), a multi-file refactor, or something flagged as risky.
- Review findings: `file:line — verdict`. Report only what a tool result backs.
- Go long only for security warnings, irreversible actions or architecture decisions.

## Subagents

- Spawn one only for work that cannot finish in a few tool calls or needs parallel independent tracks.
  Never to re-check your own work, except a fresh-context reviewer at a phase gate.
- Brief implementers with exact files, contracts, flags and the tests to write. Tell them to finish
  without checking in, run nothing, and add no unrequested tests, docs or files. The main session runs
  one sequential test pass per wave, then the review.
- Every review brief asks: "Where does a failure turn into a default, and who is told?" and "Which test
  would still pass if this feature were broken?"

## Finishing work

- Finish the whole task; context pressure is never a reason to cut scope. Park items that need a
  ruling, keep working on the rest, batch the questions. Stop only when nothing can move without me,
  for a git write, or before a risky or destructive action.
- Before saying done: re-read the request, diff every changed file, map each requirement to a change,
  check the empty, error and offline states and keyboard and screen-reader access in the PWA, and name
  exactly which files changed. A false completion claim is worse than "not done yet".
- When something fails, fix the root cause; offer a workaround only if asked. After two failed
  attempts at the full fix, explain why and propose a different approach, not a degraded one.
- A build slice is done when its listed tests pass, never because it was typed as done. The plan and
  its state live in `devpost/checklist.md`; tick a slice only after its verification ran. The workflow
  skills under `.claude/skills/` (`1-start` to `6-ship`) drive planning, build and shipping.

## Files and tools

- No new `.md` files unless asked; commit messages replace implementation summaries.
- HTML goes in `docs/` (gitignored here), never through the Artifact tool. Once a page is open in the
  browser, don't `open` it again; say "changed, refresh".
- Feature flags and caps live in `infra/cdk.json`. I run `cdk deploy` and the seed and enrol scripts;
  don't invoke authenticated CLIs without flagging exactly what will run.
- No test emails, texts or calls to real people without telling me first.
- When compacting, preserve: modified files, pending decisions, active debugging context, and my
  constraints from earlier in the conversation.
