---
name: usage-audit
description: Run the full personal-vs-work Claude usage audit (efficiency, blockers/permissions, prompt patterns vs dev cycle) from this repo. Use when asked to audit, re-run or refresh Claude usage analysis.
---
# Usage audit

Run this from the repo root. Read `SPEC.md` first if you haven't this session.

## 0. Preflight
- Run `python3 -m cua preflight`. Do **not** `ls` the config dirs or `exports/`: the project's
  `Read(~/.claude*/projects/**)` and `Read(./exports/**)` deny rules also block `ls` on those paths.
  Preflight prints only names and paths and never opens a transcript.
  - `SHARED` means two accounts read one transcript folder (e.g. one config dir is a symlink to
    the other). Split them by project path in step 1 (`PROJECT_GLOB_1`/`PROJECT_GLOB_2` in `.env`),
    or the comparison measures the same sessions twice.
  - `extra` lines list other machines' config dirs copied to `exports/<name>-claude/`. Ask about
    any that are missing, e.g. a WSL distro's `~/.claude`.
  - `none export …` means that zip is absent. Carry on without it and note the gap. The work export has to be requested from the **work** claude.ai account (Settings → Privacy → Export data). On Team/Enterprise plans the org admin may have to do it.
- Ask once whether to limit the window, e.g. `--since 30d`. The default is all history.

## 1. Scan
These are the only steps that touch raw data. Dirs, labels and globs default from
`.env` (see `.env.example`); the commands below show the explicit form.

**Single account (the common case):**
```
python3 -m cua scan --out out/me --export exports/me-*.zip
```

**Two accounts to compare:**
```
python3 -m cua scan --label personal --claude-dir ~/.claude      --exclude-project '<work-glob>' --export exports/personal-*.zip --out out/personal
python3 -m cua scan --label work     --claude-dir ~/.claude-work --include-project '<work-glob>' --export exports/work-*.zip     --out out/work --project-names hash
python3 -m cua compare out/personal out/work --out out/compare.md
```
- Quote project globs, or the shell expands them first.
- Use the `--include/--exclude-project` pair only when preflight reports a `SHARED` folder (two accounts sharing one transcript dir).
- Drop any `--export` whose file doesn't exist.
- If there are additional config dirs, add more `--claude-dir` flags.
- `--project-names hash` keeps client/project names out of shared analyses; use `basename` for readable names.
- `compare` warns when two accounts read the same folder with no project filter.

Then lint the config layer once (config files only, never a transcript):
```
python3 -m cua lint --root ~/code --out out/lint
```
Add `--account 'LABEL=GLOB'` (repeatable) to label projects by path when you audit more than one account, e.g. `--account 'work=/path/to/work-repos/*'`.
It reads config files only (CLAUDE.md, skills, agents, commands, hooks, settings, `*/memory/*.md`, MCP server names from `~/.claude.json`) and writes `out/lint/lint.{json,md}`. Pass `out/lint/lint.md` to the comparator in step 3.

**If `PROMPT_LOG` is set in `.env`** (you log permission prompts with a PermissionRequest hook), also run `waits` per account so the blocker analyst works from the prompts you *actually approved*, not an estimate:
```
python3 -m cua waits --out out/me
```
Two accounts: add `--label`/`--claude-dir` (and the same `--include/--exclude-project` split as scan) and run it once per account. This writes `out/<label>/waits.json`; its `approved_prompts` block is the real allow-list triage input. Skip this step when no `PROMPT_LOG` is set — the `cua scan` estimate still covers permissions.

## 2. Analyse (3 agents per account, in parallel)
Send the Agent calls in ONE message: `usage-efficiency-analyst`, `blocker-permissions-analyst` and `prompt-pattern-analyst`, once per account you scanned (three for a single account, six for two).
- Each prompt only needs to say: "Account: <label>. Inputs in out/<label>/. Follow your instructions." Add one line on the window and on how the accounts were split, if they were.

- Subagents may be barred from writing report files ("Subagents should return findings as text").
  If an analyst returns its report as text instead, save it yourself to
  `out/<label>/analysis-{efficiency,blockers,patterns}.md` before step 3.

## 3. Compare
With two accounts, run `cross-account-comparator` once all six analyses exist (with a
single account, skip this and use that account's three analyses directly). Tell it to
read `out/lint/lint.md` too, and to give config-layer findings their own section. If it
can't write either, ask it to return `AUDIT.md` and each `out/proposed/` file as
delimited sections, then save them.

## 4. Report back
Give the user the comparator's TL;DR and point them to `out/AUDIT.md` and `out/proposed/`.
- **Do not apply any proposed settings.** Offer to apply specific rules only if they name them.
- If they do name rules, edit the right settings file and show the diff first.
