# claude-usage-audit

This repo audits your Claude usage across one or more accounts (e.g. a personal and a work login). See `SPEC.md` for the design and `.claude/skills/usage-audit/SKILL.md` for the run procedure (`/audit`).

## Rules
- Raw transcripts (`~/.claude*/projects`, export zips) are only ever read by `python3 -m cua …`. Agents read `out/` only. This keeps the work done here cheap and private.
- Never edit real Claude settings (`~/.claude*/settings*.json`) unless the user names the specific rules to apply. Proposals go in `out/proposed/`.
- Never commit `out/` or `exports/`; both are gitignored.
- Everything is stdlib Python 3.9+, with no dependencies. Run `python3 -m unittest discover tests` after changing `cua/`.
- Real-world log formats drift between Claude Code versions. If a parse looks wrong, fix `cua/sources/claude_code.py` defensively and add a test case built from a redacted line.
- Two accounts can share one config dir (e.g. a second config dir whose `settings.json`, `CLAUDE.md` and `projects` are symlinks to the first). When they share transcripts, split the accounts by project path (see the skill and `.env`), never by config dir.

## Analysis method
- Use the 5 whys. When a number or pattern surprises you, keep asking "why" and check each link against data until you reach a cause someone can act on. Then report the chain, mark which links are verified and which are inferred, and name the decision the user needs to make.
- Read-only digging needs no permission, so don't pause to ask "shall I look into X?".
- Before blaming the user's setup, check whether the measurement is wrong. Several "findings" in this audit were `cua` bugs (window leak, matcher, file contents counted as denials, batch rejections).

## Development
See `CONTRIBUTING.md` for the full contributor guide. The essentials, which always apply:
- Stdlib only (no dependencies). Tests green before every commit: `python3 -m unittest discover tests`. Add a redacted fixture for any `cua/` change.
- Work on a `feature/…` or `fix/…` branch and open a PR into `main`; a human reviews and merges. Never push to `main` directly, never merge your own unreviewed PR.
- Releasing: bump `__version__` in `cua/__init__.py`, move the `[Unreleased]` notes into a dated `CHANGELOG.md` section, then commit. `cua version` reports it.
- Keep private content out of git: `out/`, `exports/`, `.env` and any real policy doc are ignored; publish only via `scripts/make-public-snapshot.sh`.
