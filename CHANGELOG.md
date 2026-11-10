# Changelog

Notable changes to `cua`. The version lives in `cua/__init__.py`; bump it and add an
entry here on each release (see CONTRIBUTING.md → Releasing). Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic versioning.

## [Unreleased]

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
