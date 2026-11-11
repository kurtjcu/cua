# Changelog

Notable changes to `cua`. The version lives in `cua/__init__.py`; bump it and add an
entry here on each release (see CONTRIBUTING.md → Releasing). Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic versioning.

## [Unreleased]

### Added
- **Shipped prompt-logger hook.** A small, shape-only `PermissionRequest` hook now ships in
  `hooks/`, with a consent-first installer (`python3 hooks/install.py`). It feeds
  `cua waits`' `approved_prompts` with the prompts you actually approved, logging only the
  *shape* of each prompt (never values or contents) to `~/.local/state/cua/prompts.log`. It
  is a pure observer — it never allows or denies a prompt and fails open. See
  `hooks/README.md`.
- **Measured permission approvals.** `cua waits` now emits an `approved_prompts` rollup —
  the prompts you actually approved (a logged prompt that was not `user_rejected`), grouped
  by shape with a suggested allow rule and risk tier. Sourced from an optional
  `PermissionRequest` hook log, set via `PROMPT_LOG` in `.env` or `--prompt-log`; empty when
  no log is supplied (Claude Code does not log approvals itself).

- **`.env`-driven account split.** `cua scan`, `waits` and `sdl` now default the project
  filter from the `.env` account whose label matches `--label`, via new
  `PROJECT_GLOB_1_EXCLUDE` / `PROJECT_GLOB_2_EXCLUDE` keys. Two accounts sharing one
  transcript folder (a symlinked `projects/`) can finally be split entirely from `.env`
  — the personal half excludes the work glob — instead of requiring
  `--include/--exclude-project` flags on every call. An explicit flag still overrides.

### Changed
- `cua waits` defaults `--label`, `--claude-dir` and `--prompt-log` from `.env`, so
  `cua waits --out out/me` works with no flags; `scan`/`sdl` default `--claude-dir` from
  the matched `.env` account too.
- The `/cua-audit` skill runs `cua waits` per account when `PROMPT_LOG` is set, and the
  `blocker-permissions-analyst` prefers the measured `approved_prompts` over the
  rule-based estimate.

## [0.1.0] - 2026-11-11

First versioned release of the toolkit.

### Added
- **Usage audit** — deterministic, stdlib-only extractors that reduce raw Claude Code /
  claude.ai logs to redacted `summary.json` + `samples.jsonl`: `cua scan`, `compare`,
  `preflight`, `waits`, and `lint` (config-layer audit). Agents read only `out/`.
- **SDL / change-management compliance audit** — `cua sdl` distils agent conduct from
  transcripts (branch discipline, issue-tracker traceability, PR workflow, segregation of
  duties, secrets, dependencies) into redacted compliance facts, judged by the
  `sdl-compliance-analyst` agent via the `sdl-audit` skill. Configurable issue-tracker
  prefix and policy document via `.env`.
- **Tooling commands** — `cua version`, `cua update` (remote-agnostic, git-based,
  fast-forward only), and `cua help`. On a run, a once-a-day fail-silent update check
  prints a one-line notice when the clone is behind its remote.
- **Claude Code entry points** — `/cua-audit`, `/cua-sdl`, `/cua-help`, `/cua-update`
  command wrappers over the skills.
- **Configuration** — `.env` (copied from `.env.example`) for account dirs, labels,
  project globs, issue-tracker prefix and policy; nothing secret.
- **Docs & release hygiene** — `CONTRIBUTING.md`, this changelog, and
  `scripts/make-public-snapshot.sh` to cut a public, tool-only snapshot (tracked files
  only, private notes and real policy stripped).
