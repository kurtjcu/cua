# claude-usage-audit

This repo is local tooling plus Claude Code agent personas for auditing Claude usage on a **personal** and a **work** account: efficiency, blockers and permission prompts, and repeating prompt patterns mapped to the dev cycle, then a comparison of the two accounts.

- **Raw data stays on your machine.** Deterministic Python (stdlib only) reduces the raw transcripts to a redacted summary.
- **Agents read only that summary.** This keeps the audit itself cheap in tokens.

## Install

No packaging — `cua` is standard-library only and runs from the clone, so there's nothing to `pip install`. You need **Python 3.9+** and **git** (git powers `cua version` and the update check).

```bash
git clone git@github.com:<owner>/claude-usage-audit.git
cd claude-usage-audit
```
Run everything as `python3 -m cua …` from the repo root: it reads `.env` and writes to `out/` there, and the project's permission rules are scoped to this directory.

## Setup (once)
```bash
cp .env.example .env                      # then edit; most people only set CLAUDE_DIR
python3 -m unittest discover tests        # sanity check, no deps
mkdir -p exports                          # optional: put claude.ai export zips here (gitignored)
#   exports/me-<date>.zip         ← claude.ai: Settings → Privacy → Export data
#   (a work claude.ai export may need an org admin on Team/Enterprise plans)
```
Optional: copy `pricing.example.json` to `pricing.json` and fill in current per-model prices to get cost estimates.

## Run everything with agents
```bash
claude            # the account that runs the agents need not be the one being analysed
> /audit
```
The skill does the following:
1. Scans the account(s) in your `.env` (`~/.claude` by default), plus any export zips.
2. Fans out 3 analyst agents per account (3 lenses).
3. Runs the comparator when there are two accounts.
4. Writes `out/AUDIT.md` and `out/proposed/`.

Nothing in your real settings changes unless you ask.

The project `.claude/settings.json` pre-allows exactly the commands the audit needs, so it runs without prompts. It also denies agent `Read` of the raw transcript folders and exports, and denies network access.

## Run just the deterministic part
```bash
# Single account (dir and label come from .env):
python3 -m cua scan --out out/me
# Or two accounts to compare:
python3 -m cua scan --label work    --claude-dir ~/.claude-work    --export exports/work-*.zip    --out out/work
python3 -m cua scan --label private --claude-dir ~/.claude-private --export exports/private-*.zip --out out/private --project-names hash
python3 -m cua compare out/work out/private --out out/compare.md
```
Each `out/<account>/` gets three files:
- `summary.json`: all the metrics
- `samples.jsonl`: one redacted row per prompt
- `report.md`: a readable report

| Flag | Effect |
|---|---|
| `--claude-dir` (repeatable) | Add other machines, e.g. a WSL `~/.claude` copied over |
| `--since 90d` | Limit the time window |
| `--no-snippets` | Remove all prompt text from the output |
| `--project-names hash` | Anonymise project names |

## Layout
```
SPEC.md                         what is measured and why
cua/sources/claude_code.py      transcript parser: usage dedupe, denial classification, subagents, compactions
cua/sources/claude_ai.py        claude.ai export parser (token estimates)
cua/permissions.py              rule keys, settings matching, prompt estimation, risk tiers
cua/classify.py                 dev-cycle phases, correction/continue signals, prompt clustering
cua/analyze.py                  builds summary.json + samples.jsonl
cua/report.py                   per-account report + comparison
.claude/agents/                 efficiency, blockers, patterns analysts + cross-account comparator
.claude/skills/usage-audit/     the orchestration procedure (/audit)
.claude/skills/sdl-audit/       optional change-management compliance audit (cua sdl)
cua/config.py                   loads .env (account dirs, labels, ticket prefix, policy)
tests/                          unit tests + synthetic fixture generator
```

## Known limits
- **Approved permission prompts aren't logged.** They are inferred from your current allow rules and defaultMode.
- **claude.ai exports have no token counts.** Usage there is estimated at about 4 characters per token.
- **Managed/org policy settings aren't parsed.** Run `/permissions` in Claude Code to see the effective rules.
