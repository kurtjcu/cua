"""Org/machine-specific configuration, loaded from a `.env` file at the repo root.

Everything here is paths and non-secret settings (see `.env.example`); the real
`.env` is gitignored. CLI flags always override whatever is set here, so the tool
works with no `.env` at all — the defaults below assume a single `~/.claude`.
"""
from __future__ import annotations

import os

DEFAULTS = {
    "CLAUDE_DIR": "~/.claude",      # primary account's Claude Code config dir
    "LABEL_1": "me",                # name for the primary account in output
    "PROJECT_GLOB_1": "",           # optional: keep only projects matching this glob
    "PROJECT_GLOB_1_EXCLUDE": "",   # optional: drop projects matching this glob (e.g. the other account's)
    "CLAUDE_DIR_2": "",             # optional second account (blank = single account)
    "LABEL_2": "work",
    "PROJECT_GLOB_2": "",
    "PROJECT_GLOB_2_EXCLUDE": "",
    "PROJECT_NAMES": "basename",    # basename | hash | full
    "PROMPT_LOG": "",               # optional permission-prompt hook log for `cua waits` (blank = infer prompts)
    "TICKET_PREFIX": "",            # issue-tracker prefix(es) for `cua sdl`, e.g. PROJ or PROJ,OPS
    "POLICY_NAME": "Change-management policy",
    "POLICY_DOC": ".claude/skills/sdl-audit/references/example-sdl-policy.md",
}


def load_env(path: str = ".env") -> dict:
    """Parse a simple KEY=VALUE `.env` (ignoring blanks, `#` comments and inline
    `# ...` trailers). Missing file => defaults only."""
    env = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.split(" #", 1)[0].strip().strip('"').strip("'")
                env[key.strip()] = val
    return env


def accounts(env: dict) -> list[dict]:
    """Resolve the configured account(s): a list of
    {label, claude_dir, include_glob, exclude_glob} dicts. One entry for a single-account
    setup, two when CLAUDE_DIR_2 is set. Two accounts that share one transcript folder are
    split by project path: give the work account an include glob (PROJECT_GLOB_2) and the
    personal account the matching exclude (PROJECT_GLOB_1_EXCLUDE), since include-only cannot
    express 'everything but the other account'."""
    out = []
    if env.get("CLAUDE_DIR"):
        out.append({"label": env.get("LABEL_1") or "me", "claude_dir": env["CLAUDE_DIR"],
                    "include_glob": env.get("PROJECT_GLOB_1") or None,
                    "exclude_glob": env.get("PROJECT_GLOB_1_EXCLUDE") or None})
    if env.get("CLAUDE_DIR_2"):
        out.append({"label": env.get("LABEL_2") or "work", "claude_dir": env["CLAUDE_DIR_2"],
                    "include_glob": env.get("PROJECT_GLOB_2") or None,
                    "exclude_glob": env.get("PROJECT_GLOB_2_EXCLUDE") or None})
    return out


def account_for_label(env: dict, label: str | None) -> dict | None:
    """The configured account whose LABEL matches `label`, else None."""
    for acct in accounts(env):
        if acct["label"] == label:
            return acct
    return None


def label_filters(env: dict, label: str | None):
    """(include, exclude) project globs for `label`, as single-item lists for
    project_selected; (None, None) when the account or its globs aren't configured."""
    acct = account_for_label(env, label)
    if not acct:
        return None, None
    inc = [acct["include_glob"]] if acct["include_glob"] else None
    exc = [acct["exclude_glob"]] if acct["exclude_glob"] else None
    return inc, exc
