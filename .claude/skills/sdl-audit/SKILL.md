---
name: sdl-audit
description: Audit agent transcripts for adherence to a secure-development-lifecycle / change-management policy — branch discipline, issue-tracker traceability, PR workflow, segregation of duties, secrets, dependencies. Use when asked to check whether agents followed the change-management process.
---
# SDL compliance audit

Checks whether **agents** (main thread and subagents) followed a secure-development-
lifecycle / change-management process when they changed code. It judges agent
*conduct* from transcripts — the branches they worked on, the commits and pushes
they made, the PRs they opened, secrets and dependencies they touched. It does
**not** judge repo setup (branch protection, environments) or human approvals (UAT
sign-off, release approval) — those it lists for a human to attest.

Run from the repo root. Configure org specifics in `.env` (see `.env.example`):
- `TICKET_PREFIX` — your issue-tracker key(s), e.g. `PROJ` or `PROJ,OPS`. Blank disables
  the traceability checks.
- `POLICY_NAME` / `POLICY_DOC` — the policy the analyst cites. Ships with a generic
  `references/example-sdl-policy.md`; point `POLICY_DOC` at your own extract to use it.

Read the configured policy doc and `SPEC.md` if you haven't this session. Do **not**
`ls` the config dirs — the project's `Read(~/.claude*/projects/**)` deny rule also
blocks `ls` there. `cua sdl` is the only step that opens a transcript.

Ask once whether to limit the window (e.g. `--since 60d`); default is all history.

## 1. Extract (the only step that touches raw data)
```
python3 -m cua sdl --out out/me
```
- Uses `CLAUDE_DIR` / `TICKET_PREFIX` / `POLICY_NAME` from `.env`. Override with
  `--claude-dir`, `--label`, `--since`, `--include-project '<glob>'` as needed
  (quote globs, or the shell expands them).
- To scope to one set of repos, add `--include-project '<glob>'`.
- It writes `out/<label>/sdl.json` (rollups) and `out/<label>/sdl-events.jsonl` (one
  distilled, redacted event per finding). No commit bodies, PR bodies or file
  contents leave the extractor — only check ids, controls, shapes, booleans and a
  redacted head. The stderr line reports events, violations and attention counts.

## 2. Judge (one analyst per account)
Dispatch `sdl-compliance-analyst` for each account you extracted. Prompt only needs:
"Account: <label>. Inputs in out/<label>/ (sdl.json, sdl-events.jsonl). Policy doc at
<POLICY_DOC>. Follow your instructions." Add one line on the window and scope.
- If the subagent can't write its report ("Subagents should return findings as
  text"), save it yourself to `out/<label>/sdl-compliance.md` before reporting.

## 3. Report back
Give the user the analyst's TL;DR and point them to `out/<account>/sdl-compliance.md`.
- Lead with the violation count (hard-rule breaches) and then the systemic gaps.
- A common headline is a **convention gap, not a breach**: e.g. a repo that runs
  milestone branches (`feature/<n>-<name>`) rather than `feature/<TICKET-id>` rarely
  threads an issue id — a traceability gap to decide on, not a push-to-prod incident.
- Surface the **human-attestation checklist** as the part the audit cannot close.
- **Propose, don't apply.** Any fix (a commit-msg hook requiring an issue id, branch
  guidance, a dependency-assessment step) goes in `out/proposed/` or as a suggestion;
  the user decides. Never edit real settings or add hooks without them naming them.
