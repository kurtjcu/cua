# cua prompt-logger (optional)

A tiny [Claude Code `PermissionRequest` hook](https://docs.claude.com/en/docs/claude-code/hooks)
that records every permission prompt you see — including the ones you **approve**.

## Why you'd want it

Claude Code transcripts record only the prompts you *reject*. They don't record the
ones you *approve*. So by default `cua waits` has to **infer** which calls prompted,
from your allow rules.

With this hook installed, `cua waits` instead reports the prompts you **actually
approved** (its `approved_prompts` block) — grouped by shape, risk-rated, ready to
turn into allow rules. It's the difference between a guess and a measurement.

It's entirely optional. `cua` works without it; the estimate still covers permissions.

## What it does

Nothing but log. It is a **pure observer**:

- It **never** allows or denies a prompt — installing it cannot change which dialogs
  you see or what you approve.
- It **fails open**: on any error it exits silently and you get the normal dialog.
- It needs only Node's standard library.

## What it logs — and what it doesn't

It records a **shape**, never a value. One tab-separated row per prompt:

```
time   kind   mode   project   shape   tool_use_id   session_id   call_hash
```

- **shape** is the tool plus the first word or two — `Bash: git status`,
  `Edit: <proj>/src/app.ts`, `WebFetch: docs.claude.com`. Quoted strings, `=values`,
  and long tokens are masked out *before* anything is written.
- **call_hash** is a 12-hex fingerprint of the tool name + input so `cua waits` can
  match a logged prompt to the exact transcript call. It is **not reversible** and
  stores nothing readable.
- No command values, file contents, or arguments are ever written.

The log stays on your machine (default `~/.local/state/cua/prompts.log`). Nothing
leaves your computer.

## Install

From the repo root:

```bash
python3 hooks/install.py
```

It shows exactly what it will change and asks before touching anything. It:

1. copies `prompt-logger.js` into `~/.claude/hooks/`, and
2. adds a `PermissionRequest` entry to `~/.claude/settings.json` (merged in — your
   other hooks are left alone).

Then do the two steps it prints:

1. add `PROMPT_LOG=~/.local/state/cua/prompts.log` to your `.env`, and
2. **restart Claude Code** — settings are read at startup and not hot-reloaded.

Options:

```bash
python3 hooks/install.py --config-dir ~/.claude-work   # a second account's config dir
python3 hooks/install.py --yes                         # skip the confirmation prompt
python3 hooks/install.py --uninstall                   # remove the hook entry + file
```

Log somewhere else by setting `CUA_PROMPT_LOG` in the hook's environment; point
`PROMPT_LOG` in `.env` at the same file.

## Verify

After a restart, approve one prompt, then:

```bash
tail -n 2 ~/.local/state/cua/prompts.log
python3 -m cua waits        # reads PROMPT_LOG; see the approved_prompts block
```

## Uninstall

```bash
python3 hooks/install.py --uninstall
```

Removes the hook file and its settings entry. Your existing log is left in place —
delete it yourself if you want it gone.
