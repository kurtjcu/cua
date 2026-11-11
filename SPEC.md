# Claude usage audit: spec

## Goal
Assess your Claude usage across one or more accounts (e.g. **personal** and **work**), then compare them:

1. **Efficiency:** where tokens go and how much of that is productive.
2. **Blockers:** what stops or slows work: permission prompts, denials, errors, interrupts and rework.
3. **Streamlining:** concrete, risk-rated changes such as allowlist rules, CLAUDE.md entries, skills, hooks and Projects.
4. **Patterns:** repeating prompts, mapped to the development cycle.
5. **Compare and contrast:** personal vs work.

## Principles
- **Raw data never leaves the machine.** Deterministic extractors (`cua/`, stdlib Python) turn raw logs into a redacted `summary.json` and `samples.jsonl`. Agents read only those. Project settings deny agent `Read` access to raw transcript folders.
- **Cheap layer first, judgement second.** Counting, matching and clustering are done in code. Agents interpret, cross-check samples, and write recommendations.
- **Never auto-apply.** Agents propose settings changes as diffs in `out/`. You apply them.
- **Every number carries its provenance:** *measured* (from usage fields), *estimated* (from characters, or inferred permission prompts), or *sampled* (from agent reading).

## Data sources
| Source | Location | What it has | Limits |
|---|---|---|---|
| Claude Code transcripts | `<config>/projects/**/*.jsonl` (`~/.claude`, any second account dir, plus any WSL/other machines) | Prompts, tool calls and results, token usage per message, model, cwd, branch, subagent sidechains, compactions | Approved permission prompts aren't logged, so they are **inferred** from settings (or **measured** from an optional prompt-log hook — see below) |
| Permission-prompt hook log (optional) | `PROMPT_LOG` in `.env` (a `PermissionRequest` hook writes it) | One row per prompt shown: time, kind, mode, project, shape, tool_use_id | Only present if you run such a hook; `cua waits` joins it to calls to measure approvals |
| Claude Code prompt history | `<config>/history.jsonl` | Every typed prompt, including sessions since deleted | No usage data |
| Claude Code settings | `<config>/settings*.json`, `<project>/.claude/settings*.json` | allow/deny/ask rules, defaultMode | Managed/enterprise policy files aren't read |
| claude.ai export | Settings → Privacy → Export data (zip) | Conversations, messages, tool use, projects | No token counts (estimated at about 4 characters per token) |
| claude.ai history (live) | Chat-history search tools | Used for ad-hoc interim findings | Sampled, not exhaustive |

## Metrics (summary.json)
- **meta:** account, sources, date range, sessions, prompts, estimated-token share, permission config sources.
- **efficiency:**
  - token breakdown (input, cache write, cache read, output)
  - cache-read ratio and subagent share
  - tokens and output per prompt (median, p90)
  - prompts per session and tool calls per prompt
  - tool error rate
  - correction, interrupt, bare-continue and context re-explain rates
  - compactions and API errors
  - retry loops (3 or more consecutive failures of the same rule key)
  - models, optional cost (from `pricing.json`)
  - breakdowns by month, by project, by hour and weekday, plus the heaviest sessions
- **blockers:**
  - denials by kind: `user_rejected`, `rule_denied`, `not_granted`, `hook_blocked`, `classifier_denied`, `sandbox_blocked`
  - denials by rule key, with the user's rejection feedback
  - **estimated permission prompts** by rule key: calls not covered by allow rules or defaultMode, and not prompt-free tools
  - tool errors by rule
  - allowlist suggestions split by risk tier (low means suggest, medium means review, high means keep prompting)
  - stale allow rules
- **patterns:**
  - phase distribution by prompts and by tokens
  - corrections following each phase
  - phase by month
  - phase transitions (Markov counts)
  - slash commands
  - prompt clusters (bigram Jaccard over normalised prompts), each with phase, projects, months and average tokens

### Dev-cycle phases
`research`, `planning_design`, `scaffolding`, `implementation`, `debugging`, `testing`, `review_refactor`, `deployment_ops`, `documentation`, `other`. The classifier uses keyword rules with a leading-verb boost, so it is transparent and easy to tune in `cua/classify.py`.

### Risk tiers (`cua/permissions.py`)
- **high:** destructive or remote side effects: `rm`, `sudo`, `git push/reset/clean`, `cdk deploy`, `terraform apply`, `kubectl delete`, `aws` write verbs, piping into a shell. These are never suggested for the allowlist.
- **medium:** installs, container runs, local git writes, network, arbitrary interpreters, Edit, MCP servers.
- **low:** read-only and local verification: tests, lint, build, `git status/diff/log`, `aws … list/describe`, `cdk synth/diff`, and similar.

## Agent pipeline (`/usage-audit` skill)
1. **Scan:** `python3 -m cua scan` once per account. These are the only commands that touch raw data.
2. **Analyse:** for each account, 3 analyst agents run in parallel (6 in total). Each writes to `out/<account>/analysis-<lens>.md`.
   - `usage-efficiency-analyst`
   - `blocker-permissions-analyst`
   - `prompt-pattern-analyst`
3. **Compare:** `cross-account-comparator` reads both summaries and all six analyses, then writes `out/AUDIT.md`.
4. **Propose:** the comparator also writes `out/proposed/` with a settings.json allowlist diff for each account, CLAUDE.md snippets, and skill/command stubs for the top automation candidates.
5. **Review:** you read `out/AUDIT.md` and apply what you want.

## Success criteria
- **Blockers:** the top 10 permission-prompt sources for each account, each with a risk-rated recommendation.
- **Patterns:** the top 10 repeating prompt patterns for each account, mapped to phases, with an automation verdict for each.
- **Efficiency:** at least 3 quantified efficiency findings for each account (for example, "x% of tokens go to sessions with more than 2 compactions").
- **Comparison:** a personal vs work table, plus a narrative on what differs and why.
- **Provenance:** every recommendation names its evidence (a metric or a sample row).

## Out of scope / known gaps
- Approved permission prompts aren't recorded by Claude Code; the rule-based estimate assumes the current settings applied throughout the whole period. Set `PROMPT_LOG` (a `PermissionRequest` hook log) and `cua waits` reports the approvals it actually measured (`approved_prompts`) instead.
- claude.ai token counts are estimates.
- Managed/enterprise settings (for example a work org policy) aren't parsed. Check `/permissions` in Claude Code for the effective rules.
