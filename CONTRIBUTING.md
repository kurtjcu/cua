# Contributing

Thanks for working on `claude-usage-audit`. It's a small, dependency-free tool: a
deterministic Python layer that reduces raw Claude logs to redacted summaries, plus
Claude Code agent personas that read those summaries. Keep it that way and you'll be
fine. Read `CLAUDE.md` (how agents behave in this repo) and `SPEC.md` (what's measured
and why) alongside this.

## The rules that actually matter

1. **The privacy boundary is sacred.** Raw transcripts (`~/.claude*/projects`, export
   zips) are read **only** by `python3 -m cua …`. Agents and every output file read from
   `out/` only. Anything that could leave the machine goes through `cua/redact.py` first —
   distilled facts (counts, shapes, booleans, a redacted head), never raw prompt/commit/file
   text. If you add an extractor, it emits facts, not transcript content.
2. **Stdlib only, Python 3.9+.** No dependencies, no `pip install`. If you reach for a
   package, find another way — this is a deliberate constraint so anyone can run it with a
   bare Python.
3. **Tests stay green, and you add one.** Run `python3 -m unittest discover tests` before
   every PR. If you touch `cua/`, add or extend a test — and build fixtures from **redacted**
   lines, never from a real transcript (see `tests/test_*.py`, `tests/make_fixtures.py`).
4. **Never commit** `out/`, `exports/`, `.env`, or `pricing.json` (all gitignored — leave
   them that way). No secrets in commits. **No AI/assistant attribution** in commit messages
   or PR descriptions.

## Dev setup

```bash
git clone <repo> && cd claude-usage-audit
python3 --version                   # need 3.9+
cp .env.example .env                # paths/settings, not secrets; gitignored
python3 -m unittest discover tests  # sanity check, no deps
python3 -m cua help                 # tour of the commands
```

## House style (match what's already there)

- **Defensive parsing.** Log formats drift between Claude Code versions. Fail soft — one
  weird line must never crash a whole run. When a parse looks wrong, fix it defensively and
  add a redacted test case.
- **Docstrings explain *why*** — the non-obvious invariant or the bug that forced the shape —
  not what the code plainly does.
- **Every number carries its provenance:** measured, estimated, or sampled.
- **Cheap layer first, judgement second.** Counting/matching/clustering live in `cua/`; agents
  interpret. Don't push interpretation into the deterministic code or counting into the agents.
- **Before blaming a user's setup, check the measurement.** Several past "findings" were `cua`
  bugs. A surprising number is a hypothesis, not a result.

## Branching & pull requests

> This section is the maintainer's call — adjust to taste; it's written for a small team.

- `main` is protected: no direct pushes, no force-push.
- One branch per change: `feature/<short-name>` or `fix/<short-name>`, cut from `main`.
- Open a PR into `main`. At least one other developer reviews it; the author doesn't merge
  their own unreviewed PR. A human does the merge via GitHub.
- Imperative commit subjects, scoped small. Keep the diff reviewable.
- Optional dogfood: run `/cua-sdl` on your branch to check your own change-management hygiene
  before you open the PR — this repo audits exactly that.

## Releasing

The version is hand-maintained — one source of truth, bumped deliberately:

1. Bump `__version__` in `cua/__init__.py` (semver: patch for fixes, minor for new
   commands/checks, major for a breaking change to flags, output schema, or `.env`).
2. Move the relevant notes from `## [Unreleased]` into a new dated section in `CHANGELOG.md`.
3. Commit (e.g. `release: v0.2.0`). `cua version` reports the new number; the once-a-day
   update check tells clones they're behind once the commit reaches the remote.

`cua version` shows the release plus the git revision of the checkout (e.g.
`0.1.0 (ef6c787, 2026-11-11)`), so a working copy is distinguishable from a clean release.

## Keeping private things private

This is a **private** repo, and a public, tool-only build can be cut from it. Some content
must never be published even so:

- `docs/findings/` — private analysis notes.
- Any **real** change-management policy you point `POLICY_DOC` at — keep that file
  **untracked** (it's excluded locally via `.git/info/exclude`). Only the generic
  `references/example-sdl-policy.md` is tracked.

Public releases are produced with `scripts/make-public-snapshot.sh <target-dir>`, which exports
**tracked files only** and strips the above. The rule of thumb: don't commit anything you
wouldn't want to appear in that snapshot.
