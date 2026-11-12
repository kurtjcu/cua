# Changelog

Notable changes to `cua`. The version lives in `cua/__init__.py`; bump it and add an
entry here on each release (see CONTRIBUTING.md → Releasing). Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic versioning.

## [Unreleased]

## [0.2.0] - 2026-11-13

### Added
- **Shipped prompt-logger hook.** A small, shape-only `PermissionRequest` hook now ships in
  `hooks/`, with a consent-first installer (`python3 hooks/install.py`). It feeds
  `cua waits`' `approved_prompts` with the prompts you actually approved. For each prompt it
  logs the tool name and a short pattern (for Bash the program name, never its arguments), the
  project path, the session and tool-use ids and a one-way call fingerprint, to
  `~/.local/state/cua/prompts.log`; see `hooks/README.md` for exactly what is kept. It is a pure
  observer — it never allows or denies a prompt and fails open. See `hooks/README.md`.
- **Measured permission approvals.** `cua waits` now emits an `approved_prompts` rollup —
  the prompts you actually approved (a logged prompt that was not `user_rejected`), grouped
  by shape with a suggested allow rule and risk tier. Sourced from an optional
  `PermissionRequest` hook log, set via `PROMPT_LOG` in `.env` or `--prompt-log`; empty when
  no log is supplied (Claude Code does not log approvals itself).
- **Prompt-log accounting.** `waits.json` `meta.prompt_log` reports rows read, rows kept and
  each exclusion reason (malformed, non-prompt kind, before `--since`, other account), and
  `cua waits` prints non-zero exclusions on stderr, so no log row disappears silently.
- **Hook tests.** `tests/test_prompt_logger.py` runs the real `hooks/prompt-logger.js` under
  node and reads its output back through `waits.load_prompt_log`, pinning call-hash parity,
  masking, the HOME boundary, the column order and fail-open behaviour. It skips cleanly
  when node is missing; set `CUA_HOOK_TEST_NODE` to select a node binary.

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
- `waits.analyse()` is now a thin orchestrator over per-section `rollup_*` functions
  (approvals, dialog groups, by label, by project, by day, gaps, modes, pauses, background
  style), each with its own test. `waits.json` output is unchanged, pinned by a
  characterisation test against a synthetic fixture.

### Fixed
- **`cua waits` counts prompt-log rows exactly as the shipped hook writes them.** The hook
  logs the project as a home-relative `~/…` path while transcripts record absolute paths, so
  the account filter and the same-project tie-break never matched and every legacy 5-column
  row was dropped from `logged_prompts` and `approved_prompts`. Project paths are now
  normalised before both. The account filter also follows the selected sessions, including
  ones whose calls have no latency.
- **The call hash `cua waits` recomputes now matches the one the hook logs.** JavaScript and
  Python wrote numbers (`1.0`, `1e20`, `1e-7`, `-0.0`, integers above 2^53) and non-BMP
  object keys differently, so the exact-call join silently missed for those inputs.
  `waits.call_hash` now follows JavaScript `JSON.stringify`. Reader-side only; the hook's
  hash is unchanged.
- **The logged shape no longer leaks quoted text.** Quotes are now read the way the shell
  reads them. Backslash-escaped quotes inside double quotes, the apostrophe idiom for single
  quotes (`'it'\''s'`) and `$'…'` strings are masked whole, and an unterminated or
  backslash-escaped quote masks the rest of the command (an apostrophe too: `don't` is
  masked from the apostrophe on). Previously `echo "pw is \"hunter2\" ok"` logged
  `hunter2`. Re-run `python3 hooks/install.py` to pick this up.
- **A Bash shape logs only the program name.** The shape is the leading run of letters,
  digits and `_ . + -` of the command after masking, or `<other>` when the command does not
  start with such a run. Nothing after it is written, even when no whitespace separates it
  from its arguments. So `cat<<<secret`, `echo${IFS}secret`, `{echo,secret}`,
  `echo>/path/secret`, URLs with credentials, database DSNs, `key:value` strings, paths, and
  plain arguments such as `echo hunter2` do not reach the log. A program given with a path
  logs its file name only when the path ends at a space, tab, newline or the end of the
  command and no `/` follows. Otherwise it logs `<other>`, so a directory is never logged,
  even one with a space, `;` or `=` in it (`/home/u/Acme Corp/run.sh`, `/x/acme;ls`). A name
  of 24 or more characters, counting `.` and `+`, logs `<other>`, and so does a command that
  is not a string. A command that starts with an assignment (`TOKEN=abc123 npm test`) logs
  the variable name (`Bash: TOKEN`); no value is ever logged. The limits: a secret typed as
  the command word itself is logged (a bare `hunter2` logs `Bash: hunter2`), and an unquoted
  space ends the word the shell runs (`Acme Corp/run.sh` logs `Bash: Acme`). `cua waits`
  reads only the tool name from the shape, so measurement is unchanged. Re-run
  `python3 hooks/install.py` to pick this up.
- **Prompt-logger hook hardening.** Control characters in any column are written as spaces,
  so a crafted tool input cannot shift columns or forge rows; a new log is created `0600` in
  a `0700` directory (an existing log keeps its mode, so `chmod 600` it); a HOME with a
  trailing slash still yields `~/` paths; a non-string path field no longer drops the row;
  a non-string `cwd` no longer drops the row; file-tool, Skill and WebFetch shapes are cut to
  60 characters (only Bash shapes are masked); and the call-hash docs now say it can confirm
  a guess at an exact command. Re-run `python3 hooks/install.py` to pick this up.
- **`cua waits` no longer aborts on a huge integer.** A tool input holding an integer too
  large for a double (above about 1.8e308) made the call hash raise; it now writes `null`, as
  JavaScript does. A transcript, `history.jsonl` or settings line holding an integer literal
  longer than Python's 4300-digit limit (3.11+) is now skipped like any other unparseable
  line instead of aborting the run. Reader-side only, so no re-install is needed.
- **Settings files are read as UTF-8, with undecodable bytes replaced.** Like transcripts and
  `history.jsonl`. One stray byte, or a locale that is not UTF-8, no longer drops every
  permission rule in the file, which made `cua scan` and `cua waits` count nearly every call
  as prompted. A settings file that is not valid JSON, or that holds an integer literal over
  the 4300-digit limit, is still skipped. Reader-side only, so no re-install is needed.
- **`hooks/install.py` exits cleanly on a settings file it cannot read.** Invalid UTF-8 or an
  integer literal over the 4300-digit limit now gives the `error: could not read …` message
  instead of a traceback. The file is left untouched, as before.
- **The hook replaces HOME with `~` only at a path boundary.** A project or file under a
  sibling such as `$HOME2/x`, or one that merely contains HOME later in the path, was
  rewritten to a misleading `~`-path. Re-run `python3 hooks/install.py` to pick this up.

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
