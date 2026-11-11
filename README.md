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
> /cua-audit
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
python3 -m cua preflight                  # list config dirs + exports without reading transcripts
# Single account (dir and label come from .env):
python3 -m cua scan --out out/me
# Or two accounts to compare:
python3 -m cua scan --label work    --claude-dir ~/.claude-work    --export exports/work-*.zip    --out out/work
python3 -m cua scan --label private --claude-dir ~/.claude-private --export exports/private-*.zip --out out/private --project-names hash
python3 -m cua compare out/work out/private --out out/compare.md
```
`preflight` is the safe first look: it prints the config dirs and export zips cua will read (and flags a shared transcript folder) without opening a single transcript.
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

### Where time goes, and which prompts you approved (optional)
```bash
python3 -m cua waits --out out/me                        # uses PROMPT_LOG from .env
```
`waits` measures where sessions sit still — how long permission dialogs kept you waiting, and other pauses. If you log permission prompts with a `PermissionRequest` hook and set `PROMPT_LOG` in `.env`, it also emits `approved_prompts`: the prompts you actually said yes to, grouped by shape with a suggested allow rule and risk tier — the precise input for trimming your allowlist. Without a prompt log it still reports waits, but `approved_prompts` is empty (Claude Code doesn't log approvals on its own). The `/cua-audit` skill runs this step automatically when `PROMPT_LOG` is set, and the blocker analyst prefers it over the estimate.

A ready-made, shape-only logger ships in [`hooks/`](hooks/) — `python3 hooks/install.py` sets it up (it asks first and only records the *shape* of each prompt, never values). See [`hooks/README.md`](hooks/README.md).

### Audit the config layer (optional)
```bash
python3 -m cua lint --root ~/code --out out/lint     # reads config files only, never a transcript
```
`lint` inspects the configuration around Claude Code — `CLAUDE.md`, skills, agents, commands, hooks, settings, `*/memory/*.md`, and MCP server names — and flags dead allow rules, oversized context files, duplicate rules and the like. It writes `out/lint/lint.{json,md}`. Add `--account 'LABEL=GLOB'` (repeatable) to label projects by path when you audit more than one account. The `/cua-audit` skill runs it and feeds `lint.md` to the comparator.

## Keeping cua up to date
```bash
python3 -m cua version     # installed git revision + whether it's current
python3 -m cua update      # git pull --ff-only to the latest
```

## Change-management (SDL) compliance audit (optional)

A second, opt-in audit — `/cua-sdl` (or `python3 -m cua sdl`) — checks whether **the agents** followed a secure-development-lifecycle / change-management process while changing code. It's judged from the same transcripts, so there's nothing extra to instrument. It looks at branch discipline, issue-tracker traceability, pull-request workflow, segregation of duties (no self-merge / self-approve / admin-bypass), committed secrets, and new dependencies, and splits findings into hard-rule **violations** and **attention** items. Things a transcript can't prove — that a human actually reviewed, UAT sign-off, release approval — are listed for a person to attest, never auto-passed.

As with the usage audit, only `cua sdl` touches raw transcripts; it writes redacted compliance facts to `out/<account>/sdl.json` and `sdl-events.jsonl`, which the `sdl-compliance-analyst` agent judges against your policy.

Configure it in `.env` (all optional):

| Setting | Effect |
|---|---|
| `TICKET_PREFIX` | Issue-tracker key(s) for the traceability checks, e.g. `PROJ` or `PROJ,OPS`. Blank disables those checks. |
| `POLICY_NAME` / `POLICY_DOC` | The change-management policy the analyst cites. Ships with a generic `.claude/skills/sdl-audit/references/example-sdl-policy.md`; point `POLICY_DOC` at your own policy extract to hold agents to it. |

Leave `TICKET_PREFIX` blank (or just don't run it) and the usage audit is unaffected.

## Layout
```
SPEC.md                         what is measured and why
CONTRIBUTING.md                 contributor guide (branches, tests, releasing)
CHANGELOG.md                    notable changes per version
cua/sources/claude_code.py      transcript parser: usage dedupe, denial classification, subagents, compactions
cua/sources/claude_ai.py        claude.ai export parser (token estimates)
cua/permissions.py              rule keys, settings matching, prompt estimation, risk tiers
cua/classify.py                 dev-cycle phases, correction/continue signals, prompt clustering
cua/redact.py                   strips values, secrets and paths from prompt text
cua/analyze.py                  builds summary.json + samples.jsonl
cua/waits.py                    session-stillness + approved_prompts (cua waits)
cua/lint.py                     config-layer audit of CLAUDE.md, skills, agents, hooks, settings (cua lint)
cua/sdl.py                      SDL / change-management compliance signals (cua sdl)
cua/update.py                   version + self-update (cua version / update)
cua/report.py                   per-account report + comparison
cua/config.py                   loads .env (account dirs, labels, ticket prefix, policy)
hooks/                          optional PermissionRequest logger + consent-first installer
scripts/make-public-snapshot.sh builds a clean public snapshot of this repo
.claude/agents/                 efficiency, blockers, patterns analysts + cross-account comparator
.claude/skills/usage-audit/     the orchestration procedure (/cua-audit)
.claude/skills/sdl-audit/       optional change-management compliance audit (/cua-sdl)
tests/                          unit tests + synthetic fixture generator
```

## Known limits
- **Approved permission prompts aren't logged by Claude Code.** By default they're inferred from your current allow rules and defaultMode. To measure them instead, install the shape-only logger in [`hooks/`](hooks/) (or any `PermissionRequest` hook) and point cua at its log (`PROMPT_LOG` in `.env`, or `cua waits --prompt-log`); `waits.json`'s `approved_prompts` then shows the prompts you actually approved, by shape. See [*Where time goes, and which prompts you approved*](#where-time-goes-and-which-prompts-you-approved-optional) above.
- **claude.ai exports have no token counts.** Usage there is estimated at about 4 characters per token.
- **Managed/org policy settings aren't parsed.** Run `/permissions` in Claude Code to see the effective rules.
