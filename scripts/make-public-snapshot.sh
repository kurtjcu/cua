#!/usr/bin/env bash
# Produce a clean, public snapshot of this repo in a NEW directory, then start a
# fresh git history there. The private repo (with its full history) is untouched.
#
# What this guarantees:
#   * Tracked files only — so .env, pricing.json, out/, exports/ and any untracked
#     local policy document are never copied.
#   * The private planning notes under docs/findings/ are removed.
#   * Only the generic example policy survives under the sdl-audit references dir.
#
# Prerequisite: commit the generalized tree first — this exports `git archive HEAD`.
#
# Usage: scripts/make-public-snapshot.sh /path/to/claude-usage-audit-public
set -euo pipefail

dest=${1:?usage: scripts/make-public-snapshot.sh <target-dir>}
if [ -e "$dest" ]; then
  echo "error: $dest already exists; choose a new, empty path" >&2
  exit 1
fi
mkdir -p "$dest"

# Tracked files only; untracked local files (.env, a real policy doc) are excluded.
git archive HEAD | tar -x -C "$dest"

# Remove private, non-tool content that is tracked here but must not be published.
rm -rf "$dest/docs/findings"
refs="$dest/.claude/skills/sdl-audit/references"
if [ -d "$refs" ]; then
  find "$refs" -type f ! -name 'example-sdl-policy.md' -delete
fi

echo "Snapshot written to $dest"
echo "Next:"
echo "  cd $dest"
echo "  cp .env.example .env        # local config, gitignored"
echo "  python3 -m unittest discover tests"
echo "  git init && git add -A && git commit -m 'Initial public release'"
